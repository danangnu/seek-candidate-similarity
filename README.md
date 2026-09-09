# SEEK numeric ID → UUID backfill

This extends your two-file Python comparison script into a visible Chrome workflow:

1. Read pending numeric SEEK IDs from **local `seek_scrap_detail.seekid_detail`**.
2. Open SEEK and let you sign in, including any verification.
3. Open `https://au.employer.seek.com/talentsearch/profiles/<numeric-id>` and read the live numeric-ID profile.
4. Search Talent Search using that candidate's name and follow the result pages.
5. Check names on every result card, then open only exact-name UUID profiles and compare their details with the live numeric-ID profile.
6. Show evidence and an optional local Ollama opinion.
7. Save confirmed matches manually, or use `--auto-save --apply` to save the first identical complete Profile-content match without candidate-selection prompts.

The program is separate from the VB.NET application. It does not use `DllConnection`, `SQLSetting.ini`, TrackIt login tables, the shared LIVE configuration, or the VB application's browser. Database access is explicitly limited to localhost and a `seek_uuid_test_...` database in this version.

## Faster name filtering

The browser now reads candidate names directly from the result cards before
opening full profiles. Only names matching the live baseline after normalization
(case, whitespace and punctuation) are retained. For example, searching Robert
Rogers no longer opens Robert Smith or other differently named people. Names
with substantive differences, aliases or spelling variations are not opened by
name search; this is consistent with the existing exact-name matching rule.
You can use the explicit direct-pair comparison to investigate such cases.

All results pages are still scanned within the configured limits. In automatic
mode, full-profile visits stop as soon as one exact content match is found. Other
candidates sharing that name do not block the match. An unreadable or changed-name
profile cannot qualify; the tool can continue to the next candidate. Searches
with no exact-name cards make no profile visits and save no mapping.

Console progress now reports cards checked, profiles selected and different-name
cards skipped. The report and audit include the search-card names and UUIDs
(without token-bearing links) under `search_scan`. The original 10-second delays
remain; fewer profile visits provide the speed improvement. Automatic mode uses
deterministic content equality and does not call Ollama. Manual comparison can
still obtain its advisory opinion.

Keep the name-filter/browser changes when upgrading. Follow the destination
update instructions below for the current required files and schema preparation.
Stop the current run with Ctrl+C first. Existing saved mappings remain recorded.

## Change of destination: seek_scrap_detail.seek_scrap_id

The current default destination is now:

| Purpose | Column |
| --- | --- |
| Numeric SEEK identity | `seek_scrap_detail.seekid_detail` |
| UUID to save | `seek_scrap_detail.seek_scrap_id` |
| Detail row primary key | `seek_scrap_detail.id_detail` |

UUID saves do not modify `seek_scrap.uuid`. `seek_scrap` is used only by the
explicit preparation command to seed missing numeric IDs from your existing
local sample. Normal runs read pending IDs directly from `seek_scrap_detail`.
The live numeric profile supplies the name because the detail table has no name
column. Normal detail runs do not require corresponding `seek_scrap` rows or
saved HTML files.

### Update your existing installation

Stop the program. Replace ALL FOUR application files from this ZIP:
`compare.py`, `profiles.py`, `seek_browser.py` and `repository.py`. Keep your
own `config.json`, and add this field INSIDE its `database` object:

```json
"target_table": "seek_scrap_detail"
```

Missing `target_table` now defaults to `seek_scrap_detail`. To intentionally use
the previous destination, set it explicitly to `seek_scrap`. Other names are
rejected; table/column identifiers cannot be supplied as arbitrary SQL.

### Prepare the local table once

Your uploaded schema has `seek_scrap_id INT`. A UUID needs a text column. Run:

```powershell
python compare.py prepare-detail
python compare.py init-db
```

