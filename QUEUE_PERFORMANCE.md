# MariaDB 10.1 queue performance fix

Both previous queries grouped the joined history for every person, sorted the
result, and returned the whole queue before Python applied --limit. Wrapping
that in a derived table did not remove the expensive aggregation. The original
aggregate alias inside an ORDER BY expression can also be problematic on older
MariaDB versions; the exact database error is needed to confirm that separately.
A four-minute runtime alone does not establish a SQL syntax/version error.

This update replaces that normal queue path. It does not run the old query or
raise database timeouts to hide it.

## Install and measure

Replace **repository.py and compare.py together**. Keep the latest review schema,
config, reports and pending review records. No DDL or index migration is run.

```powershell
python compare.py --config config.remote.json check-queue --limit 5 --explain
```

This opens no browser, invokes no Ollama model and writes no records. It prints
EXPLAIN plans for the latest-date lookup and a page within that date, then the
first five eligible numeric IDs and elapsed queue time. The timed portion does
not include the earlier EXPLAIN commands. Share the output to assess the actual
server plan; SQLite unit tests do not establish production latency.

When the plan/time is acceptable:

```powershell
python compare.py --config config.remote.json run --limit 5 --submit --auto-propose
```

For one known numeric ID, the queue is skipped altogether:

```powershell
python compare.py --config config.remote.json run --id 260138 --submit --auto-propose
```

## New read path

1. Find the next non-NULL profile date using the existing date_updated index:

```sql
SELECT date_updated
FROM seek_scrap FORCE INDEX (date_updated)
WHERE date_updated IS NOT NULL
ORDER BY date_updated DESC
LIMIT 1;
```

2. Read a bounded page within that date, newest historical primary key first:

```sql
SELECT p.id_pk, d.seekid_detail AS id, p.date_updated
FROM (
    SELECT id_pk, seek_scrap_id, date_updated
    FROM seek_scrap FORCE INDEX (date_updated)
    WHERE date_updated = %s
    ORDER BY id_pk DESC
    LIMIT 500
) p
LEFT JOIN seek_scrap_detail d ON d.id_detail = p.seek_scrap_id
ORDER BY p.id_pk DESC;
```

On the next page, add `AND id_pk < %s`. After finishing the date, the next-date
lookup uses `date_updated < %s`. Placeholders above are bound by Python.

This implements the confirmed `seek_scrap.seek_scrap_id = seek_scrap_detail.id_detail`
relationship. The inner page is bounded to 500 history rows before joining. The
internal LEFT JOIN retains orphaned rows only to advance the pagination cursor;
NULL numeric IDs are discarded. Returned people therefore require a matching
detail row, as in the requested INNER JOIN. A page consisting entirely of missing
links cannot prematurely stop the scan. No full-history derived aggregation is used.

3. Deduplicate numeric people in Python, and check source membership/past
proposals using an IN list of at most 500 IDs. Close each cursor before yielding
IDs to the browser. The first occurrence gives each person's latest date.
4. Stop as soon as the requested number of people has been yielded. Full runs
consume the same iterator progressively, rather than preloading one million IDs.
5. Only after exhausting dated history, retrieve people with linked history but no
profile date in ascending numeric-ID pages. Unlinked detail people are excluded. They remain behind dated profiles.

This follows the cursor-based pagination approach described in
[MariaDB pagination optimization](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/query-optimizations/pagination-optimization).

For explicit CSV files, the collector filters IDs first, then gets MAX(date_updated)
for ONLY selected eligible IDs in batches. That restricted aggregation uses the
detail seekid_detail index and the history seek_scrap_id link index. It must rank the selected CSV set before yielding
results; a very large CSV is still more work than a small one.

## Existing indexes and limits

The supplied schemas already show date_updated and seek_scrap_id indexes on
seek_scrap, the id_detail primary key and idx_seekid on seek_scrap_detail.seekid_detail, and the review DDL defines an index
starting with (source_table, seekid_detail). The date queries name the date_updated index explicitly; if your installed names differ, reconcile them before running.

Check EXPLAIN for a usable date_updated key and a primary-key detail lookup.
Remeasure after this join update; earlier timings do not validate the new plan. If the within-date query shows a
large filesort or poor estimates, investigate the actual plan and index definitions;
do not assume that FORCE INDEX guarantees an efficient plan. This release does
not create indexes on the live table. A DBA can assess a composite
(date_updated, id_pk) index if the existing secondary index/optimizer cannot
satisfy within-date ordering; that change needs its own deployment assessment.

A page contains at most 500 returned history rows. Finding eligible people can
require multiple pages, especially when many snapshots are duplicates, reviewed,
or absent from detail. An empty queue may require walking all dated history;
progress is printed every 10,000 history rows. Full runs still ultimately read
the relevant history. The undated fallback and very large CSV sets can also be
expensive. No specific speedup is claimed without measuring on your server.

Tie behavior: normal date ties use historical id_pk descending; CSV date ties
use numeric ID ascending. Both keep newest-profile dates first. Live changes
can place rows ahead of an already-advanced cursor; these wait for a later run.
No server cursor or transaction remains open during browser work or breaks.

Candidate UUIDs remain untouched. Every submission is pending human review.

Validation: 104 tests passed. Link-specific checks cover mismatched legacy IDs,
orphan/null-link pages, CSV dates and linked undated people. Added bounded early-stop, cross-page/date duplicate,
reviewed/non-detail exclusion, CSV restriction, iterator resume and direct-ID
bypass checks. SQL execution used SQLite adapters; real MariaDB 10.1 query plans,
four-minute production behavior and server load were not reproduced here.
