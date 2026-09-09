"""SEEK legacy-ID to UUID migration CLI. Run --help for commands."""
import argparse
import csv
import getpass
import json
import os
from pathlib import Path, PureWindowsPath
from uuid import uuid4
from profiles import extract_candidate_profile, read_html, compare_evidence, compare_profiles_with_ollama, norm, canonical_uuid, automatic_match_decision
from repository import Repository, blank, fingerprint


def resolve_file(value, config):
    # Preserve directory structure when remapping a network share. No basename guessing.
    for item in config.get('path_mappings', []):
        old, new = PureWindowsPath(item['from']), Path(item['to'])
        try:
            relative = PureWindowsPath(value).relative_to(old)
            if '..' in relative.parts:
                raise ValueError('Parent traversal in saved HTML path')
            return new.joinpath(*relative.parts)
        except ValueError:
            continue
    return Path(value)


def source_profile(rows, config):
    names = {norm(r['name']) for r in rows if r['name']}
    if len(names) != 1:
        raise ValueError('Historical rows have missing or inconsistent names; review this numeric ID manually')
    failures = 0
    for row in rows:  # Repository orders newest first.
        try:
            content, digest = read_html(resolve_file(row['file'] or '', config))
            profile = extract_candidate_profile(content)
            if norm(profile['candidate_name']) not in names:
                failures += 1
                continue
            return profile, {'source_id_pk': row['id_pk'], 'source_sha256': digest,
                             'newer_unusable_snapshots': failures}
        except (OSError, ValueError):
            failures += 1
    raise ValueError('No usable saved HTML matching the database name. Check file paths/path_mappings.')


def csv_ids(path):
    content = Path(path).read_bytes()
    try:
        content = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = content.decode('cp1252')
    import io
    rows = csv.DictReader(io.StringIO(content))
    if not rows.fieldnames or 'id' not in rows.fieldnames:
        raise ValueError('CSV must have an id column (numeric SEEK ID)')
    return sorted({int(r['id']) for r in rows if r['id'].isdigit() and int(r['id']) > 0})