`prepare-detail` is restricted to your configured localhost `seek_uuid_test_...`
database. It requires the existing `seek_scrap_detail` and `seek_scrap` tables,
checks for declared foreign-key relationships, and widens `seek_scrap_id` to
`VARCHAR(255)` when it is currently an integer or a short text column. It then
adds only missing detail rows for distinct positive IDs in your local
`seek_scrap` sample. New rows contain `seekid_detail`; other detail columns keep
their defaults. Existing detail rows and their matching metadata are preserved.
It does not copy existing UUIDs from `seek_scrap` and does not alter `seek_scrap`.
The preparation is repeatable: missing rows are added once.

Existing nonblank `seek_scrap_id` values, including old numeric parent IDs, are
preserved and excluded from ordinary pending runs. Do not clear them blindly.
The earlier VB.NET importer used this column as an internal numeric parent-row
link; repurposing it for UUIDs changes that meaning. Keep this schema change in
the local test copy until those VB.NET consumers are adjusted. A declared
foreign key on the column causes preparation to stop instead of removing it.
MariaDB DDL is separate from transactions: the column widening is not undone by
the mapping rollback command, even if seeding subsequently fails.

If your current column is already `CHAR`/`VARCHAR` of at least 36 characters and
the intended detail rows already exist, skip preparation and run `init-db`.
If the local detail table itself is missing, create it from your supplied schema
first. No production server is contacted by the preparation or writer.

### Test the new destination

```powershell
python compare.py run --limit 1 --auto-save
python compare.py run --limit 5 --apply --auto-save
```

The connection banner displays `target: seek_scrap_detail`. An eligible match
updates only the UUID column for its numeric ID, equivalent to this parameterized
operation, with additional transaction/conflict/audit checks:

```sql
UPDATE seek_scrap_detail
SET seek_scrap_id = :matched_uuid
WHERE seekid_detail = :numeric_seek_id
  AND (seek_scrap_id IS NULL OR TRIM(seek_scrap_id) = '');
```

The colon names above describe parameters; the Python code binds actual values.
To verify in local HeidiSQL:

```sql
SELECT id_detail, seekid_detail, seek_scrap_id
FROM seek_uuid_test_trackitlive.seek_scrap_detail
WHERE seekid_detail = 260138;
```

The audit record stores the destination table with each new change. Rollback
restores that recorded destination. Earlier audit records with the old list
format still roll back `seek_scrap.uuid`, so their meaning is preserved.

## 1. Install on your Windows PC

Extract this ZIP to a new folder. In PowerShell, open that folder and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Use Python 3.10 or newer. Chrome must be installed. The normal browser startup uses Selenium Manager to find a compatible ChromeDriver. If your PC cannot download drivers, set `browser.chromedriver` to the full path of a driver that matches your Chrome version. Do not reuse the old Chrome 150 driver with Chrome 152.

## 2. Configure MariaDB and baseline profiles

Edit `config.json`:

- `database.host`: `127.0.0.1`
- `database.port`: your local MariaDB port, normally `3306`
- `database.user`: a local account with SELECT/UPDATE on the selected target, and CREATE/SELECT/INSERT/UPDATE/DELETE for the mapping/audit tables. `prepare-detail` additionally needs ALTER/INSERT on `seek_scrap_detail` and SELECT on local `seek_scrap`.
- `database.database`: `seek_uuid_test_trackitlive`
- `reviewer`: your staff ID

The program prompts for the MariaDB password without displaying it. Optionally supply it through `SEEK_DB_PASSWORD` in the process environment. Do not put it into the JSON file or a command-line argument.

**The numeric IDs must exist in your selected local target table.** Creating the seven empty tables is not enough. If you have only the earlier `seek_scrap` sample, run `prepare-detail` to create the missing local detail rows. The CSV option only selects numeric IDs; it does not insert candidate rows or change file paths in MariaDB.

Your CSV had 1,000 snapshots covering 285 distinct numeric SEEK IDs. It uses Windows text encoding; the CSV filter supports UTF-8 and Windows-1252 and deduplicates IDs.

### Live numeric profile (default)

