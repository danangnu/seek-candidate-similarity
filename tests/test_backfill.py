import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from profiles import extract_candidate_profile, compare_evidence, uuid_from_url, validate_verdict, canonical_uuid
from repository import check_mapping, fingerprint, Repository
from compare import csv_ids, resolve_file

UID = '35011ba6-373e-8818-5731-45449246b18e'
FIXTURE = '''<html><h2>Alex Example</h2><div role="tabpanel"><div><h4>Career history</h4><div>
<div>Welder</div><div data-testid="subHeading">Example Engineering</div>
<div data-testid="subHeadingSecondary">Jan 2018 – Dec 2022</div></div></div>
<div><h4>Education</h4><div><div data-testid="subHeading">Trade College</div>Certificate III</div></div>
</div><a href="/talentsearch/profiles/11111111-1111-1111-1111-111111111111"><h2>Other Person</h2>
<div><h4>Career history</h4><div data-testid="subHeading">Wrong company</div></div></a></html>'''

class ProfilesTest(unittest.TestCase):
    def test_scopes_roles_and_ignores_recommendations(self):
        p = extract_candidate_profile(FIXTURE)
        self.assertEqual(p['candidate_name'], 'Alex Example')
        self.assertEqual(p['work_history'], [{'company':'Example Engineering', 'title':'Welder', 'dates':'Jan 2018 – Dec 2022'}])
        self.assertIn('Trade College', p['education'][0])

    def test_name_only_not_eligible(self):
        p = extract_candidate_profile(FIXTURE); q=copy.deepcopy(p)
        q['work_history']=[]
        self.assertFalse(compare_evidence(p,q)['eligible_for_review'])

    def test_different_name_not_eligible(self):
        p = extract_candidate_profile(FIXTURE); q=copy.deepcopy(p);q['candidate_name']='Another Name'
        self.assertFalse(compare_evidence(p,q)['eligible_for_review'])

    def test_career_match_reviewable_not_automatic(self):
        p=extract_candidate_profile(FIXTURE)
        self.assertTrue(compare_evidence(p,p)['eligible_for_review'])
        self.assertNotIn('auto_save',compare_evidence(p,p))

    def test_ambiguous_main_profiles_rejected(self):
        with self.assertRaises(ValueError): extract_candidate_profile(FIXTURE+'<h2>Another main profile</h2>')

    def test_loading_profile_rejected(self):
        with self.assertRaises(ValueError): extract_candidate_profile('<h2>Alex Example</h2>')

    def test_url_validation_and_token_exclusion(self):
        self.assertEqual(uuid_from_url('https://au.employer.seek.com/talentsearch/profiles/'+UID+'?serviceToken=SECRET'), UID)
        for value in ['https://evil.example/talentsearch/profiles/'+UID,
                      'https://au.employer.seek.com/talentsearch/profiles/search',
                      'https://au.employer.seek.com/talentsearch/profiles/123']:
            with self.assertRaises(ValueError): uuid_from_url(value)

    def test_model_output_validation(self):
        for value in [{'is_same_person':'true','confidence':1,'reason':'x'},
                      {'is_same_person':True,'confidence':float('nan'),'reason':'x'},
                      {'is_same_person':True,'confidence':True,'reason':'x'}]:
            with self.assertRaises(ValueError):validate_verdict(value)

    def test_mapping_collision_guards(self):
        rows=[{'id':42,'uuid':None}]
        check_mapping(rows,[{'id':None,'uuid':UID}],42,UID)
        with self.assertRaises(ValueError): check_mapping(rows,[{'id':43,'uuid':UID}],42,UID)
        with self.assertRaises(ValueError): check_mapping([{'id':42,'uuid':'11111111-1111-1111-1111-111111111111'}],[],42,UID)
        with self.assertRaises(ValueError): check_mapping([],[],42,UID)

    def test_fingerprint_order_independent_and_changes_detected(self):
        rows=[{'id_pk':1,'uuid':None},{'id_pk':2,'uuid':''}]
        self.assertEqual(fingerprint(rows),fingerprint(list(reversed(rows))))
        changed=copy.deepcopy(rows);changed[0]['uuid']=UID
        self.assertNotEqual(fingerprint(rows),fingerprint(changed))

    def test_csv_deduplicates_numeric_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rows.csv';path.write_bytes(b'id,name\n42,Example\n42,Example\n43,A\n\\N,B\n')
            self.assertEqual(csv_ids(path),[42,43])

    def test_path_mapping_preserves_id_subfolder(self):
        cfg={'path_mappings':[{'from':r'\\server\share\SEEK', 'to':'copied_html'}]}
        self.assertEqual(resolve_file(r'\\server\share\SEEK\42\snapshot.txt',cfg),Path('copied_html/42/snapshot.txt'))

    def test_transaction_rolls_back_on_failure(self):
        repo=Repository.__new__(Repository);repo.database='seek_uuid_test_unit';repo.target_table='seek_scrap'
        conn=Mock();cur=Mock();cur.fetchone.return_value={'acquired':1}
        context=Mock();context.__enter__=Mock(return_value=cur);context.__exit__=Mock(return_value=False)
        conn.cursor.return_value=context;repo.connection=conn
        with self.assertRaises(RuntimeError):
            with repo.transaction():raise RuntimeError('simulate failed child/audit write')
        conn.rollback.assert_called_once();conn.commit.assert_not_called()


