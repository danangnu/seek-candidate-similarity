"""Claim SQL/state tests. SQLite models DB time/locks; live MariaDB is a separate gate."""
import argparse
import contextlib
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import test_detail_repository as fixtures
from repository import Repository, fingerprint
from work_claims import ClaimLease, ClaimLost, claim_settings, worker_identity
from compare import run
from seek_browser import SeekBrowser


class ClaimSQLTest(unittest.TestCase):
    setUp = fixtures.ReviewRepositoryTest.setUp
    tearDown = fixtures.ReviewRepositoryTest.tearDown
    submit = fixtures.ReviewRepositoryTest.submit
    reject = fixtures.ReviewRepositoryTest.reject

    def claim(self, worker='machine-A', numeric=42, allow_rejected=False):
        return self.repo.claim_candidate(numeric, worker, 'staff', 600, allow_rejected)

    def advance(self, seconds):
        self.repo.connection.now += timedelta(seconds=seconds)

    def test_only_one_worker_can_claim_person_and_source_is_unchanged(self):
        token = self.claim()
        self.assertTrue(token)
        self.assertIsNone(self.claim('machine-B'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_work_claim').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT uuid FROM seek_scrap_detail').fetchone()[0],'12345')
        self.assertEqual(self.db.execute('SELECT seek_scrap_id FROM seek_scrap').fetchone()[0],100)

    def test_expired_claim_can_be_taken_but_stale_owner_cannot_save_renew_or_release(self):
        old = self.claim()
        self.advance(600)
        new = self.claim('machine-B')
        self.assertNotEqual(old,new)
        with self.assertRaises(ClaimLost):self.repo.renew_claim(42,old,600)
        with self.assertRaises(ClaimLost):
            self.repo.submit_proposal(42,fixtures.UID,fingerprint(self.repo.rows(42)),self.evidence,'staff',claim_token=old)
        self.repo.finish_claim(42,old,0,'interrupted')
        row=dict(self.db.execute('SELECT * FROM seek_uuid_work_claim').fetchone())
        self.assertEqual(row['claim_token'],new);self.assertEqual(row['state'],'active')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review').fetchone()[0],0)

    def test_expired_token_cannot_submit_even_before_takeover(self):
        token=self.claim();self.advance(600)
        with self.assertRaises(ClaimLost):
            self.repo.submit_proposal(42,fixtures.UID,fingerprint(self.repo.rows(42)),self.evidence,'staff',claim_token=token)

    def test_renewal_protects_long_running_work_and_does_not_resurrect_expired_work(self):
        token=self.claim();self.repo.renew_claim(42,token,600)  # same second is valid
        self.advance(590);self.repo.renew_claim(42,token,600)
        self.advance(20);self.assertIsNone(self.claim('machine-B'))
        self.advance(600)
        with self.assertRaises(ClaimLost):self.repo.renew_claim(42,token,600)

    def test_successful_submission_is_fenced_and_blocks_future_claims(self):
        token=self.claim()
        result=self.repo.submit_proposal(42,fixtures.UID,fingerprint(self.repo.rows(42)),self.evidence,'staff',claim_token=token)
        self.assertTrue(result['created'])
        self.repo.finish_claim(42,token,0,'submitted')
        self.assertIsNone(self.claim('machine-B'))
        self.assertIsNone(self.claim('machine-B',allow_rejected=True))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_match_review_history').fetchone()[0],1)

    def test_retry_cooldown_and_interrupt_release(self):
        token=self.claim();self.repo.finish_claim(42,token,3600,'no_matches')
        self.assertIsNone(self.claim('machine-B'))
        self.advance(3600);token=self.claim('machine-B');self.assertTrue(token)
        self.repo.finish_claim(42,token,0,'interrupted')
        self.assertTrue(self.claim('machine-C'))

    def test_review_created_between_queue_read_and_claim_is_skipped(self):
        self.assertEqual(self.repo.ids(),[42]);self.submit()
        self.assertIsNone(self.claim())
        self.reject()
        self.assertIsNone(self.claim())
        self.assertTrue(self.claim(allow_rejected=True))

    def test_claim_queue_exclusion_for_dated_csv_and_undated(self):
        self.claim()
        self.assertEqual(list(self.repo.iter_ids(exclude_claimed=True)),[])
        self.assertEqual(list(self.repo.iter_ids(candidate_ids=[42],exclude_claimed=True)),[])
        self.db.execute('UPDATE seek_scrap SET date_updated=NULL')
        self.assertEqual(list(self.repo.iter_ids(exclude_claimed=True)),[])
        self.assertEqual(self.repo.ids(),[42])  # unreserved preview remains read-only
        self.advance(600)
        self.assertEqual(list(self.repo.iter_ids(exclude_claimed=True)),[42])

    def test_two_connections_racing_acquire_only_one_claim(self):
        # Execute real SQL on two SQLite connections; emulate GET_LOCK with a shared mutex.
        with tempfile.TemporaryDirectory() as folder:
            lock=threading.Lock();path=str(Path(folder)/'claims.sqlite')
            first=fixtures.Connection(path,named_lock=lock)
            self.db.backup(first.db)
            self.repo.close();self.repo.connection=first;self.db=first.db
            second=Repository.__new__(Repository)
            second.database=self.repo.database;second.target_table=self.repo.target_table
            second.connection=fixtures.Connection(path,named_lock=lock)
            second.verify_connection=Mock();second.preflight=Mock()
            start=threading.Barrier(2)
            def acquire(repo, worker):
                start.wait(timeout=3)
                return repo.claim_candidate(42,worker,'staff',600)
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a=pool.submit(acquire,self.repo,'A');b=pool.submit(acquire,second,'B')
                    tokens=[a.result(timeout=5),b.result(timeout=5)]
                self.assertEqual(sum(t is not None for t in tokens),1)
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM seek_uuid_work_claim').fetchone()[0],1)
            finally:second.close()


