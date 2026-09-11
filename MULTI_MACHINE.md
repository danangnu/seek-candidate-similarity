# Run the collector on multiple machines

Update every worker first. Older versions do not reserve candidates before
browsing and must be stopped during rollout. All workers must connect to the
same MariaDB write server, database name (same spelling/case), and target_table.
Separate local databases or multiple independent write servers do not share locks.

## Install

1. Stop old collector processes. Keep existing config files and reports.
2. Copy all Python application modules and review_schema.sql from this package
   onto every worker, including work_claims.py. Dependencies are unchanged.
3. Once per shared database, run:

```powershell
python compare.py --config config.remote.json init-db
```

Setup creates the missing seek_uuid_work_claim table. It does not alter candidate
tables, reset reviews, or clear existing claims. The setup account needs CREATE.
The runtime account needs SELECT/INSERT/UPDATE on seek_uuid_work_claim, in addition
to existing SELECT on candidate/settings tables and SELECT/INSERT on review/history
(the existing review update locks remain). No DELETE or candidate-write grant is
needed for this feature. Have the DBA grant the claim privileges to the existing
account if necessary; do not change the account's host restriction.

4. On each machine, verify and test:

```powershell
python compare.py --config config.remote.json check-connections --no-ollama
python compare.py --config config.remote.json check-queue --limit 5 --available-only
python compare.py --config config.remote.json run --limit 5 --submit --auto-propose
```

Use each operator's own created_by value. Worker IDs are automatically generated
from hostname, process ID and a random run ID; operators need not invent unique
machine names in config. Each worker counts only acquired attempts toward its
limit. A run may finish below its limit if no available candidates remain.

## Defaults and optional configuration

Existing configs work after the schema/application update. Optionally add:

```json
"claims": {
  "lease_seconds": 600,
  "heartbeat_seconds": 60,
  "retry_seconds": 3600
}
```

- Lease: 10 minutes, measured using the database's UTC clock.
- Heartbeat: every minute, with a dedicated connection and bounded IO timeouts.
  Renewal continues during login, request delays, Ollama calls and runtime breaks.
- Retry delay: one hour after an unmatched or failed attempt; completed proposals
  remain excluded by the review table regardless of claim expiry.
- Ctrl+C attempts immediate release. A crash/failed cleanup leaves the claim to
  expire. A stopped run does not itself restart; another active or later run can
  acquire expired work. IDs skipped earlier in a run may wait for a new run.
- Explicit --id uses the same claim protection. It may re-investigate rejected
  proposals after any retry delay; pending/approved/applied people are skipped.
- Without --submit/--apply, no claim is written. Concurrent dry runs may overlap.
- Plain check-queue shows unreserved preview order. --available-only additionally
  excludes current claims and retry delays, without reserving or writing anything.

## Monitor

In the shared database:

```sql
SELECT source_table, seekid_detail, worker_id, claimed_by, state,
       claimed_at, heartbeat_at, expires_at, last_result,
       (expires_at > UTC_TIMESTAMP()) AS reserved_now
FROM seek_uuid_work_claim
ORDER BY heartbeat_at DESC
LIMIT 50;
```

There is one reusable claim row per source/person. state='active' with a future
expiry means a worker currently owns it. state='finished' with a future expiry
means the retry delay is still in effect. An expired row can be reclaimed. This
table is operational state, not an append-only audit. Review history remains in
seek_uuid_match_review_history, and proposed UUIDs remain in seek_uuid_match_review.

## Ownership and failure behavior

A short database advisory lock and transaction serialize acquisition with the
existing submission path. Ownership is rechecked after queue selection. The
advisory lock is not held during browser work. The primary key prevents multiple
claim rows for one source/person. New owners receive fresh random tokens.

Heartbeat updates require the current token and an unexpired active lease;
expired tokens cannot be revived. Final submission checks that token under a row
lock inside the proposal transaction, before any proposal/history insert. A
stale worker cannot submit or clear a newer worker's claim. Existing submission
hash and active-proposal checks are retained.

If renewal fails, or a machine resumes after its local lease deadline, guarded
browser actions stop and the worker cannot submit with the old token. An already
in-flight HTTP request cannot be undone. Lease expiry therefore prevents stale
writes, but cannot promise zero overlapping requests during network partitions
or machine suspension. No live multi-machine MariaDB/SEEK test was performed here.

Implementation uses [MariaDB GET_LOCK](https://mariadb.com/docs/server/reference/sql-functions/secondary-functions/miscellaneous-functions/get_lock)
and [TIMESTAMPADD](https://mariadb.com/docs/server/reference/sql-functions/date-time-functions/timestampadd).
It does not require SKIP LOCKED or newer multi-table UPDATE LIMIT support.

## Acceptance check on your server

Start two updated workers with --limit 5 --submit --auto-propose against the same
database. Compare their 'Claimed numeric SEEK ID' messages and the monitoring
query above. A person already reserved by worker A should be skipped by worker B.
Each worker should collect different available people. Review only the pending
proposals; this collector still performs no approved candidate UUID updates.

Automated validation covers 147 tests, including two SQLite connections racing
under an emulated advisory lock, stale-token rejection, actual heartbeat-thread
lifecycle, interruption, claim-loss propagation and read-only preview behavior.
MariaDB 10.1 SQL parsing, its real named-lock behavior, remote permissions and
production timing require the deployment check above.