class PaginationTest(unittest.TestCase):
    def browser(self, totals, uuids, limit=5, card_names=None, singular=False):
        from seek_browser import SeekBrowser
        import types
        class Stale(Exception): pass
        class Element:
            def __init__(self, text='', href=None, click=None): self.text=text; self.href=href; self._click=click
            def is_displayed(self): return True
            def is_enabled(self): return True
            def get_attribute(self, name): return self.href
            def find_elements(self, by, value): return [Element(text=self.text)]
            def click(self): self._click()
        class Driver:
            index=0
            def get(self, url): self.index=0
            def advance(self): self.index+=1
            def find_elements(self, by, value):
                from profiles import COUNT_XPATH, CARDS_XPATH, NEXT_XPATH
                if value==COUNT_XPATH:return [Element(str(totals[self.index])+(' matching profile' if singular else ' matching profiles'))]
                if value==CARDS_XPATH:return [Element(text=(card_names or {}).get(u,'Alex Example'), href='https://au.employer.seek.com/talentsearch/profiles/'+u) for u in uuids[self.index]]
                if value==NEXT_XPATH:return [Element(click=self.advance)] if self.index+1<len(totals) else []
                return []
        class Wait:
            def until(self, callback):
                for _ in range(3):
                    result=callback(None)
                    if result:return result
                raise TimeoutError('No changed results')
        b=SeekBrowser.__new__(SeekBrowser); b.driver=Driver();b.wait=Wait();b.delay_seconds=0
        b.config={'max_search_pages':limit,'max_candidates':100}
        self.exceptions=types.ModuleType('selenium.common.exceptions')
        self.exceptions.StaleElementReferenceException=Stale
        self.exceptions.ElementClickInterceptedException=Stale
        return b

    def test_singular_matching_profile_is_recognized(self):
        from unittest.mock import patch
        b=self.browser([1],[[UID]],singular=True)
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            links,complete,_=b.search('Alex Example')
        self.assertTrue(complete);self.assertEqual(set(links),{UID})

    def test_different_names_filtered_after_all_pages(self):
        from unittest.mock import patch
        other='11111111-1111-1111-1111-111111111111'
        b=self.browser([2,2],[[other],[UID]],card_names={other:'Other Person',UID:' ALEX   Example '})
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            links,complete,_=b.search('Alex Example')
        self.assertTrue(complete);self.assertEqual(set(links),{UID})
        self.assertEqual(b.last_search_scan['cards_checked'],2)
        self.assertEqual(b.last_search_scan['skipped_different_names'],1)
        self.assertNotIn('serviceToken',json.dumps(b.last_search_scan))

    def test_no_same_name_returns_no_profile_visits(self):
        from unittest.mock import patch
        b=self.browser([1],[[UID]],card_names={UID:'Other Person'})
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            links,complete,_=b.search('Alex Example')
        self.assertTrue(complete);self.assertEqual(links,{})

    def test_unreadable_name_blocks_search(self):
        from unittest.mock import patch
        b=self.browser([1],[[UID]],card_names={UID:''})
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            with self.assertRaises(TimeoutError):b.search('Alex Example')

    def test_all_pages_collected(self):
        from unittest.mock import patch
        other='11111111-1111-1111-1111-111111111111'
        b=self.browser([2,2],[[UID],[other]])
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            links,complete,_=b.search('Alex Example')
        self.assertTrue(complete);self.assertEqual(set(links),{UID,other})

    def test_truncated_search_never_complete(self):
        from unittest.mock import patch
        b=self.browser([2,2],[[UID],['11111111-1111-1111-1111-111111111111']],limit=1)
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            _,complete,_=b.search('Alex Example')
        self.assertFalse(complete)

    def test_repeated_page_never_complete(self):
        from unittest.mock import patch
        b=self.browser([2,2],[[UID],[UID]])
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            with self.assertRaises(TimeoutError):b.search('Alex Example')

    def test_zero_result_search_is_recognized(self):
        from unittest.mock import patch
        b=self.browser([0],[[]])
        with patch.dict(sys.modules,{'selenium.common.exceptions':self.exceptions}):
            links,complete,_=b.search('Alex Example')
        self.assertTrue(complete);self.assertEqual(links,{})