The baseline is now the live numeric-ID profile URL, not a saved file.
Keep your existing configuration; the default is `"source_mode": "live_numeric"`.
You can also add that setting explicitly at the top level of `config.json`.
`path_mappings` is not used in this mode, and missing `seek_scrap.file` files do not
block comparison. The local detail rows themselves must still exist (or be created using `prepare-detail`).

After sign-in, the tool opens the numeric route, selects the Profile tab if
necessary, and extracts the main profile. When the legacy `seek_scrap` target is explicitly selected, it also checks the
profile name against that numeric ID's stored names. The detail target has no
name column and uses the live numeric route as its baseline. Login redirects, missing/restricted profiles or unsupported
HTML do not become a successful comparison.

If SEEK redirects the numeric URL directly to a UUID, the tool records that UUID
and reopens its profile for review. It does not pretend to have obtained two
independent profiles in this case. If you specify a different UUID with `--uuid`,
that conflict is rejected.

### Compare a specific pair of URLs

To compare the numeric and UUID routes directly, as in your screenshot, use:

```powershell
python compare.py run --id 12345678 --uuid 11111111-1111-1111-1111-111111111111
```

Replace BOTH example IDs with the numeric ID and UUID from your two links.
The numeric ID must be in the selected local target table. No name search is performed in this
mode, and the report says `direct_pair`. Add `--apply` for normal reviewed-save
prompts, or `--apply --auto-save` to save only if the complete normalized Profile
content is identical. Without `--uuid`, the tool searches by the name
read from the live numeric profile (unless the numeric route already redirects
to a UUID).

### Optional archived HTML mode

The archived workflow requires both `database.target_table: "seek_scrap"` and
`"source_mode": "saved_html"`. The detail target supports live numeric mode only. In that mode the PC must be able to read the `.txt`
HTML paths in `seek_scrap.file`. Network folders may be remapped while preserving
numeric-ID subfolders:

```json
"path_mappings": [
  {
    "from": "\\\\server\\share\\SEEK Scrape Results",
    "to": "C:\\SEEK_Backfill\\old_html"
  }
]
```

Archived mode tries snapshots newest first and records a fallback to older
usable files. Inconsistent historical names require manual investigation.

## Request pacing (updated version)

The default example now uses `"delay_seconds": 10` under `browser`.
It pauses BEFORE every name search, numeric-ID or UUID profile visit, next-results-page
click, and Profile-tab selection when necessary. This includes the transition
to the next numeric ID. The previous version paused only after reading a profile.
Page loading, comparison and your review add more time to that fixed pause.
The initial manual sign-in and local DOM polling are not delayed.

When updating, replace `seek_browser.py` in your existing installation and edit
`browser.delay_seconds` in YOUR `config.json` to `10` (or a longer interval).
Keep your existing database settings and path mappings. An existing explicit
value of `1` continues to mean one second; replacing the example file alone does
not change your actual configuration. Values from 0 to 300 seconds are accepted.

The console displays each pause; Ctrl+C stops the run. A ten-second delay is a
configurable starting value, not a verified SEEK rate limit or a guarantee
against account restrictions. If SEEK displays a rate-limit, verification or
account-restriction message, stop the run and follow SEEK's instructions. This
version does not implement automatic block detection or Retry-After handling.

## 3. Configure Ollama

Use your installed Ollama application and a local model:

```powershell
ollama list
```

Set `ollama.model` to the exact installed model name, for example `llama3.1:8b`. If needed, install that model with `ollama pull llama3.1:8b`. Keep Ollama running on `http://127.0.0.1:11434`.

Ollama sees only the extracted candidate fields, not browser cookies or token-bearing URLs. Its confidence is an uncalibrated opinion, not an identity probability. Profile content is presented as untrusted data, and the model cannot execute SQL or approve a mapping.

Use `--no-ollama` to compare structured evidence without the model. If Ollama fails, the report records that it was unavailable; it does not invent an AI verdict.

## 4. First run: compare only

