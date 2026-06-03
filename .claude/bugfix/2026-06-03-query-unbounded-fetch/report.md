# Report — fabric_execute_query unbounded fetch (P8, Medium)

## Root Cause

`execute_query` drained the cursor with no row bound; `fabric_execute_query` applied `effective_cap`
only after the full list was built. A large `SELECT` materialised the entire result set into Python
dicts before discarding all but `max_rows`, risking an OOM on memory-limited ACA replicas (which would
drop every concurrent request on that replica).

## Fix Description

`execute_query` now accepts `max_rows`. When set, the `fetchmany` loop stops once `max_rows + 1` rows
are accumulated (cap+1 preserves the caller's truncation detection) and trims to that bound; `None`
(internal schema queries) drains as before. `fabric_execute_query` passes `max_rows=effective_cap`. The
existing `truncated = len(rows) > effective_cap` / trim logic is unchanged. Memory is now O(cap), not
O(result set). No SQL change.

Scope: `src/database.py` (`execute_query`), `src/tools/query.py` (push the cap down).

## Tests Added

- `test_database.py::test_execute_query_bounds_fetch_to_max_rows_plus_one` — large first chunk +
  `max_rows=2` → 3 rows returned, `fetchmany` called once (no drain).
- `test_database.py::test_execute_query_without_max_rows_still_drains` — regression: no cap → full set.
- `test_query.py::test_passes_effective_cap_to_db_execute_query` — the tool pushes the cap down.

Verification: the two bound/push tests failed before, pass after; drain test passed throughout.
db+query suites: 67 passed. Full suite: **198 passed, 6 skipped**. `ruff` clean.

## Residual Risks

- The bound is client-side (fetch loop), not a server-side `TOP`/`OFFSET-FETCH`. The Fabric server may
  still compute the full result; only client memory is protected. A server-side row limit could be a
  future enhancement but would require safely wrapping arbitrary user SQL.
- Internal callers that pass no `max_rows` (schema discovery) still drain — intended, as those results
  are inherently small.

## Git Commits

- Branch: `fix/query-bounded-fetch` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
