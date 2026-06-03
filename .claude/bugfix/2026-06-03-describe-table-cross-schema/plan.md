# Plan — Fix fabric_describe_table cross-schema column merge

## Root Cause

For an unqualified `table_name`, `schema` is `None`, so the SQL skips the `AND c.TABLE_SCHEMA = ...`
predicate and returns every column of every same-named table across all schemas, ordered by
`ORDINAL_POSITION`. The code then takes `rows[0]["TABLE_SCHEMA"]` as the single resolved schema and
concatenates all rows' columns, silently merging distinct physical tables with no error.

## Proposed Fix

In `fabric_describe_table`, after fetching `rows`, when the call was **unqualified** (`schema is None`)
and the returned rows span more than one distinct `TABLE_SCHEMA`, raise
`ToolInputError(code="TABLE_AMBIGUOUS", ...)` whose message lists the candidate schemas (sorted) so the
caller can re-query with a `schema.table` qualifier. Do not guess a schema; do not merge.

When the rows belong to exactly one schema (qualified call, or unqualified name unique across schemas),
behavior is unchanged.

Minimal: a few lines in `fabric_describe_table` plus documenting the new `TABLE_AMBIGUOUS` code in the
contract. No SQL change required (the existing query already returns `TABLE_SCHEMA` per row); the guard
is applied to the result set. The `(ToolInputError, FabricQueryError)` handler already serializes the
error via `error_envelope`, so the new code flows out uniformly.

## Files to Modify

- `src/tools/schema.py` — ambiguity guard in `fabric_describe_table`; mention multi-schema behavior in
  the docstring.
- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md` — add `TABLE_AMBIGUOUS` to the Error Code
  Reference.

## Test Strategy

Failing tests added in `tests/unit/test_tools/test_schema.py::TestFabricDescribeTable`:

- `test_unqualified_name_ambiguous_across_schemas_rejected` — unqualified name present in `gold` + `raw`
  → `TABLE_AMBIGUOUS`, message lists both schemas, no `columns` returned.
- `test_unqualified_name_single_schema_multiple_columns_ok` — regression guard: multiple columns of one
  table (same schema) are NOT mistaken for ambiguity.

Existing tests (qualified name, unqualified-unique name, not-found, object_type) must still pass.