```powershell
.\.venv\Scripts\python.exe compare.py run --limit 5
```

Or test one numeric SEEK ID already in your database:

```powershell
.\.venv\Scripts\python.exe compare.py run --id 569702361
```

The terminal prints the configured host, actual server hostname/port and selected database. Chrome opens visibly. Sign in yourself, complete any verification, open Talent Search, then return to PowerShell and press Enter. No SEEK password is stored in the script.

The script searches by name through `searchQuery` using the supplied new Talent Search route, handles the introduction popup and compares UUID profiles. It does not click **Access profile**, **Send message**, **Connect**, or any other paid/contact action. Restricted or incomplete profiles stay unresolved.

Reports are written under `reports/<run-id>/<numeric-id>.json`. Each contains the baseline profile, compared UUID profiles, shared evidence and model opinion if available. Live mode records the numeric URL, any redirected UUID and a hash of the extracted baseline fields; archived mode records the source snapshot ID and file SHA-256. It does not store full browser HTML or `serviceToken` links.

### Limit a run to your CSV

```powershell
.\.venv\Scripts\python.exe compare.py run --csv "C:\path\09(2).csv" --limit 5
```

Only IDs both present in the CSV and pending in the selected local target table are processed. With no CSV, all pending local numeric IDs are eligible. Already-filled IDs are excluded from subsequent ordinary/CSV runs. Unresolved IDs remain pending; use `--id` to investigate a particular record.

## Automatic saving: no candidate number, reason or confirmation prompts

First complete the destination update and column preparation above. Automatic
saving uses the same target and audit schema as manual saving; the flag itself
requires no further schema change. First-time users must still run
`python compare.py init-db` once.

Run the known candidate automatically:

```powershell
python compare.py run --id 260138 --apply --auto-save
```

Or process a small batch:

```powershell
python compare.py run --limit 5 --apply --auto-save
```

You still enter the MariaDB password when needed and sign in to SEEK once.
After that, there are no candidate-selection, reason or SAVE-confirmation
questions. Eligible matches save automatically. Others are skipped and their
reason is recorded in the JSON report; automatic mode never falls back to a
manual prompt. Existing nonblank UUIDs and conflicting mappings are protected
by the same transactional checks.

To preview automatic decisions without any database updates:

```powershell
python compare.py run --limit 5 --auto-save
```

Automatic policy `first_identical_profile_content_v2` compares the main Profile
panel captured from both pages' HTML. It compares the name, structured career
history, summary, education, licences, skills, languages and current status,
plus ALL Profile-panel text, including career descriptions and unrecognized
sections. It ignores surrounding navigation, interaction-history tabs,
recommendation cards, buttons, CSS/classes and generated element IDs. Unicode
and whitespace are normalized; actual words, dates, punctuation and case are
retained. This is exact equality of normalized candidate content, not a
byte-for-byte comparison of the entire HTML page.

Both captures must contain an identifiable main Profile tab and non-name
evidence: a complete employer/title/date entry, or a summary together with
education or licences. A name alone, a loading screen, or an old extraction
without the full Profile tab cannot qualify. A summary is NOT required when a
complete career entry is present. Two employers are NOT required. Matching
names only select which pages to visit; rank 100 or an Ollama verdict cannot
substitute for identical content.

For each numeric ID, the first exact content match is saved immediately and the
remaining UUID profiles are not visited. The run then moves to the next numeric
ID. The report records `profiles_not_visited`; it does not claim the unvisited
profiles were checked or that the chosen account is unique. Identical visible
content does not establish uniqueness across separate accounts. Existing
mapping collisions still prevent writes.

Discovery still requires collection of result cards within the configured
search limits. Explicit `--uuid` pairs and numeric-route redirects also support
automatic comparison, without claiming a name search was performed. A redirect
is recorded as such and is not presented as two independently obtained profiles.

The JSON evidence includes:

- `profile_content_equal`: the actual automatic equality result.
- `old_content_sha256` and `new_content_sha256`: normalized content hashes.
- `differing_fields`: fields that differ; `profile_content` includes full tab text.
- `full_profile_tab_captured` and `non_name_details_present`: readiness checks.
- Both extracted records, including `profile_content`, for investigating differences.

When no exact match is found, no UUID is written. Inspect the differing fields
instead of relying on the old rank. The September 9 console log alone cannot
prove the entire Profile content matched because the old extractor omitted
fields and descriptions; run the updated version to capture that evidence.

Audit evidence records the generated reason, matching policy, content hashes,
selected UUID and `review_mode: automatic`. The configured `reviewer` identifies
the operator, not a claim of manual review. Rollback remains available.

Reports use `auto_eligible_dry_run`, `auto_skipped`, or `saved` as appropriate.
A successful automatic write prints `Automatically saved UUID ...` with the
numeric ID, target table, affected-row count and rollback change ID. A dry run
prints `Would automatically save ...` and never updates the database.

Search readiness now accepts both `1 matching profile` and plural result counts.
The log/report identifies whether a timeout happened while loading the numeric
profile, loading search results, comparing UUID profiles or saving. The earlier
two search timeouts have not been reproduced live here; a timeout in a changed
layout may still require a new HTML capture. Profile extraction waits for loaded,
stable content over 1.5 seconds of polling. This does not expand collapsed sections
or access restricted content; it compares the Profile content present in the DOM.

## Quick test of the updated matching rule

Keep your local database configuration and ten-second delay, replace the four
Python files, and run:

```powershell
python compare.py run --id 464775 --apply --auto-save
```

Then check local HeidiSQL:

```sql
SELECT id_detail, seekid_detail, seek_scrap_id
FROM seek_uuid_test_trackitlive.seek_scrap_detail
WHERE seekid_detail = 464775;
```

The UUID is saved in `seek_scrap_id`; the legacy numeric ID remains in
`seekid_detail`. Existing nonblank destination values are preserved. Once the
single-record result is verified, run `--limit 5 --apply --auto-save`.

## 5. Save reviewed mappings (manual mode)

Initialize two additional tables once:

```powershell
.\.venv\Scripts\python.exe compare.py init-db
```

Then run:

```powershell
.\.venv\Scripts\python.exe compare.py run --limit 5 --apply
```

For each numeric ID:

1. Read the old and new profiles in the JSON report, including career dates and contradictions.
2. Select the matching candidate's number, or press Enter to skip.
3. Enter your reason for confirming the match.
4. Type the exact displayed `SAVE <numeric-id> <uuid>` phrase.

In manual mode, all writes require review, even when the top rank is high. Same-name-only matches are blocked. This first version permits review for an exact normalized name plus at least one shared employer/job-title pair, or a substantial identical summary. That is a minimum review gate, not proof. Multiple plausible people should be skipped. Name variations requiring an override are not supported in this version.

The local database transaction uses the selected target table:

- Rechecks the original row fingerprint to detect changes since comparison.
- Rejects a numeric ID with a different nonblank UUID.
- Rejects a UUID already linked to a different numeric ID or approved mapping.
- Allows UUID-only rows with a NULL numeric identity; it does not assign them a numeric ID.
- Adds an identity mapping and a detailed audit record with the original UUID values.
- Updates only blank `seek_scrap_detail.seek_scrap_id` values where `seekid_detail` matches the numeric ID (or `seek_scrap.uuid` in explicit legacy-target mode).
- Rolls everything back if any part fails.

`seekid_detail`, `id_detail`, matching metadata, and other fields stay unchanged. The uploaded detail schema uniquely indexes `seekid_detail`; each legacy numeric ID normally has one detail row. The two new tables are `seek_candidate_identity_map` and `seek_uuid_backfill_audit`.

Use a local test copy with the old scraper stopped while validating. The tool uses database transactions and an application lock, but unrelated applications do not necessarily obey the same mapping rules. If a connection is lost around commit, check the audit table before deciding that an update failed.

