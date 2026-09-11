"""Command compatibility and end-to-end submission wiring with fake external services."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from compare import main, run
from profiles import extract_candidate_profile
from test_backfill import FIXTURE, UID

class ReviewCliTest(unittest.TestCase):
    def test_database_order_survives_csv_filter_and_limit(self):
        for use_csv in (False, True):
            with self.subTest(csv=use_csv), tempfile.TemporaryDirectory() as folder:
                repo=Mock();repo.database='test';repo.target_table='seek_scrap_detail'
                repo.iter_ids.return_value=[90,70,3] if use_csv else [90,12,70,3];repo.rows.return_value=[]
                repo.run_schedule.return_value = {'server_now': __import__('datetime').datetime(2026, 9, 7, 12), 'rows': [{'day': 1, 'time_from': '00:00', 'time_to': '23:59:59'}]}
                repo.scrap_idle_settings.return_value=dict(idle_less_than=0,idle_less_than2=0,idle_more_than=0,idle_more_than2=0)
                csv_file=Path(folder)/'ids.csv';csv_file.write_text('id\n3\n70\n90\n')
                args=argparse.Namespace(apply=False,auto_save=True,id=None,csv=str(csv_file) if use_csv else None,
                                        limit=2,no_ollama=True,uuid=None)
                with contextlib.redirect_stdout(io.StringIO()):
                    run(args,{'created_by':'collector','report_dir':folder},repo)
                self.assertEqual([c.args[0] for c in repo.rows.call_args_list],[90,70] if use_csv else [90,12])
                repo.iter_ids.assert_called_once_with(limit=2,candidate_ids={3,70,90} if use_csv else None)

    def test_new_and_legacy_flags_both_mean_proposals(self):
        for flags in (['--submit','--auto-propose'],['--apply','--auto-save']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'config.json'
                path.write_text(json.dumps({'created_by':'collector','database':{'host':'db','port':3306}}))
                repo=Mock();repo.server={'hostname':'server','port':3306}
                with patch('sys.argv',['compare.py','--config',str(path),'run',*flags]),patch('compare.Repository',return_value=repo),patch('compare.run') as runner,patch.dict('os.environ',{'SEEK_DB_PASSWORD':'test'}),contextlib.redirect_stdout(io.StringIO()):
                    main()
                args=runner.call_args.args[0];self.assertTrue(args.apply);self.assertTrue(args.auto_save)
                repo.apply.assert_not_called();repo.close.assert_called_once()
    def test_retired_mutation_commands_rejected_before_connecting(self):
        for command in ('prepare-detail','rollback'):
            with self.subTest(command=command),patch('sys.argv',['compare.py',command]),patch('compare.Repository') as repo,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:main()
                self.assertEqual(stopped.exception.code,2);repo.assert_not_called()
    def execute(self,auto=True,fail_report=False):
        repo=Mock();repo.database='remote';repo.target_table='seek_scrap_detail';repo.iter_ids.return_value=[42]
        repo.rows.return_value=[{'id':42,'id_pk':1,'uuid':12345,'name':None,'file':None,'scrap_date':None}]
        repo.run_schedule.return_value = {'server_now': __import__('datetime').datetime(2026, 9, 7, 12), 'rows': [{'day': 1, 'time_from': '00:00', 'time_to': '23:59:59'}]}
        repo.scrap_idle_settings.return_value=dict(idle_less_than=0,idle_less_than2=0,idle_more_than=0,idle_more_than2=0)
        repo.submit_proposal.return_value={'review_id':77,'status':'pending','created':True}
        browser=Mock();profile=extract_candidate_profile(FIXTURE)
        browser.numeric_profile.return_value=(profile,{'mode':'live_numeric'})
        browser.profile.return_value=profile;browser.search.return_value=({UID:'https://au.employer.seek.com/talentsearch/profiles/'+UID},True,'')
        args=argparse.Namespace(apply=True,auto_save=auto,id=42,csv=None,limit=1,no_ollama=True,uuid=None)
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser',return_value=browser),patch('builtins.input',side_effect=AssertionError('No approval prompt in collector')),contextlib.redirect_stdout(io.StringIO()) as output:
            config={'created_by':'collector','review':{'assigned_reviewer':'human-reviewer'},'report_dir':folder}
            if fail_report:
                from compare import write_report
                calls=0
                def failing_write(*a):
                    nonlocal calls
                    calls+=1
                    if calls==2:raise OSError('disk full')
                    return write_report(*a)
                with patch('compare.write_report',side_effect=failing_write),self.assertRaises(OSError):run(args,config,repo)
                report=None
            else:
                run(args,config,repo)
                report=json.loads(next(Path(folder).rglob('42.json')).read_text())
        return repo,browser,report,output.getvalue()
    def test_numeric_parent_link_is_compared_and_assignment_forwarded(self):
        repo,browser,report,_=self.execute()
        repo.submit_proposal.assert_called_once()
        repo.iter_ids.assert_not_called()  # Direct --id bypasses the large queue.
        self.assertEqual(repo.submit_proposal.call_args.args[-2:],('collector','human-reviewer'))
        self.assertEqual(report['review_status'],'pending');self.assertEqual(report['candidate_rows_updated'],0)
        self.assertNotIn('reviewed_by',report);repo.apply.assert_not_called()
        repo.preflight.assert_called_once_with(['seek_uuid_match_review','seek_uuid_match_review_history','seek_uuid_work_claim'])
        self.assertEqual(repo.submit_proposal.call_args.kwargs['claim_token'],repo.start_claim_lease.return_value.token)
        repo.start_claim_lease.return_value.close.assert_called_once()
    def test_nonautomatic_mode_submits_without_operator_review_prompts(self):
        repo,_,report,_=self.execute(auto=False)
        self.assertEqual(report['status'],'submitted')
        self.assertEqual(repo.submit_proposal.call_args.args[3]['selection_mode'],'ranked_proposal')
    def test_report_failure_after_commit_does_not_claim_submission_failed(self):
        repo,browser,_,output=self.execute(fail_report=True)
        self.assertIn('Proposal exists in the database',output)
        repo.submit_proposal.assert_called_once();browser.close.assert_called_once()

if __name__=='__main__':unittest.main()