class LeaseTest(unittest.TestCase):
    def lease(self):
        lease=ClaimLease.__new__(ClaimLease)
        self.now=100.0;lease.clock=lambda:self.now
        lease.repo=Mock();lease.numeric_id=42;lease.token='token'
        lease.settings=dict(lease_seconds=600,heartbeat_seconds=60,retry_seconds=3600)
        lease.deadline=690;lease.stopped=threading.Event();lease.lost=threading.Event()
        return lease

    def test_heartbeat_extends_deadline_during_login_or_break(self):
        lease=self.lease();self.now=650;lease._renew_once()
        self.now=700;lease.check()
        lease.repo.renew_claim.assert_called_once_with(42,'token',600)

    def test_machine_sleep_past_deadline_fails_closed_without_renewal(self):
        lease=self.lease();self.now=691
        with self.assertRaises(ClaimLost):lease._renew_once()
        lease.repo.renew_claim.assert_not_called()

    def test_slow_response_does_not_resurrect_local_lease(self):
        lease=self.lease();self.now=650
        lease.repo.renew_claim.side_effect=lambda *a:setattr(self,'now',700)
        with self.assertRaises(ClaimLost):lease._renew_once()

    def test_heartbeat_database_failure_sets_lost_and_closes_its_connection(self):
        lease=self.lease();lease.stopped=Mock();lease.stopped.wait.return_value=False
        lease.stopped.is_set.return_value=False
        lease.repo.renew_claim.side_effect=OSError('private DB error')
        lease._heartbeat()
        self.assertTrue(lease.lost.is_set());lease.repo.close.assert_called_once()
        with self.assertRaises(ClaimLost):lease.check()

    def test_guard_checks_after_delay_before_browser_navigation(self):
        browser=SeekBrowser.__new__(SeekBrowser);browser.delay_seconds=10;browser.driver=Mock()
        browser.claim_guard=Mock(side_effect=[None,ClaimLost('expired')])
        with patch('seek_browser.time.sleep'),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(ClaimLost):
            browser.numeric_profile(42)
        browser.driver.get.assert_not_called()

    def test_real_heartbeat_thread_uses_its_own_connection_and_closes(self):
        import time
        renewed=threading.Event();repo=Mock()
        repo.renew_claim.side_effect=lambda *args:renewed.set()
        lease=ClaimLease(repo,42,'token',dict(lease_seconds=600,heartbeat_seconds=0.01),time.monotonic())
        try:
            self.assertTrue(renewed.wait(timeout=2))
            lease.check()
        finally:
            self.assertTrue(lease.close())
        repo.close.assert_called_once()

    def test_claim_settings_validate_and_worker_ids_are_unique(self):
        self.assertEqual(claim_settings({})['lease_seconds'],600)
        for options in ({'lease_seconds':True},{'lease_seconds':0},{'heartbeat_seconds':300},{'retry_seconds':-1}):
            with self.subTest(options=options),self.assertRaises(ValueError):claim_settings({'claims':options})
        self.assertNotEqual(worker_identity(),worker_identity())


