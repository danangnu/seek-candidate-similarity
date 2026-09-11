"""SEEK numeric-ID to UUID review proposal CLI. Run --help for commands."""
import argparse
import csv
import getpass
import json
import os
from pathlib import Path, PureWindowsPath
from uuid import uuid4
from profiles import extract_candidate_profile, read_html, compare_evidence, compare_profiles_with_ollama, norm, canonical_uuid, automatic_match_decision
from repository import Repository, fingerprint, review_actors
from runtime_breaks import RuntimeBreaks, BreakSettingsError, validate_settings
from network_config import ollama_options, check_ollama
from work_claims import ClaimLost, claim_settings, worker_identity


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
    from seek_browser import SeekBrowser, BrowserReadError
    created_by, assigned_reviewer = review_actors(config)
    print('Review-only mode: submit runs write work claims and pending proposals; candidate tables are never updated.')
    use_ollama = not args.no_ollama and (not getattr(args, 'auto_save', False) or getattr(args, 'with_ollama', False))
    if getattr(args, 'auto_save', False):
        print('Automatic policy: first_identical_profile_content_v2 (first exact content match)')
    print('Ollama advisory:', 'enabled' if use_ollama else 'disabled')
    settings = claim_settings(config)
    worker_id = worker_identity()
    if args.apply:
        repo.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_uuid_work_claim'])
        print('Shared work claims enabled | worker:', worker_id)
    else:
        print('Dry run: no work claims or database writes; simultaneous previews may overlap.')
    source_mode = config.get('source_mode', 'live_numeric')
    if source_mode not in ('live_numeric', 'saved_html'):
        raise ValueError('source_mode must be live_numeric or saved_html')
    if repo.target_table == 'seek_scrap_detail' and source_mode != 'live_numeric':
        raise ValueError('The detail target requires source_mode live_numeric; it has no name or saved HTML columns')
    from itertools import chain, islice
    csv_selection = set(csv_ids(args.csv)) if args.csv else None
    queue = (iter([args.id]) if args.id else
             iter(repo.iter_ids(limit=None, candidate_ids=csv_selection, exclude_claimed=True)) if args.apply else
             iter(repo.iter_ids(limit=args.limit, candidate_ids=csv_selection)))
    first_id = next(queue, None)
    ids = chain([first_id], queue if args.apply else islice(queue, args.limit-1)) if first_id is not None else ()
    breaks = RuntimeBreaks(created_by, repo.scrap_idle_settings,
                           config.get('breaks', {}).get('settings_id', 1)) if first_id is not None else None
    folder = Path(config.get('report_dir', 'reports')) / str(uuid4())
    browser = None
    attempted = 0
    try:
        for numeric_id in ids:
            lease = None
            if args.apply:
                lease = repo.start_claim_lease(numeric_id, worker_id, created_by, settings, allow_rejected=bool(args.id))
                if lease is None:
                    print('Skipped numeric SEEK ID', numeric_id, '| already reviewed, reserved, or awaiting retry.')
                    continue
                print('Claimed numeric SEEK ID', numeric_id, '| worker:', worker_id)
            attempted += 1
            report = {'numeric_seek_id': numeric_id, 'database': repo.database,
                      'target_table': repo.target_table, 'database_host': config.get('database', {}).get('host'), 'status': 'unresolved', 'created_by': created_by,
                      'assigned_reviewer': assigned_reviewer, 'storage_table': 'seek_uuid_match_review',
                      'worker_id': worker_id if lease else None}
            try:
                if lease:
                    lease.check()
                rows = repo.rows(numeric_id)
                if not rows:
                    raise ValueError('Numeric ID is not present in the configured '+repo.target_table+'; check the selected database and candidate sample')
                if browser is None:
                    browser = SeekBrowser(config.get('browser', {}))
                    browser.claim_guard = lease.check if lease else None
                    browser.login()
                    breaks.start()
                browser.claim_guard = lease.check if lease else None
                report['stage'] = 'runtime_break'
                report['runtime_break'] = breaks.before_candidate()
                # The wait may exceed the database idle timeout. Refresh outside any write transaction.
                breaks.refresh()
                if lease:
                    lease.check()
                rows = repo.rows(numeric_id)
                if not rows:
                    raise ValueError('Numeric ID disappeared from the configured target during the break')
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
                        if use_ollama:
                            if lease:
                                lease.check()
                            try:
                                candidate['ollama'] = compare_profiles_with_ollama(old, profile,
                                    **ollama_options(config.get('ollama', {})))
                            except Exception as ex:
                                candidate['ollama_error'] = type(ex).__name__
                        print('  Compared', uid, '| rank', candidate['evidence']['rank'],
                              '| identical Profile content:', candidate['evidence']['profile_content_equal'])
                    except ClaimLost:
                        raise
                    except Exception as ex:
                        # Selenium errors may embed token-bearing URLs: do not serialize them.
                        candidate['capture_error'] = type(ex).__name__
                        if isinstance(ex, BrowserReadError):
                            candidate['reason'] = str(ex)
                            candidate['browser_diagnostics'] = ex.diagnostics
                    report['candidates'].append(candidate)
                    if getattr(args, 'auto_save', False):
                        decision = automatic_match_decision([candidate], comparison_scope, report['search_complete'])
                        if decision['eligible']:
                            print('  Exact Profile content match found; stopping further candidate profile visits.')
                            break
                if lease:
                    lease.check()
                report['profiles_not_visited'] = len(links) - len(report['candidates'])
                report['profiles_compared'] = sum('evidence' in c for c in report['candidates'])
                if not getattr(args, 'auto_save', False) and any('capture_error' in c for c in report['candidates']):
                    raise ValueError('One or more profiles could not be read; no proposal can be submitted')
                ordered = sorted(report['candidates'], key=lambda c: (c.get('evidence', {}).get('profile_content_equal', False),
                    c.get('evidence', {}).get('rank', -1)), reverse=True)
                report['candidates'] = ordered
                report['status'] = 'ready_for_review' if ordered else 'no_matches'
                report_path = write_report(folder, numeric_id, report)
                print('Evidence report:', report_path.resolve())
                for index, candidate in enumerate(ordered, 1):
                    if 'capture_error' in candidate:
                        print(index, candidate['uuid'], 'profile unreadable:', candidate['capture_error'])
                        if candidate.get('reason'):
                            print('Reason:', candidate['reason'])
                        continue
                    print(index, candidate['profile']['candidate_name'], candidate['uuid'])
                    print(json.dumps(candidate['evidence'], ensure_ascii=False, indent=2))
                    if 'ollama' in candidate:
                        print('Ollama (advisory):', json.dumps(candidate['ollama'], ensure_ascii=False))
                    print('Career history:', json.dumps(candidate['profile']['work_history'], ensure_ascii=False))
                if getattr(args, 'auto_save', False):
                    decision = automatic_match_decision(ordered, comparison_scope, report['search_complete'])
                    report['automatic_decision'] = decision
                    selected = next((c for c in ordered if c['uuid'] == decision['uuid']), None) if decision['eligible'] else None
                else:
                    # Queue the strongest reviewable comparison, without approving it.
                    selected = next((c for c in ordered if c.get('evidence', {}).get('profile_content_equal')
                                     or c.get('evidence', {}).get('eligible_for_review')), None)
                    decision = {'eligible': selected is not None,
                                'reason': 'Strongest comparison proposed for human review; no approval has been made.'}
                if selected is None:
                    report['status'] = 'auto_skipped' if getattr(args, 'auto_save', False) else 'no_reviewable_match'
                    print('Proposal skipped:', decision['reason'])
                elif not args.apply:
                    report['status'] = 'auto_eligible_dry_run' if getattr(args, 'auto_save', False) else 'proposal_dry_run'
                    print('Would submit pending proposal', selected['uuid'], '(dry run; add --submit).')
                else:
                    evidence = {'worker_id': worker_id, 'source': source, 'old_profile': old, 'selected': selected,
                                'comparison_scope': comparison_scope, 'search_complete': report['search_complete'],
                                'profiles_not_visited': report['profiles_not_visited'],
                                'search_scan': report.get('search_scan'), 'runtime_break': report.get('runtime_break'),
                                'candidates_compared': len(ordered),
                                'selection_mode': 'automatic_exact' if getattr(args, 'auto_save', False) else 'ranked_proposal',
                                'selection_reason': decision['reason'], 'automatic_decision': report.get('automatic_decision'),
                                'report_reference': str(report_path.resolve()),
                                'candidate_ranks': [{'uuid': c['uuid'], 'name': c['profile']['candidate_name'],
                                                     'rank': c['evidence']['rank'],
                                                     'name_equal': c['evidence']['name_equal']} for c in ordered if 'evidence' in c]}
                    report['stage'] = 'submitting_review_proposal'
                    lease.check()
                    result = repo.submit_proposal(numeric_id, selected['uuid'], fingerprint(rows), evidence,
                                                  created_by, assigned_reviewer, claim_token=lease.token)
                    report.update({'status': 'submitted' if result['created'] else 'already_submitted',
                                   'review_id': result['review_id'], 'review_status': result['status'],
                                   'candidate_rows_updated': 0, 'assigned_reviewer': result.get('assigned_reviewer')})
                    print('Pending proposal submitted.' if result['created'] else 'Existing proposal retained.',
                          '| review ID:', result['review_id'], '| status:', result['status'],
                          '| assigned reviewer:', result.get('assigned_reviewer') or '(unassigned)', '| candidate rows updated: 0')
                write_report(folder, numeric_id, report)
            except ClaimLost as ex:
                report.update(status='stopped_claim_lost', reason=str(ex))
                write_report(folder, numeric_id, report)
                raise
            except BreakSettingsError as ex:
                report.update(status='stopped_invalid_break_settings', reason=str(ex))
                write_report(folder, numeric_id, report)
                raise
            except Exception as ex:
                if report.get('status') in ('submitted', 'already_submitted'):
                    print('Proposal exists in the database; report write failed. Check review ID:', report.get('review_id'))
                    raise
                report['status'] = 'unresolved'
                report['error_type'] = type(ex).__name__
                if isinstance(ex, BrowserReadError):
                    report['browser_diagnostics'] = ex.diagnostics
                scan = getattr(browser, 'last_search_scan', None) if browser else None
                if isinstance(scan, dict):
                    report['search_scan'] = scan
                # Only our controlled ValueError messages are displayed; never driver/SQL raw exceptions.
                if type(ex) is ValueError or isinstance(ex, BrowserReadError):
                    print('Unresolved:', str(ex))
                    report['reason'] = str(ex)
                else:
                    print('Unresolved:', type(ex).__name__, '| stage:', report.get('stage', 'setup'),
                          '(proposal not confirmed; check review tables and browser/database)')
                write_report(folder, numeric_id, report)
            finally:
                if browser:
                    browser.claim_guard = None
                if lease:
                    import sys
                    interrupted = isinstance(sys.exc_info()[1], KeyboardInterrupt)
                    stopped = lease.close()
                    try:
                        if stopped:
                            repo.finish_claim(numeric_id, lease.token,
                                              0 if interrupted else settings['retry_seconds'],
                                              'interrupted' if interrupted else report['status'])
                    except Exception:
                        print('Claim cleanup not confirmed; it will become retryable after expiry.')
            if attempted >= args.limit:
                break
    finally:
        if browser:
            browser.close()
    print('Reports:', folder.resolve())


