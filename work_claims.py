"""Renewable candidate claims; each heartbeat owns a separate database connection."""
import os
import socket
import threading
import time
from uuid import uuid4


class ClaimLost(RuntimeError):
    """Stop browser work and prevent submission after ownership becomes uncertain."""


def validate_duration(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum} seconds')
    return value


def claim_settings(config):
    config = config.get('claims', {})
    if not isinstance(config, dict):
        raise ValueError('claims must be an object')
    lease = validate_duration(config.get('lease_seconds', 600), 'claims.lease_seconds', 120, 86400)
    heartbeat = validate_duration(config.get('heartbeat_seconds', 60), 'claims.heartbeat_seconds', 5, 3600)
    retry = validate_duration(config.get('retry_seconds', 3600), 'claims.retry_seconds', 0, 604800)
    if heartbeat > lease // 3:
        raise ValueError('claims.heartbeat_seconds must be at most one third of lease_seconds')
    return dict(lease_seconds=lease, heartbeat_seconds=heartbeat, retry_seconds=retry)


def worker_identity():
    # Every process/run is distinct even when staff ID or hostname is shared.
    return f'{socket.gethostname()[:80]}:{os.getpid()}:{uuid4().hex}'


class ClaimLease:
    def __init__(self, repo, numeric_id, token, settings, started, *, clock=None):
        self.repo = repo
        self.numeric_id = numeric_id
        self.token = token
        self.settings = settings
        self.clock = clock or time.monotonic
        # Use request-start time, not response time, as a conservative local bound.
        self.deadline = started + settings['lease_seconds'] - 10
        self.stopped = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, name='seek-claim-heartbeat', daemon=True)
        self.thread.start()

    def check(self):
        if self.lost.is_set() or self.stopped.is_set() or self.clock() >= self.deadline:
            self.lost.set()
            raise ClaimLost('Candidate claim could not be maintained. Run stopped; retry after checking the database connection.')

    def _renew_once(self):
        self.check()
        started = self.clock()
        self.repo.renew_claim(self.numeric_id, self.token, self.settings['lease_seconds'])
        # Do not resurrect an expired local lease after a slow DB response or machine sleep.
        self.check()
        self.deadline = started + self.settings['lease_seconds'] - 10

    def _heartbeat(self):
        try:
            while not self.stopped.wait(self.settings['heartbeat_seconds']):
                self._renew_once()
        except Exception:
            # Never log raw DB exceptions/credentials. Foreground checks stop the run.
            self.lost.set()
        finally:
            self.repo.close()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=30)
        # If IO has not returned, do not share/close its connection from this thread.
        # The daemon will close its own connection and the DB lease remains bounded.
        return not self.thread.is_alive()
