# SEEK UUID proposals for human review

This release replaces direct UUID backfilling with a shared review queue.
**Python never updates `seek_scrap_detail`, `seek_scrap`, or the old identity
mapping/audit tables.** It compares live profiles and submits pending proposals.
The separate review app will approve/reject and apply approved mappings later.

## Install this update

Stop older running copies first. Replace the application files with this package,
including `repository.py`, `review_schema.py` and the latest `review_schema.sql`. Keep your actual
configuration and reports. Seven application modules are included:
`compare.py`, `repository.py`, `profiles.py`, `seek_browser.py`,
`runtime_breaks.py`, `network_config.py`, and `review_schema.py`.

```powershell
pip install -r requirements.txt
```

Keep existing database, Ollama and browser settings. Add these top-level entries
to your actual config, substituting your staff IDs:

```json
"created_by": "dnurdiansyah",
"review": {
  "assigned_reviewer": null
}
```

Set `assigned_reviewer` to the intended reviewer's staff ID to assign new
proposals, or leave it `null` for the separate app to assign later. It is not an
approval. Existing `reviewer` configurations remain supported as the collector's
identity if `created_by` is absent. Python does not verify staff membership;
the separate app must resolve staff IDs against its authenticated user directory.

For remote servers use [REMOTE_SETUP.md](REMOTE_SETUP.md). The commands below use
`config.remote.json`; omit `--config config.remote.json` to use `config.json`.

Create only the two review tables in the configured database, then check:

```powershell
python compare.py --config config.remote.json init-db
python compare.py --config config.remote.json check-connections
```

`init-db` reads the bundled `review_schema.sql`. It does not alter candidate
columns, insert candidate rows, migrate old mappings, or change existing reviews.
Existing incompatible review tables need a deliberate migration; they are not
silently replaced. Both source and review tables are expected to use InnoDB.
The supplied dump identifies MariaDB 10.1.48. Setup keeps snapshots as LONGTEXT;
JSON/CHECK constraints are version-gated for MariaDB 10.2.6 and later. On 10.1,
application validation is required; the collector submits strict JSON and only
pending proposals. The separate review app must enforce its decision rules.

One candidate, preview only (local JSON report; no queue insert):

```powershell
python compare.py --config config.remote.json run --limit 1 --auto-propose
```

Five candidates, submit pending proposals automatically:

```powershell
python compare.py --config config.remote.json run --limit 5 --submit --auto-propose
```

Add `--with-ollama` for an advisory opinion from the configured Ollama server.
Automatic comparison otherwise skips Ollama. The model never approves a proposal.
Sign in to SEEK manually in the visible Chrome window, complete verification,
then press Enter once in the terminal. No candidate number, reason or approval
confirmation is requested by the collector.

For a larger run (up to one million pending numeric IDs):

```powershell
python compare.py --config config.remote.json run --limit 1000000 --submit --auto-propose
```

### Compatibility changes

| Old command/flag | Current behavior |
| --- | --- |
| `--apply` | Alias for `--submit`: pending review inserts only |
| `--auto-save` | Alias for `--auto-propose`: select first identical full Profile match |
| `init-db` | Creates review queue/history only |
| `prepare-detail` | Removed; no candidate schema changes |
| `rollback` | Removed; candidate correction belongs to the review app |

Old mapping/audit tables and already-written UUID values are preserved. This
release does not undo earlier approved or automatic backfills. Stop running old
versions to prevent them continuing to update candidate tables.

## Where results are saved

In the SAME configured MariaDB database:

- `seek_uuid_match_review`: proposed numeric ID/UUID, both snapshots, evidence,
  source fingerprint, comparison version, creator, assigned reviewer and status.
- `seek_uuid_match_review_history`: a `submitted` event created in the same
  transaction, recording the collector and initial reviewer assignment.

New rows have `status='pending'`, `row_version=1`, and NULL `reviewed_by`,
`reviewed_at`, `review_reason`, and `applied_at`. Snapshots are JSON objects stored
in LONGTEXT, including full extracted Profile text and structured fields. The
other app can review these without reading files from your Windows PC.

Local diagnostic reports also remain at `reports/<run-id>/<numeric-id>.json`.
Reports include `review_id`, `review_status`, and `candidate_rows_updated: 0`
after successful submission. An optional report reference in database evidence
is informational; snapshots and evidence are already in the database.

Inspect the queue in HeidiSQL with the intended database selected:

```sql
SELECT review_id, seekid_detail, proposed_uuid, exact_content_match,
       status, created_by, assigned_reviewer, reviewed_by, created_at
FROM seek_uuid_match_review
ORDER BY review_id DESC
LIMIT 20;
```

## Collection and matching rules

The default source is numeric `seek_scrap_detail.seekid_detail`.
`database.target_table` now identifies a READ-ONLY source; the alternate source
`seek_scrap.id` remains supported. In the latest uploaded schema,
`seek_scrap_detail.uuid` is the existing VARCHAR(255) UUID column and
`seek_scrap.seek_scrap_id` is an integer link. The main `seek_scrap` table has
no UUID column. Python reads the detail UUID as source state and preserves the
main table link as `source_link` in evidence; neither value is changed.
No conversion or new candidate UUID column is required for this collector.
The separate review app's approved destination is now `seek_scrap_detail.uuid`,
selected by `seekid_detail`. See [SCHEMA_COMPATIBILITY.md](SCHEMA_COMPATIBILITY.md).

