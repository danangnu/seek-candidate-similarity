# Daily SEEK schedule

Every `run` now requires the configured database's `seek_run_times` table,
including dry runs and direct numeric/UUID comparisons. The app only reads this
table. Day 1 is Monday and day 7 is Sunday. All rows are used; `id` is not a
weekday or staff identifier. Multiple windows for a day are supported.

## Time basis and supplied schedule

The shared clock is read with `SELECT UTC_TIMESTAMP()` from MariaDB and converted
in Python to `schedule.timezone`, defaulting to **Australia/Perth**. Schedule
weekday and time boundaries use that business timezone, not the worker laptop
clock or database session timezone. No server timezone settings are changed.
For example, Friday 2026-09-11 04:20 UTC is Friday 12:20 in Perth, inside the
uploaded Friday window of 07:00–20:11.

Add this top-level section to make the business timezone explicit (existing
configs receive the Perth default automatically):

```json
"schedule": {"timezone": "Australia/Perth"}
```

The startup log prints the selected timezone and converted local schedule time.
An invalid or unavailable timezone stops the run. Install the updated
requirements.txt; tzdata supplies timezone data on Windows.

```sql
SELECT UTC_TIMESTAMP() AS utc_clock, NOW() AS database_session_clock;
SELECT day, time_from, time_to FROM seek_run_times ORDER BY day, id;
```

| Day | Weekday | From | To |
| --- | --- | --- | --- |
| 1 | Monday | 07:01 | 22:00 |
| 2 | Tuesday | 07:00 | 22:18 |
| 3 | Wednesday | 07:06 | 21:15 |
| 4 | Thursday | 07:02 | 22:12 |
| 5 | Friday | 07:00 | 20:11 |
| 6 | Saturday | 09:03 | 19:11 |
| 7 | Sunday | 09:00 | 16:58 |

These values are from the uploaded dump; live database edits take precedence.
Starts are inclusive and ends exclusive: at Monday 22:00 no new browser action
starts. HH:MM and HH:MM:SS are accepted. An end earlier than the start runs
overnight into the next day (including Sunday into Monday). Equal start/end
means closed. Missing days and rows with both times blank/NULL are closed.
An empty table pauses the app until a window is added. Invalid days, malformed
times, a missing table, or a database read failure stop the run with ScheduleError.

## Waiting and existing behavior

Before claiming a new candidate or opening Chrome, the app waits for an allowed
window. Browser actions and readiness checks also use the gate, including after
a delay and before navigating a profile or results page. A request already sent
cannot be cancelled at the closing second; local processing, report saving,
review submission and claim maintenance may finish outside the window.

The schedule/clock refresh every 30 seconds, while known window boundaries are
checked using elapsed monotonic time between refreshes. Database edits can take
up to 30 seconds to apply, plus database response time. Waits sleep one second
at a time, check claim ownership, and print progress each minute. Ctrl+C stops.

A mid-candidate pause keeps Chrome open and the existing claim heartbeat running
until the window opens. Other workers cannot take that active candidate. If the
claim is lost, the run stops. After a crash the existing lease expiry applies.
Long pauses may require manual SEEK sign-in again. Runtime-break elapsed time
continues through scheduled pauses; it does not reset at midnight. Existing
random breaks, per-action delays, WA selection, pagination, full Profile-content
matching and human review requirements remain in force.

## Update and check

Copy these four runtime files together to every machine and restart:
- compare.py
- repository.py
- seek_browser.py
- daily_schedule.py (new)

Keep your existing config.remote.json; schedule.timezone defaults to Australia/Perth.
Install the updated requirements.txt for the tzdata dependency.
The database user needs SELECT on seek_run_times. The table already exists in
the supplied remote database. init-db does not create or modify scheduling rows.
For a separate local database, copy just this table's schema/data into that
local database; the uploaded dump contains a USE trackitlive statement, so do
not blindly run it against another intended database.

```powershell
python -m pip install -r requirements.txt
python compare.py --config config.remote.json check-connections --no-ollama
python compare.py --config config.remote.json run --id 7532339 --auto-propose
```

check-connections validates schedule access and data without waiting for an open
window, opening Chrome or writing to the database. check-queue and init-db are
maintenance commands, not scraping, and are not delayed by the schedule.

## Validation

166 automated tests passed. New tests cover all seven uploaded windows, exact
boundaries, overnight Sunday rollover, closed days, malformed data, multiple
windows, database edits, failed refresh, claim loss, a delay crossing closing,
repository reads, and stopping/cleanup on schedule errors. Browser/database
integration tests use mocks and SQLite, not a live MariaDB 10.1.48 or SEEK session.
No live database writes or browser session were performed here.
Timezone tests include the reported Friday UTC/Perth discrepancy, local weekday
rollover from UTC Sunday, closing boundaries, invalid zones, and explicit UTC override.
