# Report — fabric_describe_table cross-schema column merge (P5, High)

## Root Cause

For an unqualified `table_name`, `schema` is `None`, so `fabric_describe_table`'s SQL omits the
`AND c.TABLE_SCHEMA = ...` predicate and returns columns of every same-named table across all schemas.
The code took `rows[0]["TABLE_SCHEMA"]` as the single resolved schema and concatenated all rows'
columns, silently merging distinct physical tables into one fictitious description with no error.

## Fix Description

After fetching rows, when the call was unqualified (`schema is None`) and the rows span more than one
distinct `TABLE_SCHEMA`, `fabric_describe_table` now raises `ToolInputError(code="TABLE_AMBIGUOUS", ...)`
whose message lists the candidate schemas (sorted) and suggests re-running with a `schema.table`
qualifier. Single-schema results (qualified call, or unqualified name unique across schemas) are
unchanged. No SQL change — the guard is applied to the existing result set, which already carries
`TABLE_SCHEMA` per row. `TABLE_AMBIGUOUS` is documented in the spec contract's Error Code Reference.

Scope: `src/tools/schema.py` (guard + docstring) and the contract doc.

## Tests Added

In `tests/unit/test_tools/test_schema.py::TestFabricDescribeTable`:

- `test_unqualified_name_ambiguous_across_schemas_rejected` — unqualified name in `gold` + `raw` →
  `TABLE_AMBIGUOUS`, message lists both schemas, no `columns` returned.
- `test_unqualified_name_single_schema_multiple_columns_ok` — regression guard: multiple columns of one
  table (same schema) are not mistaken for ambiguity.

Verification:
- New ambiguity test failed before the fix, passes after; the single-schema guard passed throughout.
- `tests/unit/test_tools/test_schema.py`: 14 passed. Full suite: **197 passed, 6 skipped**.
- `ruff check` on changed files: clean.

## Residual Risks

- Behavior change: an unqualified name that previously returned a (wrongly merged) result now returns
  `TABLE_AMBIGUOUS`. This is the intended correction; callers should qualify the name. Today `gold` is
  largely empty (memory), so collisions are rare in practice, but the medallion raw/gold layout makes
  this realistic as `gold` is built out.
- The ambiguity check relies on the DB returning a `TABLE_SCHEMA` per row (it does, via the existing
  `INFORMATION_SCHEMA.COLUMNS`/`TABLES` join). No view/base-table distinction is needed for the guard.

## Git Commits

- Branch: `fix/describe-table-cross-schema` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
