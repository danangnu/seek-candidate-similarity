import contextlib
from datetime import datetime, timedelta
import io
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
from daily_schedule import DailySchedule, ScheduleError, allowed_at, validate_rows
from repository import Repository
from seek_browser import SeekBrowser
from work_claims import ClaimLost
import test_work_claims as claim_fixtures
from compare import run


def row(day=1, start='07:00', end='22:00'):
    return {'day': day, 'time_from': start, 'time_to': end}


class FakeTime:
    def __init__(self): self.elapsed=0
    def clock(self): return self.elapsed
    def sleep(self, seconds): self.elapsed+=seconds


class ScheduleTest(unittest.TestCase):
    def test_uploaded_week_and_exact_boundaries(self):
        times=[('07:01','22:00'),('07:00','22:18'),('07:06','21:15'),
               ('07:02','22:12'),('07:00','20:11'),('09:03','19:11'),('09:00','16:58')]
        windows=validate_rows([row(i+1,*v) for i,v in enumerate(times)])
        monday=datetime(2026,9,7)
        for i,(start,end) in enumerate(times):
            date=(monday+timedelta(days=i)).date()
            first=datetime.combine(date,datetime.strptime(start,'%H:%M').time())
            last=datetime.combine(date,datetime.strptime(end,'%H:%M').time())
            with self.subTest(day=i+1):
                self.assertFalse(allowed_at(first-timedelta(microseconds=1),windows))
                self.assertTrue(allowed_at(first,windows))
                self.assertTrue(allowed_at(last-timedelta(microseconds=1),windows))
                self.assertFalse(allowed_at(last,windows))

    def test_sunday_overnight_into_monday(self):
        windows=validate_rows([row(7,'22:00','02:00')])
        self.assertTrue(allowed_at(datetime(2026,9,7,1),windows))
        self.assertFalse(allowed_at(datetime(2026,9,7,2),windows))
        self.assertFalse(allowed_at(datetime(2026,9,8,1),windows))

    def test_missing_blank_and_equal_days_closed(self):
        for rows in ([],[row(1,None,None)],[row(1,'','')],[row(1,'07:00','07:00')],[row(2)]):
            self.assertFalse(allowed_at(datetime(2026,9,7,12),validate_rows(rows)))

    def test_multiple_windows_allow_lunch_gap(self):
        windows=validate_rows([row(1,'07:00','12:00'),row(1,'13:00','17:00')])
        self.assertFalse(allowed_at(datetime(2026,9,7,12,30),windows))
        self.assertTrue(allowed_at(datetime(2026,9,7,13),windows))

    def test_invalid_values_fail_closed(self):
        for item in [row(0),row(8),row('1'),row(True),row(1,'25:00'),
                     row(1,'07:61'),row(1,'07:00:60'),row(1,None,'12:00'),row(1,'7am')]:
            with self.subTest(item=item),self.assertRaises(ScheduleError):validate_rows([item])

    def schedule(self, start, rows):
        clock=FakeTime()
        load=Mock(side_effect=lambda:{'server_now':start+timedelta(seconds=clock.elapsed),'rows':rows})
        schedule=DailySchedule(load,clock=clock.clock,sleep=clock.sleep,emit=Mock())
        return schedule,clock,load

    def test_wait_before_open_uses_database_clock_and_checks_claim(self):
        schedule,clock,load=self.schedule(datetime(2026,9,7,6,59,58),[row()])
        claim=Mock();schedule.wait_until_open(claim)
        self.assertEqual(clock.elapsed,2);self.assertGreaterEqual(claim.call_count,3)
        self.assertEqual(load.call_count,1)

    def test_cached_schedule_stops_at_end_and_ctrl_c_works(self):
        schedule,clock,_=self.schedule(datetime(2026,9,7,21,59,59),[row()])
        schedule.wait_until_open();clock.elapsed=1
        schedule.sleep=Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):schedule.wait_until_open()

    def test_database_edits_applied_after_30_seconds(self):
        schedule,clock,load=self.schedule(datetime(2026,9,7,12),[])
        load.side_effect=lambda:{'server_now':datetime(2026,9,7,12,0,30),'rows':[row()]}
        schedule.wait_until_open()
        self.assertEqual(clock.elapsed,30);self.assertEqual(load.call_count,2)

    def test_refresh_failure_stops_without_raw_database_details(self):
        schedule,clock,load=self.schedule(datetime(2026,9,7,12),[row()])
        clock.elapsed=30;load.side_effect=RuntimeError('SECRET')
        with self.assertRaises(ScheduleError) as error:schedule.wait_until_open()
        self.assertNotIn('SECRET',str(error.exception))

    def test_claim_loss_during_closed_period_propagates(self):
        schedule,clock,_=self.schedule(datetime(2026,9,7,6),[row()])
        claim=Mock(side_effect=[None,ClaimLost('lost')])
        with self.assertRaises(ClaimLost):schedule.wait_until_open(claim)
        self.assertEqual(clock.elapsed,1)

    def test_browser_delay_crossing_end_never_navigates(self):
        schedule,clock,_=self.schedule(datetime(2026,9,7,21,59,59),[row()])
        b=SeekBrowser.__new__(SeekBrowser);b.delay_seconds=2;b.driver=Mock()
        b.schedule_guard=schedule.wait_until_open
        schedule.sleep=Mock(side_effect=KeyboardInterrupt)
        with patch('seek_browser.time.sleep',side_effect=clock.sleep),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                b.pause('next profile');b.navigate('https://example.com','test')
        b.driver.get.assert_not_called()

    def test_repository_reads_only_selected_database_schedule(self):
        repo=Repository.__new__(Repository);repo.connection=MagicMock();repo.verify_connection=Mock()
        cursor=repo.connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value={'server_now':datetime(2026,9,7,12)}
        cursor.fetchall.return_value=[row()]
        result=repo.run_schedule()
        self.assertEqual(result['rows'],[row()])
        queries=[c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(queries,['SELECT NOW() AS server_now','SELECT day, time_from, time_to FROM seek_run_times ORDER BY day, id'])
        repo.connection.commit.assert_not_called()

    def test_run_missing_schedule_stops_before_claim_or_browser(self):
        fixture=claim_fixtures.ClaimWorkflowTest();repo=fixture.repo();repo.iter_ids.return_value=[42]
        repo.run_schedule.side_effect=RuntimeError('missing table')
        with tempfile.TemporaryDirectory() as folder,patch('seek_browser.SeekBrowser') as browser,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ScheduleError):run(fixture.args(),{'created_by':'staff','report_dir':folder},repo)
        repo.start_claim_lease.assert_not_called();browser.assert_not_called()

    def test_run_schedule_error_during_profile_stops_and_releases_claim(self):
        fixture=claim_fixtures.ClaimWorkflowTest()
        repo=fixture.execute_browser_failure(ScheduleError('invalid schedule'))
        self.assertEqual(repo.finish_claim.call_args.args[3],'stopped_schedule_error')
        repo.submit_proposal.assert_not_called()