def write_report(folder, numeric_id, report):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (str(numeric_id)+'.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
    return path


def run(args, config, repo):
    from seek_browser import SeekBrowser
    if getattr(args, 'auto_save', False):
        print('Automatic policy: first_identical_profile_content_v2 (first exact content match; no Ollama call)')
    if args.apply:
        repo.preflight(['seek_candidate_identity_map', 'seek_uuid_backfill_audit'])
    source_mode = config.get('source_mode', 'live_numeric')
    if source_mode not in ('live_numeric', 'saved_html'):
        raise ValueError('source_mode must be live_numeric or saved_html')
    if repo.target_table == 'seek_scrap_detail' and source_mode != 'live_numeric':
        raise ValueError('The detail target requires source_mode live_numeric; it has no name or saved HTML columns')
    pending = set(repo.ids())
    ids = [args.id] if args.id else [i for i in csv_ids(args.csv) if i in pending] if args.csv else sorted(pending)
    ids = ids[:args.limit]
    folder = Path(config.get('report_dir', 'reports')) / str(uuid4())
    browser = None
    try:
        for numeric_id in ids:
            report = {'numeric_seek_id': numeric_id, 'database': repo.database,
                      'target_table': repo.target_table, 'status': 'unresolved'}
            try:
                rows = repo.rows(numeric_id)
                if not rows:
                    raise ValueError('Numeric ID is not present in local '+repo.target_table+'; use prepare-detail to seed missing local detail rows')
                if not any(blank(r['uuid']) for r in rows):
                    report['status'] = 'already_mapped'
                    write_report(folder, numeric_id, report)
                    continue
                if browser is None:
                    browser = SeekBrowser(config.get('browser', {}))
                    browser.login()
                if source_mode == 'live_numeric':
                    report['stage'] = 'loading_live_numeric_profile'
                    old, source = browser.numeric_profile(numeric_id)
                    database_names = {norm(r['name']) for r in rows if r['name']}
                    if database_names and norm(old['candidate_name']) not in database_names:
                        raise ValueError('Live numeric-profile name differs from the database history; review this ID manually')
                else:
                    old, source = source_profile(rows, config)
                    source['mode'] = 'saved_html'
                report.update({'old_profile': old, 'source': source})
                print('\nNumeric SEEK ID', numeric_id, '|', old['candidate_name'], '| target rows', len(rows))
                requested_uuid = getattr(args, 'uuid', None)
                redirected_uuid = source.get('redirected_uuid')
                if requested_uuid:
                    uid = canonical_uuid(requested_uuid)
                    if redirected_uuid and uid != redirected_uuid:
                        raise ValueError('The numeric route redirects to a different UUID than the requested comparison')
                    links = {uid: 'https://au.employer.seek.com/talentsearch/profiles/'+uid}
                    complete, why = True, 'Direct pair selected by the operator; name search was not performed'
                    comparison_scope = 'direct_pair'
                elif redirected_uuid:
                    links = {redirected_uuid: 'https://au.employer.seek.com/talentsearch/profiles/'+redirected_uuid}
                    complete, why = True, 'Numeric URL redirected to this UUID; compare the profile before saving'
                    comparison_scope = 'numeric_redirect'
                else:
                    report['stage'] = 'loading_name_search_results'
                    links, complete, why = browser.search(old['candidate_name'])
                    comparison_scope = 'name_search'
                if comparison_scope == 'name_search':
                    scan = getattr(browser, 'last_search_scan', None)
                    if isinstance(scan, dict):
                        report['search_scan'] = scan
                report['comparison_scope'] = comparison_scope
                report.update({'search_complete': complete if comparison_scope == 'name_search' else None,
                               'search_note': why, 'candidates': []})
                if not complete:
                    raise ValueError(why)
                report['stage'] = 'comparing_uuid_profiles'
                for uid, url in links.items():
                    candidate = {'uuid': uid}
                    try:
                        profile = browser.profile(uid, url)
                        if comparison_scope == 'name_search' and norm(profile['candidate_name']) != norm(old['candidate_name']):
                            raise ValueError('Candidate name changed between search card and full profile; rerun the search')
                        candidate['profile'] = profile
                        candidate['evidence'] = compare_evidence(old, profile)
                        if not args.no_ollama and not getattr(args, 'auto_save', False):
                            try:
                                candidate['ollama'] = compare_profiles_with_ollama(old, profile,
                                    config.get('ollama', {}).get('model', 'llama3.1:8b'),
                                    config.get('ollama', {}).get('endpoint', 'http://127.0.0.1:11434'))
                            except Exception as ex:
                                candidate['ollama_error'] = type(ex).__name__
                        print('  Compared', uid, '| rank', candidate['evidence']['rank'],
                              '| identical Profile content:', candidate['evidence']['profile_content_equal'])
                    except Exception as ex:
                        # Selenium errors may embed token-bearing URLs: do not serialize them.
                        candidate['capture_error'] = type(ex).__name__
                    report['candidates'].append(candidate)
                    if getattr(args, 'auto_save', False):
                        decision = automatic_match_decision([candidate], comparison_scope, report['search_complete'])
                        if decision['eligible']:
                            print('  Exact Profile content match found; stopping further candidate profile visits.')
                            break
                report['profiles_not_visited'] = len(links) - len(report['candidates'])
                report['profiles_compared'] = sum('evidence' in c for c in report['candidates'])
                if not getattr(args, 'auto_save', False) and any('capture_error' in c for c in report['candidates']):
                    raise ValueError('One or more profiles could not be read; no mapping can be saved')
                ordered = sorted(report['candidates'], key=lambda c: (c.get('evidence', {}).get('profile_content_equal', False),
                    c.get('evidence', {}).get('rank', -1)), reverse=True)
                report['candidates'] = ordered
                report['status'] = 'ready_for_review' if ordered else 'no_matches'
                report_path = write_report(folder, numeric_id, report)
                print('Evidence report:', report_path.resolve())
                for index, candidate in enumerate(ordered, 1):
                    if 'capture_error' in candidate:
                        print(index, candidate['uuid'], 'profile unreadable:', candidate['capture_error'])
                        continue
                    print(index, candidate['profile']['candidate_name'], candidate['uuid'])
                    print(json.dumps(candidate['evidence'], ensure_ascii=False, indent=2))
                    if 'ollama' in candidate:
                        print('Ollama (advisory):', json.dumps(candidate['ollama'], ensure_ascii=False))
                    print('Career history:', json.dumps(candidate['profile']['work_history'], ensure_ascii=False))
                if getattr(args, 'auto_save', False):
                    decision = automatic_match_decision(ordered, comparison_scope, report['search_complete'])
                    report['automatic_decision'] = decision
                    if not decision['eligible']:
                        report['status'] = 'auto_skipped'
                        print('Automatic save skipped:', decision['reason'])
                    elif not args.apply:
                        report['status'] = 'auto_eligible_dry_run'
                        print('Would automatically save', decision['uuid'], '(dry run; add --apply to save).')
                    else:
                        selected = next(c for c in ordered if c['uuid'] == decision['uuid'])
                        evidence = {'source': source, 'old_profile': old, 'selected': selected,
                                    'comparison_scope': comparison_scope, 'search_complete': report['search_complete'],
                                    'profiles_not_visited': report['profiles_not_visited'],
                                    'search_scan': report.get('search_scan'),
                                    'candidates_compared': len(ordered), 'review_mode': 'automatic',
                                    'review_reason': decision['reason'], 'automatic_decision': decision,
                                    'candidate_ranks': [{'uuid': c['uuid'], 'name': c['profile']['candidate_name'],
                                                         'rank': c['evidence']['rank'],
                                                         'name_equal': c['evidence']['name_equal']} for c in ordered if 'evidence' in c]}
                        report['stage'] = 'saving_uuid'
                        change_id, count = repo.apply(numeric_id, selected['uuid'], fingerprint(rows), evidence,
                                                     config['reviewer'])
                        report.update({'status': 'saved', 'review_mode': 'automatic',
                                       'change_id': change_id, 'rows_updated': count})
                        print('Automatically saved UUID', selected['uuid'], 'for numeric SEEK ID', numeric_id,
                              '| target:', repo.target_table, '| rows updated:', count, '| rollback change ID:', change_id)
                    write_report(folder, numeric_id, report)
                    continue
                if not args.apply or not ordered:
                    continue
                print('Review OLD and NEW profiles in the JSON report. Ranking/confidence is not proof of identity.')
                choice = input('Candidate number to save, or Enter to skip: ').strip()
                if not choice:
                    report['status'] = 'skipped'
                elif not choice.isdigit() or not 1 <= int(choice) <= len(ordered):
                    report['status'] = 'invalid_selection'
                else:
                    selected = ordered[int(choice)-1]
                    if not selected['evidence']['eligible_for_review']:
                        raise ValueError('Insufficient independent identity evidence; name-only updates are blocked')
                    reason = input('Reason for confirming this identity match: ').strip()
                    if len(reason) < 10:
                        raise ValueError('A meaningful review reason is required')
                    phrase = 'SAVE '+str(numeric_id)+' '+selected['uuid']
                    if input('Type '+phrase+' to confirm: ').strip() != phrase:
                        report['status'] = 'skipped'
                    else:
                        evidence = {'source': source, 'old_profile': old, 'selected': selected,
                                    'comparison_scope': comparison_scope, 'search_complete': report['search_complete'],
                                    'search_scan': report.get('search_scan'),
                                    'candidates_compared': len(ordered), 'review_mode': 'manual', 'review_reason': reason}
                        report['stage'] = 'saving_uuid'
                        change_id, count = repo.apply(numeric_id, selected['uuid'], fingerprint(rows), evidence,
                                                     config['reviewer'])
                        report.update({'status': 'saved', 'change_id': change_id, 'rows_updated': count})
                        print('Saved', count, 'target rows. Rollback change ID:', change_id)
                write_report(folder, numeric_id, report)
            except Exception as ex:
                if report.get('status') == 'saved':
                    print('Mapping WAS committed; report write failed. Check the database audit table. Change ID:', report.get('change_id'))
                    raise
                report['status'] = 'unresolved'
                report['error_type'] = type(ex).__name__
                # Only our controlled ValueError messages are displayed; never driver/SQL raw exceptions.
                if type(ex) is ValueError:
                    print('Unresolved:', str(ex))
                    report['reason'] = str(ex)
                else:
                    print('Unresolved:', type(ex).__name__, '| stage:', report.get('stage', 'setup'),
                          '(no mapping saved; check browser/database)')
                write_report(folder, numeric_id, report)
    finally:
        if browser:
            browser.close()
    print('Reports:', folder.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-db', help='Create the two local mapping/audit tables')
    sub.add_parser('prepare-detail', help='Prepare local detail UUID column and seed missing numeric IDs from local seek_scrap')
    runner = sub.add_parser('run', help='Browse and compare; dry run unless --apply')
    runner.add_argument('--apply', action='store_true')
    runner.add_argument('--auto-save', action='store_true',
                        help='Stop at the first identical full Profile-content match; add --apply to save')
    runner.add_argument('--no-ollama', action='store_true')
    runner.add_argument('--limit', type=int, default=5)
    runner.add_argument('--uuid', help='Compare this UUID directly; requires --id')
    group = runner.add_mutually_exclusive_group()
    group.add_argument('--id', type=int)
    group.add_argument('--csv')
    rollback = sub.add_parser('rollback')
    rollback.add_argument('change_id')
    compare = sub.add_parser('compare-files', help='Compare two saved profiles without Chrome or MariaDB')
    compare.add_argument('old_html'); compare.add_argument('new_html')
    compare.add_argument('--no-ollama', action='store_true')
    args = parser.parse_args()
    if args.command == 'compare-files':
        old = extract_candidate_profile(read_html(args.old_html)[0])
        new = extract_candidate_profile(read_html(args.new_html)[0])
        result = {'old_profile': old, 'new_profile': new, 'evidence': compare_evidence(old, new)}
        if not args.no_ollama:
            result['ollama'] = compare_profiles_with_ollama(old, new)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if not config.get('reviewer') or len(config['reviewer']) > 100:
        raise ValueError('Set reviewer to your staff ID in config.json')
    if args.command == 'run' and (args.limit < 1 or (args.id is not None and args.id < 1)):
        raise ValueError('Use positive limit and numeric ID')
    if args.command == 'run' and args.uuid:
        if not args.id:
            raise ValueError('--uuid requires --id for a direct profile comparison')
        args.uuid = canonical_uuid(args.uuid)
    password = os.environ.get('SEEK_DB_PASSWORD')
    if password is None:
        password = getpass.getpass('Local MariaDB password: ')
    repo = Repository(config['database'], password)
    try:
        print('Connected:', config['database']['host'], repo.server['port'], repo.database,
              '| server hostname:', repo.server['hostname'], '| target:', repo.target_table)
        if args.command == 'prepare-detail':
            repo.prepare_detail()
        elif args.command == 'init-db':
            repo.initialize(); print('Mapping and audit tables ready.')
        elif args.command == 'rollback':
            if input('Type ROLLBACK '+args.change_id+' to restore UUID values: ').strip() == 'ROLLBACK '+args.change_id:
                print('Restored rows:', repo.rollback(args.change_id))
        else:
            run(args, config, repo)
    finally:
        repo.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Any committed mappings remain in the audit table.')
    except Exception as error:
        print('Stopped:', str(error) if type(error) is ValueError else type(error).__name__)
        if error.args and type(error.args[0]) is int:
            print('Database error code:', error.args[0], '(check local credentials, permissions and schema)')
        raise SystemExit(1)
