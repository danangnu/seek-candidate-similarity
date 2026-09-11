# Western Australia search and unresolved profiles

The supplied work-claim dump records numeric ID 563879489 as finished/unresolved.
The claim table records ownership and outcome, not the exception or comparison
content. A search-result card confirms a profile is listed; it does not establish
that the legacy numeric profile loaded, that all search pages were readable, or
that the complete numeric and UUID Profile content matched. The original failure
cannot be determined from the claim dump and screenshot alone.

The previous search code did not specify a location. This release defaults name
searches to Western Australia WA, matching the requested location chip. This may
reduce broad result sets; it is not a confirmed explanation for the old failure.
The listing supplied for investigation uses UUID
64113200-3264-e1d5-48ea-539900000000. No mapping has been approved or written by
this update, and the serviceToken from the supplied URL is not copied here.

## Install and configure

Stop old runs and copy the updated application files to every machine. Keep
actual configs and reports. No schema changes or new dependencies are needed.
If shared claims are already installed, init-db need not be rerun for this change.

Existing configs automatically use Western Australia WA. To make it explicit,
add this inside the existing browser object:

```json
"search_location": "Western Australia WA"
```

Set it to null to deliberately search all Australia again. This is a SEEK name
search filter only; it does not filter the numeric-ID database queue. Direct
--id/--uuid comparisons and numeric-route UUID redirects do not perform a name
search and therefore do not use this location filter.

The first search selects the exact autosuggest option in the Location filter,
reads the locations value from SEEK's resulting URL, and reloads with that
confirmed value. Later searches reuse it in the same Chrome session. No location
numeric identifier is guessed. The exact selected chip must be visible before
results are collected. A missing/ambiguous option or unconfirmed chip produces a
clear error; the app does not silently fall back to an Australia-wide search.
The selector is based on the supplied September search HTML. It still requires
verification in your authenticated live SEEK session.

## Investigate the supplied numeric/UUID pair

Preview the two profiles directly, without database writes or a name search:

```powershell
python compare.py --config config.remote.json run --id 563879489 --uuid 64113200-3264-e1d5-48ea-539900000000 --auto-propose
```

To test the Western Australia name search instead:

```powershell
python compare.py --config config.remote.json run --id 563879489 --auto-propose
```

Only add --submit when collecting pending review proposals. Shared claims and
retry delays still apply to submit runs. Full Profile-content equality remains
the automatic selection rule; appearance in search or name equality is not enough.

Locate the latest local report in PowerShell:

```powershell
Get-ChildItem .\reports -Filter 563879489.json -Recurse |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
```

Share that JSON to diagnose the actual stage. Older reports may already contain
stage, error_type, reason, search_note and candidate capture errors. New timeout
reports additionally contain browser_diagnostics, a controlled failure reason,
and partial search progress when available. They do not include raw Selenium
exception strings, query strings, service tokens, full page HTML or screenshots.

## Interpret the result

- loading_live_numeric_profile: the legacy source could not be read reliably.
- loading_name_search_results: inspect reason, search_scan and browser_diagnostics
  for location selection, page limits, incomplete results or timeouts.
- comparing_uuid_profiles: inspect each candidate's capture_error/reason and
  browser_diagnostics. A visible result card does not guarantee profile access.
- submitting_review_proposal: inspect database/claim errors; matching alone does
  not mean the database accepted the pending proposal.
- auto_skipped/proposal_dry_run/auto_eligible_dry_run: distinct from unresolved;
  these can mean no exact match or an intentional no-write preview.

If Chrome shows verification, complete it manually. The app does not bypass it.
Matching remains unchanged, candidate UUIDs remain read-only, and submissions
still go to the separate human review queue.

Validation: 161 tests passed, including fake-browser location selection/reuse,
wrong/missing location rejection, unfiltered opt-out, lost-claim propagation,
partial search progress and token-free timeout reporting. No live SEEK profile
comparison, remote database write or new server latency test was performed.

## Correct incomplete location text

The latest screenshot shows incomplete text (`etr utai A`) without a selected
location chip. The supplied HTML confirms the exact chip label is
`Western Australia WA`. This is consistent with text being lost while the
controlled input rerenders; the live cause has not been reproduced here.

The input now receives the complete value through its native setter and a
bubbling input event. The app reacquires the input after the configured delay,
checks its complete value on three consecutive polls, and retries at most three
times if the value does not remain complete. It also reacquires the exact
autosuggest option after the selection delay. Results are collected only after
the selected WA chip and location URL value have been verified.

Replace `seek_browser.py` on every worker and restart the process. Existing
configuration can be retained. Test with `--id 563879489 --auto-propose`, without
`--uuid`, so the name search and location selection are exercised. No schema
change is required. Five new fake-browser tests cover corrupted input, bounded
retries, replaced inputs/options and propagation of a lost work claim. These
tests do not replace verification in an authenticated SEEK browser.

WA suggestion query correction: enter `Western Australia`, then select the exact
`Western Australia WA` suggestion. The configured search_location remains
`Western Australia WA`, and the selected chip must have that complete label.
Replace seek_browser.py and restart; no configuration change is required.

## Incomplete first page with the WA filter confirmed

Report 573020724 shows location_verified=true, 38 total results, 20 cards checked
and one page read. The code stopped because its Next selector found no usable
control. This report alone does not distinguish delayed rendering from changed
pagination markup.

When more results remain and no usable Next control is found, the app now opens
the next page using the existing pageNumber search parameter. It retains the
name, sorting and confirmed WA location value. Repeated pages, changed totals,
page/candidate limits and missing location verification still block a complete
search. The app does not treat a partial list as complete.

Replace seek_browser.py on every worker and restart. Retry without --uuid:

```powershell
python compare.py --config config.remote.json run --id 573020724 --auto-propose
```

Expected for an unchanged result set: 38 cards checked across two pages before
full comparisons begin. This is a dry run. Live testing is still required.
