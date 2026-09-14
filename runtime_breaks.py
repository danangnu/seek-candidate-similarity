"""Per-run, per-reviewer breaks using the configured SEEK settings row."""
import random
import time


class BreakSettingsError(ValueError):
    """Stop the run rather than silently bypass missing/invalid database pacing."""


def validate_settings(row):
    if not isinstance(row, dict):
        raise BreakSettingsError('The selected seek_scrap_settings row is missing.')
    result = {}
    for key in ('idle_less_than', 'idle_less_than2', 'idle_more_than', 'idle_more_than2'):
        value = row.get(key)
        if type(value) is not int or value < 0:
            raise BreakSettingsError('seek_scrap_settings.'+key+' must be a nonnegative integer.')
        result[key] = value
    for low, high in (('idle_less_than', 'idle_less_than2'), ('idle_more_than', 'idle_more_than2')):
        if result[low] > result[high]:
            raise BreakSettingsError('seek_scrap_settings.'+low+' must not exceed '+high+'.')
    return result


class RuntimeBreaks:
    def __init__(self, reviewer, load_settings, settings_id=1, *, clock=None, sleep=None,
                 randint=None, emit=print, batch_min=5, batch_max=10, batch_randint=None):
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise BreakSettingsError('A reviewer is required to identify the running user.')
        if type(settings_id) is not int or settings_id < 1:
            raise BreakSettingsError('breaks.settings_id must be a positive integer.')
        if type(batch_min) is not int or type(batch_max) is not int or not 1 <= batch_min <= batch_max:
            raise BreakSettingsError('Candidate batch bounds must be positive integers with min <= max.')
        self.batch_min, self.batch_max = batch_min, batch_max
        self.batch_randint = batch_randint or random.randint
        self.batch_target = None
        self.candidates_since_break = 0
        self.reviewer = reviewer
        self.settings_id = settings_id
        self.load_settings = load_settings
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.randint = randint or random.randint
        self.emit = emit
        self.started_at = None
        self.attempted = False
        self.settings = self.refresh()  # Validate before opening Chrome.

    def refresh(self):
        try:
            self.settings = validate_settings(self.load_settings(self.settings_id))
        except BreakSettingsError:
            raise
        except Exception as ex:
            # Never print database error text (it could contain connection details).
            raise BreakSettingsError('Cannot read seek_scrap_settings ('+type(ex).__name__+
                '). Check the configured database and SELECT permission. setup_local_scrap_settings.sql is only for the local test copy.') from None
        return self.settings

    def start(self):
        # Start only after manual sign-in. Repeated calls do not reset the clock.
        if self.started_at is None:
            self.started_at = self.clock()
            self.batch_target = self.batch_randint(self.batch_min, self.batch_max)
            self.emit(f'Runtime breaks started for {self.reviewer}; seek_scrap_settings id={self.settings_id}. '
                      f'Under 1 hour: {self.settings["idle_less_than"]}–{self.settings["idle_less_than2"]} seconds; '
                      f'at/after 1 hour: {self.settings["idle_more_than"]}–{self.settings["idle_more_than2"]} minutes. '
                      f'Break after {self.batch_target} numeric candidates; choose a fresh {self.batch_min}–{self.batch_max} count after each break.')

    def before_candidate(self):
        if self.started_at is None:
            raise BreakSettingsError('Runtime break timer has not started after sign-in.')
        settings = self.refresh()  # Apply database changes at the next boundary.
        elapsed = max(0.0, self.clock() - self.started_at)
        record = {'reviewer': self.reviewer, 'settings_id': self.settings_id,
                  'elapsed_run_seconds': round(elapsed, 3), 'settings': dict(settings)}
        record.update(batch_target=self.batch_target, candidates_before_this=self.candidates_since_break)
        if self.candidates_since_break < self.batch_target:
            first = not self.attempted
            self.attempted = True
            self.candidates_since_break += 1
            self.emit(f'Candidate batch: {self.candidates_since_break}/{self.batch_target}; '
                      'no runtime break before this numeric candidate.')
            return dict(record, phase='first_candidate' if first else 'within_batch', duration_seconds=0,
                        candidate_in_batch=self.candidates_since_break)

        longer = elapsed >= 3600
        low, high = ('idle_more_than', 'idle_more_than2') if longer else ('idle_less_than', 'idle_less_than2')
        amount = self.randint(settings[low], settings[high])
        seconds = amount * (60 if longer else 1)
        record.update(phase='at_or_after_one_hour' if longer else 'under_one_hour',
                      duration_seconds=seconds)
        self.emit(f'Runtime break for {self.reviewer}: {amount} {"minutes" if longer else "seconds"}; '
                  f'run elapsed {elapsed/60:.1f} minutes. Ctrl+C to stop.')
        deadline = self.clock() + seconds
        next_update = self.clock() + 60
        while self.clock() < deadline:
            self.sleep(min(1.0, max(0.0, deadline-self.clock())))
            now = self.clock()
            if now >= next_update and now < deadline:
                self.emit(f'Runtime break for {self.reviewer}: {int(deadline-now+0.999)} seconds remaining.')
                next_update = now + 60
        # Reset only after the break completes. The global run timer keeps running.
        completed_batch = self.batch_target
        self.batch_target = self.batch_randint(self.batch_min, self.batch_max)
        self.candidates_since_break = 1  # The candidate admitted after this break.
        record.update(completed_batch_size=completed_batch, next_batch_target=self.batch_target,
                      candidate_in_batch=1)
        self.emit(f'Runtime break complete; next break after {self.batch_target} numeric candidates. '
                  f'Candidate batch: 1/{self.batch_target}.')
        return record
