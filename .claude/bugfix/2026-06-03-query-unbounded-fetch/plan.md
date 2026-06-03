# Plan — Bound fabric_execute_query fetch to the row cap

## Root Cause

`execute_query` drains the cursor with no row bound; `fabric_execute_query` applies `effective_cap`
only after the full list is built. Large result sets are fully materialised before truncation.

## Proposed Fix

1. `database.py::execute_query(sql, timeout=30, max_rows=None)`: when `max_rows` is set, stop the
   `fetchmany` loop once `max_rows + 1` rows are accumulated (cap+1 preserves the caller's truncation
   detection) and trim to `max_rows + 1`. When `max_rows` is `None` (internal schema queries), drain as
   before.
2. `query.py::fabric_execute_query`: pass `max_rows=effective_cap` to `db.execute_query`. The existing
   `truncated = len(rows) > effective_cap` / trim logic is unchanged and still correct (rows now capped
   at `effective_cap + 1`).

Minimal, no SQL change; the bound is on the client-side fetch loop. Memory is now O(cap), not
O(result set).

## Files to Modify

- `src/database.py` — `execute_query` gains `max_rows` and bounds the fetch loop.
- `src/tools/query.py` — pass `max_rows=effective_cap`.

## Test Strategy

- `test_database.py::test_execute_query_bounds_fetch_to_max_rows_plus_one` — large first chunk +
  `max_rows=2` → returns 3 rows, `fetchmany` called once (no drain).
- `test_database.py::test_execute_query_without_max_rows_still_drains` — regression: no cap → full set.
- `test_query.py::test_passes_effective_cap_to_db_execute_query` — the tool pushes the cap down.

Existing query/truncation/observability/connection tests must still pass.