Queue order is newest updated person first. Normal collection walks the existing
`seek_scrap.date_updated` index one date at a time, newest first, and reads at most
500 history rows per page. It uses an `id_pk` cursor inside a date, so no OFFSET
or full-history GROUP BY is needed. The first occurrence of each numeric person
is their latest dated snapshot. Small membership queries match `seek_scrap.id`
to `seek_scrap_detail.seekid_detail` and exclude existing proposals.

For same-day profiles, history `id_pk` descending breaks ties. People with no
dated history appear last in numeric-ID order. CSV selection uses indexed,
restricted groups of at most 500 selected IDs and keeps numeric-ID ties within
equal dates. Neither mode sorts by scrape time. The uploaded profile date is a
DATE column, so actual within-day update times are unavailable.

The scraper consumes this queue lazily and stops at `--limit`; it no longer loads
all pending IDs before opening Chrome. `--id` bypasses queue loading entirely.
Dates are read from the stored history as the cursor advances. A newly inserted
or updated row ahead of the cursor may wait until the next run; this is not a
frozen full-database snapshot. IDs already seen are not repeated in the run.

Both modes require SELECT on `seek_scrap`; the default path uses its existing
`date_updated` index and the CSV path uses its existing `id` index. No index is
created automatically. See [QUEUE_PERFORMANCE.md](QUEUE_PERFORMANCE.md) for the
read-only `check-queue --limit 5 --explain` timing/plan command and known limits.

Normal batches exclude numeric IDs that already have ANY proposal in the same
source table, including rejected proposals. An explicit `--id 260138` can
investigate a previously submitted candidate. Identical profile evidence returns
the existing review ID without overwriting its decision/assignment or adding
another submitted event. Changed evidence can create a new proposal after a
rejection; existing pending, approved or applied proposals block a replacement.
Proposals for different numeric IDs may share a UUID: reviewers must resolve
identity collisions before applying, as described in the review-app contract.

The default `source_mode` is `live_numeric`: open the numeric profile, search by
name, scan result pages, and compare same-name UUID profiles. Direct comparison
is available with `run --id NUMERIC_ID --uuid UUID`. `--csv file.csv` restricts
normal pending IDs using an `id` column. Archived HTML mode remains supported
for `seek_scrap` with `source_mode: saved_html` and existing `path_mappings`.

`--auto-propose` stops at the first identical normalized COMPLETE Profile-tab
content match, then submits only that proposal. Name or score alone is not
sufficient. Normalization ignores markup/styling and whitespace while preserving
Profile content, names, career history, dates, education, skills and descriptions.
This is not a guarantee of identity; human review is still required.

Without `--auto-propose`, Python compares the available candidates and proposes
the strongest comparison eligible for review. Such a proposal may contain
field differences (`exact_content_match=0`). Missing complete snapshots and
name-only matches cannot be submitted. Failed/no-match comparisons stay in
local reports and do not create a proposal with an invented UUID.

Ollama is advisory: `--with-ollama` enables it in automatic mode; `--no-ollama`
disables it in other modes. A model failure is recorded and does not override
the deterministic comparison. Review decisions do not automatically train models.

## Delays, permissions and troubleshooting

The ten-second request delay remains. The four idle settings are read from
`seek_scrap_settings` in the configured database (`breaks.settings_id`, default 1).
Before one elapsed hour, between-candidate breaks use `idle_less_than` to
`idle_less_than2` seconds; at/after one hour, `idle_more_than` to `idle_more_than2`
minutes. The per-process timer starts after sign-in, includes breaks, and does
not reset after a long break. Invalid settings stop the run. Ctrl+C interrupts.
The local settings helper SQL remains local-only; do not execute it as a remote
migration. Existing settings do not need to be recreated.

For routine collection, use SELECT on candidate/settings tables and SELECT/INSERT
on review/history tables. Initialization additionally requires CREATE (including
the history foreign key). Candidate UPDATE/INSERT/ALTER/DELETE permissions are
not needed. A separate account for the future review app can have its own grants.
MariaDB session timestamps are UTC. After an ambiguous connection/commit error,
check the review queue before retrying; submission hashes prevent identical
retries from resetting existing reviews.

The review UI and approval/apply service are not part of this Python package.
See [REVIEW_APP_CONTRACT.md](REVIEW_APP_CONTRACT.md) for their database contract.

## Validation and changed files

Run `python -m unittest discover -s tests -v`. This release passed 100 tests,
including matching, browser-flow fakes, remote-service fixtures, idle timing,
proposal/history transaction rollback, reviewer assignment and no candidate writes.
SQLite adapters exercise the schema and repository SQL with MySQL-specific DDL
translated; MariaDB named locks, actual DDL/TLS and live SEEK were not tested here.
See `VALIDATION.txt` for the actual test output and limits.

Changed: `compare.py`, `repository.py`, both example configs, README, remote guide,
and tests. Added: `review_schema.py`, `review_schema.sql`,
`REVIEW_APP_CONTRACT.md`, `tests/test_review_cli.py`.
The uploaded profile parser, browser, network settings and runtime-break logic
are preserved. `seekid.txt` and `uuid.txt` are preserved from the uploaded package.
