"""Shared database-clock schedule. Day 1 is Monday and day 7 is Sunday."""
from datetime import datetime, timedelta
import re
import time


class ScheduleError(ValueError):
    """Missing/unreadable scheduling must never enable scraping."""


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}(:\d{2})?', value.strip()):
        raise ScheduleError('seek_run_times times must be HH:MM or HH:MM:SS.')
    parts = [int(x) for x in value.strip().split(':')]
    h, m, s = (*parts, 0) if len(parts) == 2 else parts
    if h > 23 or m > 59 or s > 59:
        raise ScheduleError('seek_run_times contains an out-of-range time.')
    return h*3600 + m*60 + s


def validate_rows(rows):
    if not isinstance(rows, (list, tuple)):
        raise ScheduleError('Cannot read rows from seek_run_times.')
    windows = []
    for row in rows:
        day = row.get('day')
        if type(day) is not int or not 1 <= day <= 7:
            raise ScheduleError('seek_run_times.day must be 1 (Monday) through 7 (Sunday).')
        start, end = row.get('time_from'), row.get('time_to')
        if start in (None, '') and end in (None, ''):
            continue  # Explicit closed day.
        start, end = parse_time(start), parse_time(end)
        if start != end:  # Equal times mean closed, never an implicit 24-hour window.
            windows.append((day, start, end))
    return windows


def allowed_at(now, windows):
    seconds = now.hour*3600 + now.minute*60 + now.second + now.microsecond/1e6
    day, previous = now.isoweekday(), (now.isoweekday()-2) % 7 + 1
    for weekday, start, end in windows:
        if start < end and weekday == day and start <= seconds < end:
            return True
        if start > end and ((weekday == day and seconds >= start) or
                            (weekday == previous and seconds < end)):
            return True
    return False


class DailySchedule:
    refresh_seconds = 30

    def __init__(self, load, *, clock=time.monotonic, sleep=time.sleep, emit=print):
        self.load, self.clock, self.sleep, self.emit = load, clock, sleep, emit
        self.refresh()
        self.emit('Daily schedule enabled: seek_run_times; database server time; '
                  '1=Monday, 7=Sunday. Start inclusive, end exclusive.')

    def refresh(self):
        started = self.clock()
        try:
            snapshot = self.load()
            now = snapshot['server_now']
            if not isinstance(now, datetime):
                raise ScheduleError('Cannot read the database server time for seek_run_times.')
            windows = validate_rows(snapshot['rows'])
        except ScheduleError:
            raise
        except Exception as ex:
            raise ScheduleError('Cannot read seek_run_times ('+type(ex).__name__+
                                '). Check the configured database and SELECT permission.') from None
        self.server_now, self.windows, self.loaded_at = now, windows, started

    def wait_until_open(self, claim_check=None):
        announced = False
        next_notice = 0
        while True:
            if claim_check:
                claim_check()
            elapsed = self.clock()-self.loaded_at
            if elapsed < 0 or elapsed >= self.refresh_seconds:
                self.refresh()
                elapsed = self.clock()-self.loaded_at
            now = self.server_now + timedelta(seconds=max(0, elapsed))
            if allowed_at(now, self.windows):
                if claim_check:
                    claim_check()
                if announced:
                    self.emit('Daily schedule open; resuming at database time '+str(now.replace(microsecond=0)))
                return
            if self.clock() >= next_notice:
                self.emit('Outside seek_run_times; paused at database time '+str(now.replace(microsecond=0))+
                          '. Rechecking schedule every 30 seconds. Ctrl+C to stop.')
                next_notice = self.clock()+60
            announced = True
            self.sleep(1)  # Keep Ctrl+C and claim checks responsive, including overnight.
