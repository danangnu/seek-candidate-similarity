import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_breaks import RuntimeBreaks, BreakSettingsError, validate_settings
from repository import Repository

SETTINGS = dict(idle_less_than=30, idle_less_than2=300, idle_more_than=5, idle_more_than2=25)


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []
    def now(self):
        return self.value
    def sleep(self, amount):
        self.sleeps.append(amount)
        self.value += amount


class RuntimeBreakTest(unittest.TestCase):
    def make(self, reviewer='operator-a', select_upper=False):
        clock = FakeClock()
        load = Mock(return_value=dict(SETTINGS))
        choose = Mock(side_effect=lambda low, high: high if select_upper else low)
        logs = []
        timer = RuntimeBreaks(reviewer, load, clock=clock.now, sleep=clock.sleep,
                              randint=choose, emit=logs.append)
        return timer, clock, load, choose, logs

    def test_timer_starts_after_login_and_first_candidate_has_no_break(self):
        timer,clock,load,choose,logs=self.make()
        clock.value=5000 # manual login time is not run time
        timer.start()
        record=timer.before_candidate()
        self.assertEqual(record['phase'],'first_candidate')
        self.assertEqual(record['elapsed_run_seconds'],0)
        choose.assert_not_called()
        self.assertEqual(clock.sleeps,[])

    def test_under_hour_uses_seconds_and_both_inclusive_endpoints(self):
        for upper,expected in [(False,30),(True,300)]:
            timer,clock,load,choose,_=self.make(select_upper=upper)
            timer.start();timer.before_candidate();clock.value=3599
            record=timer.before_candidate()
            choose.assert_called_once_with(30,300)
            self.assertEqual(record['duration_seconds'],expected)
            self.assertEqual(record['phase'],'under_one_hour')
            self.assertEqual(sum(clock.sleeps),expected)

    def test_exactly_one_hour_and_later_use_minutes(self):
        for elapsed in (3600,3601,7200):
            timer,clock,load,choose,_=self.make(select_upper=True)
            timer.start();timer.before_candidate();clock.value=elapsed
            record=timer.before_candidate()
            choose.assert_called_once_with(5,25)
            self.assertEqual(record['duration_seconds'],1500)
            self.assertEqual(sum(clock.sleeps),1500)
            self.assertLessEqual(max(clock.sleeps),1)

    def test_long_break_does_not_reset_timer_and_updates_progress(self):
        timer,clock,load,choose,logs=self.make()
        timer.start();timer.before_candidate();clock.value=3600
        timer.before_candidate()
        self.assertTrue(any('remaining' in line for line in logs))
        record=timer.before_candidate()
        self.assertEqual(record['phase'],'at_or_after_one_hour')
        self.assertEqual(record['elapsed_run_seconds'],3900)
        timer.start()
        self.assertEqual(timer.started_at,0)

    def test_short_break_counts_towards_run_duration(self):
        timer,clock,load,choose,_=self.make()
        timer.start();timer.before_candidate();clock.value=3599
        timer.before_candidate()
        self.assertEqual(timer.before_candidate()['phase'],'at_or_after_one_hour')

    def test_refresh_reads_changes_at_next_boundary(self):
        timer,clock,load,choose,_=self.make()
        timer.start();timer.before_candidate()
        load.return_value.update(idle_less_than=45,idle_less_than2=45)
        self.assertEqual(timer.before_candidate()['duration_seconds'],45)
        choose.assert_called_once_with(45,45)

    def test_zero_interval_does_not_sleep(self):
        timer,clock,load,choose,_=self.make()
        load.return_value={key:0 for key in SETTINGS}
        timer.start();timer.before_candidate()
        self.assertEqual(timer.before_candidate()['duration_seconds'],0)
        self.assertEqual(clock.sleeps,[])

    def test_each_running_user_has_independent_timer(self):
        a,ca,*_=self.make('operator-a');b,cb,*_=self.make('operator-b')
        a.start();a.before_candidate();ca.value=3700
        b.start();b.before_candidate();cb.value=10
        self.assertEqual(a.before_candidate()['phase'],'at_or_after_one_hour')
        record=b.before_candidate()
        self.assertEqual(record['phase'],'under_one_hour')
        self.assertEqual(record['reviewer'],'operator-b')

    def test_invalid_missing_or_reversed_settings_stop(self):
        bad=[None,{},dict(SETTINGS,idle_less_than=-1),dict(SETTINGS,idle_less_than=True),
             dict(SETTINGS,idle_more_than='5'),dict(SETTINGS,idle_more_than=None),
             dict(SETTINGS,idle_less_than=301),dict(SETTINGS,idle_more_than=26)]
        for row in bad:
            with self.subTest(row=row),self.assertRaises(BreakSettingsError):validate_settings(row)

    def test_database_error_has_no_raw_connection_details(self):
        with self.assertRaises(BreakSettingsError) as caught:
            RuntimeBreaks('operator',Mock(side_effect=RuntimeError('password=SECRET')))
        self.assertNotIn('SECRET',str(caught.exception))
        self.assertIn('setup_local_scrap_settings.sql',str(caught.exception))

    def test_keyboard_interrupt_stops_break(self):
        timer,clock,load,choose,_=self.make()
        timer.start();timer.before_candidate()
        timer.sleep=Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):timer.before_candidate()

    def test_repository_reads_only_four_settings_from_local_connection(self):
        repo=Repository.__new__(Repository)
        repo.verify_connection=Mock()
        repo.connection=Mock()
        cur=Mock();cur.fetchone.return_value=dict(SETTINGS)
        context=Mock();context.__enter__=Mock(return_value=cur);context.__exit__=Mock(return_value=False)
        repo.connection.cursor.return_value=context
        self.assertEqual(repo.scrap_idle_settings(1),SETTINGS)
        repo.connection.ping.assert_called_once_with(reconnect=True)
        statement,params=cur.execute.call_args.args
        self.assertEqual(params,(1,))
        self.assertNotIn('updated_by',statement)
        self.assertNotIn('trackitlive.',statement)
        self.assertNotIn('email',statement)
        cur.fetchone.return_value=None
        with self.assertRaises(BreakSettingsError):repo.scrap_idle_settings(1)

    def test_run_stops_when_settings_disappear_without_attempting_second_profile(self):
        from test_backfill import FIXTURE, UID
        from profiles import extract_candidate_profile
        from compare import run
        import argparse,json,tempfile
        repo=Mock();repo.database='seek_uuid_test_unit';repo.target_table='seek_scrap_detail'
        repo.ids.return_value=[42,43]
        repo.rows.return_value=[dict(id_pk=1,id=42,uuid=None,name=None,file=None,scrap_date=None)]
        zero={k:0 for k in SETTINGS}
        # Init, first candidate, post-break reconnect, then next boundary invalid.
        repo.scrap_idle_settings.side_effect=[zero,zero,zero,None]
        profile=extract_candidate_profile(FIXTURE)
        browser=Mock();browser.numeric_profile.return_value=(profile,{'mode':'live_numeric'})
        browser.search.return_value=({UID:'https://au.employer.seek.com/talentsearch/profiles/'+UID},True,'')
        browser.profile.return_value=profile
        args=argparse.Namespace(apply=False,auto_save=True,id=None,csv=None,limit=2,no_ollama=True,uuid=None)
        with tempfile.TemporaryDirectory() as folder:
            with patch('seek_browser.SeekBrowser',return_value=browser),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(BreakSettingsError):
                    run(args,{'report_dir':folder,'reviewer':'operator'},repo)
            reports=list(Path(folder).rglob('43.json'))
            self.assertEqual(json.loads(reports[0].read_text())['status'],'stopped_invalid_break_settings')
        browser.numeric_profile.assert_called_once_with(42)
        browser.close.assert_called_once()
        repo.apply.assert_not_called()


if __name__=='__main__':unittest.main()