### Check results in HeidiSQL

```sql
USE seek_uuid_test_trackitlive;
SELECT numeric_seek_id, profile_uuid, reviewed_by, reviewed_at
FROM seek_candidate_identity_map
ORDER BY reviewed_at DESC;

SELECT id_detail, seekid_detail, seek_scrap_id
FROM seek_scrap_detail
WHERE seekid_detail = 569702361
ORDER BY id_detail;

SELECT change_id, numeric_seek_id, profile_uuid, created_at, reverted_at
FROM seek_uuid_backfill_audit
ORDER BY created_at DESC;
```

### Roll back a saved mapping

Use the change ID printed after a save or stored in the audit table:

```powershell
.\.venv\Scripts\python.exe compare.py rollback YOUR-CHANGE-ID
```

Type the displayed confirmation. Rollback restores the exact previous UUID values only on rows changed by that transaction. It refuses if any affected row is missing or no longer has the expected numeric ID/UUID. An audit record remains. An existing mapping owned by an earlier change is preserved; a mapping created by this change is removed only when no other active backfill depends on it.

## Search and matching limits

- A name search is discovery, not an exhaustive proof of identity. SEEK's search behavior, access level and available profile data affect what it returns.
- When discovering candidates by name, all returned result pages must be collected within `max_search_pages` and `max_candidates`. Truncation, changing result counts, repeated pages and unreadable card names block that search. In automatic mode, an unreadable UUID profile is skipped and cannot qualify; a later exact match can still save. Manual mode requires every selected profile to be readable.
- Career history is scoped to its own section. Employer, title and date are separate fields; education is not mixed into career history. Similar-candidate anchors are excluded.
- A changed title/location or missing data is not automatically a different person. A human must assess the evidence.
- Numeric URLs and UUID URLs both need a readable main Profile view. In optional archived mode, old saved HTML in an unsupported layout is skipped. Other layouts may need additional adapters.
- This tool does not migrate the rest of the VB.NET emergency scraping workflow or create a mapping for someone who cannot be reliably identified.

## Offline comparison and checks

The original two-file comparison is still available, without importing code triggering a run:

```powershell
.\.venv\Scripts\python.exe compare.py compare-files seekid.txt uuid.txt --no-ollama
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Supply your own existing `seekid.txt` and `uuid.txt`; candidate HTML and database exports are not bundled in this code package.

Validation performed here: 47 automated offline tests, Python syntax checks, extraction against both supplied comparison profiles and the earlier full-profile capture. Seven repository tests execute target updates and rollback through a SQLite adapter; they do not validate MariaDB-specific DDL or locking. Tests cover detail-only writes, preservation of old numeric values, transaction failure, collision checks and rollback of older main-table audit records. The supplied numeric/UUID HTML pair produced identical normalized full Profile content and matching hashes. Both comparison profiles yielded four career entries with job titles, two education entries and eight licence/certification entries. The separate CV-tab capture correctly failed the full-profile readiness check.

**Not tested here:** actual SEEK sign-in/search, a running local Ollama model, or live MariaDB writes/rollback. Start with one candidate in the local test database and verify the saved rows and rollback before a larger batch. No production writes were performed.

## Files

- `compare.py`: command-line workflow, live/archived source selection, review and reports.
- `profiles.py`: HTML extraction, UUID validation, matching evidence and Ollama API.
- `seek_browser.py`: visible Chrome sign-in, numeric/UUID profiles, name search, pagination and pauses.
- `repository.py`: local-only database connection, mapping transaction and rollback.
- `config.example.json`, `requirements.txt`: setup.
- `tests/test_backfill.py`: offline regression tests.

Implementation references: [Selenium Manager](https://www.selenium.dev/documentation/selenium_manager/), [Selenium waits](https://www.selenium.dev/documentation/webdriver/waits/), [Ollama chat API](https://docs.ollama.com/api/chat), [PyMySQL connections](https://pymysql.readthedocs.io/en/latest/modules/connections.html).
