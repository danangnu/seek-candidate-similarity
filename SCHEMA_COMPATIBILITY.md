# Latest supplied schema — 10 September 2026

Checked against `seek_scrap(1).sql` and `seek_scrap_detail(1).sql`.
Their header identifies MariaDB 10.1.48; no live server was queried.

| Purpose | Current column/type | Collector behavior |
| --- | --- | --- |
| Historical row key | seek_scrap.id_pk INT | Read only |
| Legacy numeric ID | seek_scrap.id INT | Used only by alternate seek_scrap source mode |
| History-to-detail link | seek_scrap.seek_scrap_id INT | Join to seek_scrap_detail.id_detail; never treat as UUID |
| Profile update date | seek_scrap.date_updated DATE | Newest dated history first, undated last |
| Detail row key | seek_scrap_detail.id_detail INT | Read only |
| Numeric SEEK ID in detail | seek_scrap_detail.seekid_detail INT, unique | Source ID and future approved-update key |
| Current candidate UUID | seek_scrap_detail.uuid VARCHAR(255) | Read only; future review app may update after approval |

The two old assumptions are removed: `seek_scrap.uuid` and
`seek_scrap_detail.seek_scrap_id` no longer exist in the supplied structures.
The default source remains `seek_scrap_detail`. Joining to update-date history
uses the user-confirmed `seek_scrap.seek_scrap_id = seek_scrap_detail.id_detail`.
The numeric ID comes from `seek_scrap_detail.seekid_detail`; `seek_scrap.id` is
ignored in this mode, even when it contains a different numeric ID. This applies
to dated pages, CSV selection and the undated fallback. Unlinked detail rows are
excluded from the normal queue. Explicit `--id` still selects a detail person
directly and bypasses the queue.
The paged queue keeps each linked person's latest dated snapshot and removes
duplicates. The alternate `target_table: "seek_scrap"` retains the legacy ID path.

Replace `repository.py` and `compare.py` together for paged queue loading; keep
the current `review_schema.sql` from this package. Keep all other
files from the current review-only release, including the preceding sorting fix
in `compare.py`. Alternatively replace all application files; keep your actual
configurations and reports. No change to config keys or candidate tables is needed.

Check with:

```powershell
python compare.py --config config.remote.json check-connections
```

If review tables have not been created, run `init-db` once, then repeat the check.
On MariaDB 10.1 this creates LONGTEXT JSON columns with primary/unique/foreign keys
and skips newer JSON/CHECK clauses. On MariaDB 10.2.6+ the optional checks are
included. Existing review tables and decisions are not altered or reset.
Collector JSON validation uses Python; approval and rejection validation must
be implemented in the separate app, particularly on MariaDB 10.1.

The uploaded SQL dumps contain CREATE DATABASE and USE trackitlive. They are
schema references for this update; this package does not execute or import them.
The collector continues to submit pending proposals only into
seek_uuid_match_review and seek_uuid_match_review_history. It never updates the
candidate UUID, performs approvals, or imports old mappings.

Validation: 104 tests passed, including source queries against fixtures matching
the new columns, ordering, candidate-value preservation, strict JSON, and both
legacy/modern DDL branches translated into SQLite. No actual MariaDB 10.1 DDL,
remote service connection, live SEEK session or review app was tested here.