class LiveNumericTest(unittest.TestCase):
    def test_numeric_route_and_redirect_validation(self):
        from profiles import numeric_source_identity
        base = 'https://au.employer.seek.com/talentsearch/profiles/'
        self.assertIsNone(numeric_source_identity(base+'42?serviceToken=SECRET',42))
        self.assertEqual(numeric_source_identity(base+UID,42),UID)
        for url in (base+'43', base+'search', 'https://evil.example/talentsearch/profiles/42'):
            with self.assertRaises(ValueError): numeric_source_identity(url,42)

    def run_fixture(self, direct, detail=False):
        import argparse
        import contextlib
        import io
        from unittest.mock import patch
        from compare import run
        profile=extract_candidate_profile(FIXTURE)
        repo=Mock(); repo.database='seek_uuid_test_unit'; repo.target_table='seek_scrap'; repo.iter_ids.return_value=[42]
        repo.rows.return_value=[{'id_pk':1,'id':42,'uuid':None,'name':'Alex Example',
                                 'file':'nonexistent-archived-file.txt','scrap_date':None}]
        if detail:
            repo.target_table='seek_scrap_detail'
            repo.rows.return_value[0].update({'name':None,'file':None,'scrap_date':None})
        repo.scrap_idle_settings.return_value = dict(idle_less_than=0, idle_less_than2=0, idle_more_than=0, idle_more_than2=0)
        browser=Mock()
        browser.numeric_profile.return_value=(profile, {'mode':'live_numeric','numeric_seek_id':42})
        browser.search.return_value=({UID:'https://au.employer.seek.com/talentsearch/profiles/'+UID},True,'')
        browser.profile.return_value=profile
        args=argparse.Namespace(apply=False,id=42,csv=None,limit=1,no_ollama=True,uuid=UID if direct else None)
        with tempfile.TemporaryDirectory() as folder:
            with patch('seek_browser.SeekBrowser',return_value=browser), patch('compare.source_profile') as archive:
                with contextlib.redirect_stdout(io.StringIO()):
                    run(args,{'report_dir':folder,'reviewer':'unit-test'},repo)
                archive.assert_not_called()
            report=json.loads(next(Path(folder).rglob('42.json')).read_text())
        browser.login.assert_called_once(); browser.numeric_profile.assert_called_once_with(42)
        browser.profile.assert_called_once(); repo.submit_proposal.assert_not_called()
        browser.close.assert_called_once()
        return browser,report

    def test_detail_target_can_read_live_numeric_profile_without_name_columns(self):
        browser,report=self.run_fixture(False,detail=True)
        self.assertEqual(report['status'],'proposal_dry_run')
        self.assertEqual(report['target_table'],'seek_scrap_detail')

    def test_live_default_does_not_require_archived_html(self):
        browser,report=self.run_fixture(False)
        browser.search.assert_called_once_with('Alex Example')
        self.assertEqual(report['status'],'proposal_dry_run')
        self.assertEqual(report['source']['mode'],'live_numeric')
        self.assertTrue(report['search_complete'])

    def test_direct_pair_does_not_claim_complete_name_search(self):
        browser,report=self.run_fixture(True)
        browser.search.assert_not_called()
        self.assertEqual(report['comparison_scope'],'direct_pair')
        self.assertIsNone(report['search_complete'])




def strong_profile():
    p=extract_candidate_profile(FIXTURE)
    p['summary']='Experienced trade worker with detailed employment history and specialist industrial fabrication skills. '*3
    p['work_history'].append({'company':'Second Engineering','title':'Fabricator','dates':'Jan 2023 - Dec 2025'})
    return p


