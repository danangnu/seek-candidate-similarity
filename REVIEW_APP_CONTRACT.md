# Contract for the separate review app

The collector implements submission only. This document describes the approval
and application behavior to implement in the separate app; it is not an existing
review service or runnable approval endpoint.

## Tables and ownership

`review_schema.sql` is the authoritative schema, with all proposed reviewer and
history fields. Additional fields `source_table`, `submission_hash` and
`source_fingerprint` identify the source, deduplicate submissions and record source
state. There is deliberately no foreign key from proposals to the legacy candidate
tables and no claim that a proposed UUID is an approved identity mapping.

Python can INSERT pending proposals and submitted history events. It does not
change their status, reviewer assignment or content after creation. A shared
submission hash makes identical retries return the existing review ID, even
when already rejected/approved/applied. Keep snapshot/evidence data immutable.
The initial assignment is captured in the submitted history event.

`created_by` is the collector/operator. `assigned_reviewer` is the intended staff
reviewer and may be NULL. `reviewed_by` is the authenticated user who actually
approves or rejects. Do not copy one into the other automatically. Resolve staff
IDs against your app's existing user directory; no separate password/user table
is introduced by this package.

## Queue and review screen

Use a paged queue filtered by status and assigned reviewer, ordered by
`created_at, review_id`. Fetch large snapshots only when opening a review:

```sql
SELECT review_id, seekid_detail, proposed_uuid, exact_content_match,
       status, assigned_reviewer, created_by, created_at, row_version
FROM seek_uuid_match_review
WHERE status = 'pending'
ORDER BY created_at, review_id
LIMIT 50;
```

For details, parameterize `review_id` and decode the three LONGTEXT JSON fields.
Show original and UUID profiles side by side, their URLs, `profile_content`,
structured fields, and `comparison_evidence.comparison.differing_fields`.
Also show the captured-at time (`created_at` is submission time), source metadata
where available, and the optional `selected.ollama` opinion as advisory only.
Collection spans multiple visits; do not label submission time as an exact capture
time for both profiles. Escape profile text in the UI; never render untrusted
captured text/HTML as application markup.

The evidence JSON includes `source_rows` (original primary IDs/values),
`source_fingerprint`, `source_table`, numeric key, comparison scope, candidate
ranks and runtime-break information. Full snapshots in the table are sufficient;
`report_reference` may point to an inaccessible collector PC and is optional.

Display other proposals using the same numeric ID or UUID before approval.
Do not infer identity from the same name or rank alone. Missing/unavailable live
profiles, partial content or contradictory facts need human resolution.

## Assignment and review transactions

All app sessions should use UTC: `SET time_zone = '+00:00'`.
Use a transaction and `SELECT ... FOR UPDATE` on the proposal. Check that the
expected `row_version` still matches and status is `pending`. Authenticate and
authorize the acting staff user; only an authorized reviewer/administrator can
assign or decide. An unassigned queue does not authorize anonymous approval.

- Assignment: update `assigned_reviewer`, increment `row_version`, insert an
  `assigned` history event with old/new staff ID and acting user, then commit.
- Approval/rejection: require a meaningful reason; set status, `reviewed_by`,
  `reviewed_at`, `review_reason`, and increment `row_version`. Insert a matching
  `approved` or `rejected` history event in the SAME transaction.
- On stale version, changed status or failed history insert: rollback and show
  a conflict. Never silently overwrite another reviewer's decision.

For every history record store `performed_by` from the authenticated session,
not a browser-supplied staff ID, plus old/new status or assignment, row version
and reason. History should be append-only to application users.

On MariaDB 10.2.6+ the setup includes checks for decision metadata in
approved/rejected/applied states and `applied_at` only in the applied state.
On the supplied MariaDB 10.1.48 server these checks are not installed/enforced:
the review app MUST validate all these rules itself. The schema does not implement user
authentication, authorization or the above transitions: the review app must.

## Applying an approved proposal later

Approval alone does not update candidate tables. An authorized application action
must apply the approved mapping. The latest supplied schema identifies the destination as
`seek_scrap_detail.uuid` (VARCHAR(255)), matched using `seekid_detail`.
The integer `seek_scrap.seek_scrap_id` must remain unchanged. Python creates no
new candidate column and performs no approved UUID update.

The apply transaction must:

1. Lock/re-read the approved proposal and verify its version and approval metadata.
2. Lock the exact destination rows selected by the original numeric ID. Verify
   source identity and current destination state against the captured evidence.
   Reject missing, stale or conflicting rows; an existing different UUID must
   not be overwritten. Record a verified already-equal UUID as a no-op if allowed.
3. Enforce numeric-ID/UUID mapping uniqueness with a shared database mapping
   constraint or a common lock across every applying worker. A proposal is not
   a uniqueness reservation: two numeric IDs can propose the same UUID.
4. Write only the approved UUID to the agreed destination, mark the proposal
   `applied`, set `applied_at`, increment `row_version`, and append an `applied`
   history event with affected table/primary keys and BEFORE/AFTER UUID values.
5. Commit all those changes together, or rollback all of them. On failure leave
   it approved for investigation; do not mark it applied or erase its approval.

Application workers must use a consistent lock order. Where sharing the
collector's submission lock is needed, its name is `seek_review:` plus the first
40 hex characters of SHA-256(UTF-8 database name); release it explicitly after
the transaction. Review/apply workers still need their own stated identity
uniqueness strategy; locking one proposal does not lock every matching UUID.

Corrections after application need a separately authorized, audited reversal
workflow checking current values. Preserve the original proposal and its history;
do not DELETE it or reopen it silently. The current schema/actions do not yet
implement reversal. Add that migration and service explicitly when building it.

## Database notes

Snapshots and evidence use LONGTEXT containing JSON objects. Python serializes
strict JSON (non-finite numbers rejected) and validates proposal data before
inserting. MariaDB 10.1.48 has no JSON functions or enforced CHECK constraints;
the separate review app must parse JSON and validate decision/status fields itself.

The setup wraps optional JSON_VALID/status checks in `/*M!100206 ... */`, so they
execute only on MariaDB 10.2.6 and later. These versioned comments are documented
in [MariaDB comment syntax](https://mariadb.com/docs/server/reference/sql-statements/comment-syntax).
No JSON SQL functions are used during collection. If the server is upgraded,
CREATE TABLE IF NOT EXISTS does not retrofit omitted checks into existing tables;
add those in a separately reviewed migration if required.

Primary/unique keys and the history foreign key remain in the 10.1-compatible
DDL. The unique submission hash prevents duplicate proposals; the foreign key
prevents orphaned history. The SQL contains three CREATE TABLE IF NOT EXISTS
statements (two review tables and one operational claim table), selects no database, and never alters existing candidate tables.


## Collector work claims

`seek_uuid_work_claim` coordinates collectors before scraping. Its primary key
is `(source_table, seekid_detail)`. It contains worker ownership, heartbeat/expiry,
and the last processing outcome. It is not the review queue and does not assign
or approve candidate identity. The review application should continue reading
`seek_uuid_match_review` and `seek_uuid_match_review_history` for decisions.
Do not edit an active collector token or extend/delete its claim from the review
UI. The collector includes `worker_id` in new proposal comparison evidence.