def check_connections(config, repo, no_ollama=False):
    failures = []
    checks = [
        ('Candidate source and profile dates (read-only)', lambda: repo.preflight([repo.target_table, 'seek_scrap'])),
        ('Review queue, history and work claims', lambda: repo.preflight(['seek_uuid_match_review', 'seek_uuid_match_review_history', 'seek_uuid_work_claim'])),
        ('Runtime break settings', lambda: validate_settings(repo.scrap_idle_settings(config.get('breaks', {}).get('settings_id', 1))))]
    if not no_ollama:
        checks.append(('Ollama endpoint and model', lambda: check_ollama(**ollama_options(config.get('ollama', {})))))
    for label, action in checks:
        try:
            action()
            print('OK:', label)
        except Exception as ex:
            # Driver/HTTP exception strings may contain authentication data.
            reason = str(ex) if isinstance(ex, (ValueError, BreakSettingsError)) else type(ex).__name__
            print('FAILED:', label, '|', reason)
            failures.append(label)
    print('Connection check complete. No Chrome session, candidate payload, or database writes.')
    if failures:
        raise ValueError('Connection checks failed: '+', '.join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-db', help='Create review queue/history and work-claim tables only in the configured database')
    check = sub.add_parser('check-connections', help='Read-only database/schema/settings and Ollama-model checks; no Chrome or writes')
    check.add_argument('--no-ollama', action='store_true')
    queue_check = sub.add_parser('check-queue', help='Read-only queue timing and optional EXPLAIN; no browser or writes')
    queue_check.add_argument('--limit', type=int, default=5)
    queue_check.add_argument('--explain', action='store_true')
    queue_check.add_argument('--available-only', action='store_true',
                             help='Also exclude live work claims and retry delays; still read-only')
    runner = sub.add_parser('run', help='Compare and propose for review; dry run unless --submit')
    runner.add_argument('--submit', '--apply', dest='apply', action='store_true',
                        help='Submit pending proposals ONLY; --apply is a compatibility alias, never a candidate update')
    runner.add_argument('--auto-propose', '--auto-save', dest='auto_save', action='store_true',
                        help='Stop at the first identical full Profile-content match; add --submit to queue for review')
    ai_flags = runner.add_mutually_exclusive_group()
    ai_flags.add_argument('--no-ollama', action='store_true')
    ai_flags.add_argument('--with-ollama', action='store_true', help='Also request an advisory Ollama comparison in auto-save mode')
    runner.add_argument('--limit', type=int, default=5)
    runner.add_argument('--uuid', help='Compare this UUID directly; requires --id')
    group = runner.add_mutually_exclusive_group()
    group.add_argument('--id', type=int)
    group.add_argument('--csv')
    compare = sub.add_parser('compare-files', help='Compare two saved profiles without Chrome or MariaDB')
    compare.add_argument('old_html'); compare.add_argument('new_html')
    compare.add_argument('--no-ollama', action='store_true')
    args = parser.parse_args()
    if args.command == 'compare-files':
        old = extract_candidate_profile(read_html(args.old_html)[0])
        new = extract_candidate_profile(read_html(args.new_html)[0])
        result = {'old_profile': old, 'new_profile': new, 'evidence': compare_evidence(old, new)}
        if not args.no_ollama:
            file_config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if Path(args.config).exists() else {}
            result['ollama'] = compare_profiles_with_ollama(old, new, **ollama_options(file_config.get('ollama', {})))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    review_actors(config)
    if args.command == 'check-queue' and args.limit < 1:
        raise ValueError('Queue limit must be positive')
    if args.command == 'run' and (args.limit < 1 or (args.id is not None and args.id < 1)):
        raise ValueError('Use positive limit and numeric ID')
    if args.command == 'run' and args.uuid:
        if not args.id:
            raise ValueError('--uuid requires --id for a direct profile comparison')
        args.uuid = canonical_uuid(args.uuid)
    password = os.environ.get('SEEK_DB_PASSWORD')
    if password is None:
        password = getpass.getpass('MariaDB password: ')
    repo = Repository(config['database'], password)
    try:
        print('Connected:', config['database']['host'], config['database'].get('port', 3306), repo.database,
              '| server hostname:', repo.server['hostname'], '| server port:', repo.server['port'], '| target:', repo.target_table)
        if args.command == 'check-connections':
            check_connections(config, repo, no_ollama=args.no_ollama)
        elif args.command == 'check-queue':
            import time
            if args.explain:
                repo.explain_queue()
            start = time.monotonic()
            for numeric_id in repo.iter_ids(limit=args.limit, exclude_claimed=args.available_only):
                print('Queued numeric ID:', numeric_id)
            print('Queue check elapsed seconds:', round(time.monotonic()-start, 3))
            print('No browser session or database writes.')
        elif args.command == 'init-db':
            repo.initialize(); print('Review queue, history and work-claim tables ready. Candidate tables unchanged.')
        else:
            run(args, config, repo)
    finally:
        repo.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped. Any submitted proposals remain pending in the review queue.')
    except ClaimLost as error:
        print('Stopped:', str(error))
        raise SystemExit(1)
    except Exception as error:
        print('Stopped:', str(error) if isinstance(error, (ValueError, BreakSettingsError)) else type(error).__name__)
        if error.args and type(error.args[0]) is int:
            print('Database error code:', error.args[0], '(check configured credentials, permissions and schema)')
        raise SystemExit(1)