def candidate(profile, uid=UID, baseline=None):
    return {'uuid':uid,'profile':profile,'evidence':compare_evidence(baseline or profile,profile)}


class AutomaticPolicyTest(unittest.TestCase):
    def decide(self, candidates, scope='name_search', complete=True):
        from profiles import automatic_match_decision
        return automatic_match_decision(candidates,scope,complete)

    def test_exact_profile_without_summary_or_two_employers_saves(self):
        p=extract_candidate_profile(FIXTURE)
        self.assertTrue(self.decide([candidate(p)])['eligible'])

    def test_same_name_different_content_does_not_block_exact_match(self):
        p=extract_candidate_profile(FIXTURE)
        q=extract_candidate_profile(FIXTURE.replace('Welder','Accountant'))
        first=candidate(q,'11111111-1111-1111-1111-111111111111',p)
        self.assertFalse(first['evidence']['profile_content_equal'])
        self.assertEqual(self.decide([first,candidate(p)])['uuid'],UID)

    def test_name_and_rank_alone_never_save(self):
        p=extract_candidate_profile(FIXTURE)
        q=extract_candidate_profile(FIXTURE.replace('Welder','Accountant'))
        c=candidate(q,baseline=p);c['evidence']['rank']=100
        c['ollama']={'is_same_person':True,'confidence':1}
        self.assertFalse(self.decide([c])['eligible'])

    def test_empty_or_unscoped_profile_never_saves(self):
        p=extract_candidate_profile(FIXTURE)
        p['profile_content_scope']='sections_only'
        self.assertFalse(self.decide([candidate(p)])['eligible'])
        p.update(profile_content_scope='profile_tab',work_history=[],summary='',education=[],licences=[])
        self.assertFalse(self.decide([candidate(p)])['eligible'])

    def test_changed_description_is_detected_even_when_roles_match(self):
        a=FIXTURE.replace('Jan 2018 – Dec 2022</div>', 'Jan 2018 – Dec 2022</div><p>Built steel frames.</p>')
        p=extract_candidate_profile(a)
        q=extract_candidate_profile(a.replace('Built steel frames.', 'Built wooden frames.'))
        c=candidate(q,baseline=p)
        self.assertEqual(c['profile']['work_history'],p['work_history'])
        self.assertIn('profile_content',c['evidence']['differing_fields'])
        self.assertFalse(self.decide([c])['eligible'])

    def test_education_dates_skills_and_unknown_sections_are_compared(self):
        a=FIXTURE.replace('</div><a href=', '<div><h4>Skills</h4><div>Welding</div></div><div><h4>New section</h4><div>Original value</div></div></div><a href=')
        p=extract_candidate_profile(a)
        for old,new in [('Certificate III','Certificate IV'),('Jan 2018','Jan 2019'),
                        ('Welding','Accounting'),('Original value','Changed value')]:
            with self.subTest(field=old):
                q=extract_candidate_profile(a.replace(old,new))
                self.assertFalse(self.decide([candidate(q,baseline=p)])['eligible'])

    def test_styling_navigation_tokens_and_whitespace_are_ignored(self):
        p=extract_candidate_profile(FIXTURE)
        changed=FIXTURE.replace('<html>', '<html><nav>Changed navigation</nav>')
        changed=changed.replace('<div role="tabpanel">','<div role="tabpanel" id="generated-new" class="style-new">')
        changed=changed.replace('Example Engineering','Example   Engineering')
        changed=changed.replace('Wrong company','Another recommendation')
        changed=changed.replace('Certificate III','Certificate III<button>Show less</button>')
        q=extract_candidate_profile(changed)
        evidence=compare_evidence(p,q)
        self.assertTrue(evidence['profile_content_equal'])
        self.assertEqual(evidence['old_content_sha256'],evidence['new_content_sha256'])

    def test_incomplete_search_blocks_but_explicit_pairs_and_redirects_can_match(self):
        c=candidate(extract_candidate_profile(FIXTURE))
        self.assertFalse(self.decide([c],complete=False)['eligible'])
        for scope in ('direct_pair','numeric_redirect'):
            self.assertTrue(self.decide([c],scope,None)['eligible'])

    def test_model_does_not_override_deterministic_content_equality(self):
        c=candidate(extract_candidate_profile(FIXTURE));c['ollama']={'is_same_person':False}
        self.assertTrue(self.decide([c])['eligible'])

    def test_unreadable_candidate_does_not_prevent_later_exact_match(self):
        self.assertTrue(self.decide([{'capture_error':'TimeoutError'},candidate(extract_candidate_profile(FIXTURE))])['eligible'])