class ClaimWorkflowTest(unittest.TestCase):
    def repo(self):
        repo=Mock();repo.database='test';repo.target_table='seek_scrap_detail'
        repo.scrap_idle_settings.return_value=dict(idle_less_than=0,idle_less_than2=0,idle_more_than=0,idle_more_than2=0)
        repo.rows.return_value=[]
        return repo

    def args(self, apply=True, id=None):
        return argparse.Namespace(apply=apply,auto_save=True,id=id,csv=None,limit=2,no_ollama=True,uuid=None)

    def test_claim_contention_does_not_consume_limit_and_no_unclaimed_scrape(self):
        repo=self.repo();repo.iter_ids.return_value=[41,42,43,44]
        leases=[None,Mock(),Mock()];repo.start_claim_lease.side_effect=leases
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser') as browser,contextlib.redirect_stdout(io.StringIO()):
            run(self.args(),{'created_by':'staff','report_dir':folder},repo)
        self.assertEqual([c.args[0] for c in repo.rows.call_args_list],[42,43])
        self.assertEqual(repo.start_claim_lease.call_count,3)
        repo.iter_ids.assert_called_once_with(limit=None,candidate_ids=None,exclude_claimed=True)
        browser.assert_not_called()
        for lease in leases[1:]:lease.close.assert_called_once()
        self.assertEqual(repo.finish_claim.call_count,2)

    def test_direct_id_already_claimed_does_not_open_chrome(self):
        repo=self.repo();repo.start_claim_lease.return_value=None
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser') as browser,contextlib.redirect_stdout(io.StringIO()):
            run(self.args(id=42),{'created_by':'staff','report_dir':folder},repo)
        browser.assert_not_called();repo.rows.assert_not_called();repo.submit_proposal.assert_not_called()

    def test_lost_claim_stops_run_before_any_profile_or_submission(self):
        repo=self.repo();repo.iter_ids.return_value=[42,43]
        lease=repo.start_claim_lease.return_value;lease.check.side_effect=ClaimLost('lost')
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser') as browser,contextlib.redirect_stdout(io.StringIO()),self.assertRaises(ClaimLost):
            run(self.args(),{'created_by':'staff','report_dir':folder},repo)
        browser.assert_not_called();repo.submit_proposal.assert_not_called();lease.close.assert_called_once()
        self.assertEqual(repo.start_claim_lease.call_count,1)

    def test_dry_run_does_not_claim_or_update_coordination_tables(self):
        repo=self.repo();repo.iter_ids.return_value=[42,43]
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
            run(self.args(apply=False),{'created_by':'staff','report_dir':folder},repo)
        repo.start_claim_lease.assert_not_called();repo.finish_claim.assert_not_called();repo.submit_proposal.assert_not_called()

    def execute_browser_failure(self, failure, at_login=False):
        from profiles import extract_candidate_profile
        repo=self.repo();profile=extract_candidate_profile(fixtures.FIXTURE)
        repo.rows.return_value=[{'id':42,'id_pk':100,'uuid':None,'name':None,'file':None,'scrap_date':None}]
        browser=Mock()
        browser.numeric_profile.return_value=(profile,{'mode':'live_numeric'})
        browser.search.return_value=({fixtures.UID:'https://au.employer.seek.com/talentsearch/profiles/'+fixtures.UID},True,'')
        if at_login:browser.login.side_effect=failure
        else:browser.profile.side_effect=failure
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser',return_value=browser),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(type(failure)):
            run(self.args(id=42),{'created_by':'staff','report_dir':folder},repo)
        browser.close.assert_called_once();repo.submit_proposal.assert_not_called()
        repo.start_claim_lease.return_value.close.assert_called_once()
        return repo

    def test_interrupt_during_login_releases_claim_immediately(self):
        repo=self.execute_browser_failure(KeyboardInterrupt(),at_login=True)
        self.assertEqual(repo.finish_claim.call_args.args[2:],(0,'interrupted'))

    def test_claim_loss_inside_uuid_capture_is_not_swallowed(self):
        repo=self.execute_browser_failure(ClaimLost('lost'))
        self.assertEqual(repo.finish_claim.call_args.args[3],'stopped_claim_lost')

    def test_available_queue_check_is_read_only(self):
        import json
        from compare import main
        repo=self.repo();repo.server={'hostname':'server','port':3306}
        repo.iter_ids.return_value=[42]
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.json'
            config.write_text(json.dumps({'created_by':'staff','database':{'host':'server','port':3306}}))
            with patch('sys.argv',['compare.py','--config',str(config),'check-queue','--limit','5','--available-only']),patch('compare.Repository',return_value=repo),patch.dict('os.environ',{'SEEK_DB_PASSWORD':'test'}),patch('seek_browser.SeekBrowser') as browser,contextlib.redirect_stdout(io.StringIO()):
                main()
        repo.iter_ids.assert_called_once_with(limit=5,exclude_claimed=True)
        repo.start_claim_lease.assert_not_called();repo.finish_claim.assert_not_called()
        repo.initialize.assert_not_called();repo.submit_proposal.assert_not_called();browser.assert_not_called()