class AutomaticWorkflowTest(unittest.TestCase):
    def run_case(self, apply, strong, conflict=False, batch=False):
        import argparse,contextlib,io
        from unittest.mock import patch
        from compare import run
        profile=strong_profile() if strong else extract_candidate_profile(FIXTURE)
        repo=Mock();repo.database='seek_uuid_test_unit'; repo.target_table='seek_scrap_detail';repo.iter_ids.return_value=[42,43] if batch else [42]
        repo.rows.return_value=[{'id_pk':1,'id':42,'uuid':None,'name':'Alex Example','file':'','scrap_date':None}]
        repo.submit_proposal.return_value={'review_id': 7, 'status': 'pending', 'created': True}
        if conflict:repo.submit_proposal.side_effect=ValueError('Conflicting approved identity mapping')
        repo.scrap_idle_settings.return_value = dict(idle_less_than=0, idle_less_than2=0, idle_more_than=0, idle_more_than2=0)
        browser=Mock();browser.numeric_profile.return_value=(profile,{'mode':'live_numeric'})
        browser.search.return_value=({UID:'https://au.employer.seek.com/talentsearch/profiles/'+UID},True,'')
        other='11111111-1111-1111-1111-111111111111'
        browser.search.return_value[0][other]='https://au.employer.seek.com/talentsearch/profiles/'+other
        browser.profile.return_value=profile if strong else extract_candidate_profile(FIXTURE.replace('Welder','Accountant'))
        args=argparse.Namespace(apply=apply,auto_save=True,id=None if batch else 42,csv=None,limit=2 if batch else 1,no_ollama=False,uuid=None)
        with tempfile.TemporaryDirectory() as folder:
            with patch('seek_browser.SeekBrowser',return_value=browser), patch('builtins.input',side_effect=AssertionError('Unexpected match prompt')), patch('compare.compare_profiles_with_ollama', side_effect=AssertionError('Auto mode must not call Ollama')):
                with contextlib.redirect_stdout(io.StringIO()):run(args,{'report_dir':folder,'reviewer':'test'},repo)
            report=json.loads(next(Path(folder).rglob('42.json')).read_text())
        return repo,report,browser

    def test_automatic_save_records_reason_without_match_prompts(self):
        repo,report,browser=self.run_case(True,True)
        repo.submit_proposal.assert_called_once()
        browser.profile.assert_called_once()
        self.assertEqual(report['profiles_not_visited'],1)
        self.assertEqual(report['target_table'],'seek_scrap_detail')
        self.assertEqual(repo.submit_proposal.call_args.args[:2],(42,UID))
        evidence=repo.submit_proposal.call_args.args[3]
        self.assertEqual(evidence['selection_mode'],'automatic_exact')
        self.assertIn('Automatic match',evidence['selection_reason'])
        self.assertEqual(report['status'],'submitted')

    def test_uncertain_match_skips_without_prompt(self):
        repo,report,browser=self.run_case(True,False)
        repo.submit_proposal.assert_not_called();self.assertEqual(report['status'],'auto_skipped')

    def test_automatic_preview_never_writes(self):
        repo,report,browser=self.run_case(False,True)
        repo.submit_proposal.assert_not_called();self.assertEqual(report['status'],'auto_eligible_dry_run')

    def test_first_match_saves_then_moves_to_next_numeric_id(self):
        repo,report,browser=self.run_case(True,True,batch=True)
        self.assertEqual(browser.profile.call_count,2)
        self.assertEqual(browser.numeric_profile.call_args_list[1].args,(43,))
        self.assertEqual(repo.submit_proposal.call_count,2)
        self.assertEqual(repo.submit_proposal.call_args_list[1].args[:2],(43,UID))
        # Proposals may share a UUID; the review app must resolve collisions before applying.

    def test_database_conflict_is_not_reported_as_saved(self):
        repo,report,browser=self.run_case(True,True,conflict=True)
        self.assertEqual(report['status'],'unresolved')
        self.assertNotIn('rows_updated',report)

if __name__ == '__main__': unittest.main()
