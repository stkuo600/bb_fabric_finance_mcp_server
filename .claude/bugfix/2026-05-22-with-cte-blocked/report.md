## Root Cause

`src/tools/query.py:17` defined `_SELECT_PATTERN = re.compile(r"^\s*SELECT\b",
re.IGNORECASE)`. Line 35 rejected any SQL whose first non-whitespace
token was not exactly `SELECT`. CTE-prefixed analytics queries (
`WITH cte AS (...) SELECT ...`) failed this prefix match and were
turned into `INVALID_OPERATION` responses even though they are pure
reads. The spec contract
`specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md:41` said
"Only SELECT statements are accepted", so the implementation was
faithful to the spec — the spec itself was the source of the
over-strict rule.

## Fix Description

The prefix-only check is replaced with a two-step validator in
`src/tools/query.py`:

1. **`_READ_PREFIX`** — `^\s*(?:SELECT|WITH)\b`, case-insensitive.
   The statement must begin with `SELECT` or `WITH`.
2. **Outer-statement write-DML guard** — strip SQL comments and
   string / identifier literals, then `_WRITE_DML.search()` rejects
   if any of `INSERT`/`UPDATE`/`DELETE`/`MERGE` appears at a word
   boundary on the cleaned text.

Why the second step is needed: T-SQL allows
`WITH cte AS (SELECT ...) INSERT|UPDATE|DELETE|MERGE INTO target ...`
which is a write composed onto a CTE. Letting any `WITH`-prefixed
SQL through would re-open the write hole the two-phase confirmation
protocol exists to close.

Why it is sound: CTE bodies in T-SQL grammar can only be SELECT
statements — the body cannot itself contain INSERT/UPDATE/DELETE/MERGE.
So any of those keywords surviving the literal/comment strip must
belong to the outer statement.

Helpers added:

- `_strip_literals_and_comments(sql)` removes `--` line comments,
  `/* */` block comments, `'...'` string literals (with `''` escape),
  `[...]` and `"..."` quoted identifiers. The strip is for keyword
  scanning only; the original `sql` is unchanged before it reaches
  `FabricDatabase.execute_query`.
- `_is_read_only_query(sql)` composes the prefix check and the DML
  guard.

`fabric_execute_query` now calls `_is_read_only_query(sql)`. The
rejection message was updated to mention CTE support so users get a
useful hint instead of a misleading "Only SELECT statements" reply.

## Tests Added

In `tests/unit/test_tools/test_query.py::TestCommonTableExpression`
(11 tests, all green):

**Acceptance (4)** — the WITH-allowing tests that were RED against
the previous code:

- `test_simple_with_cte_then_select_allowed`
- `test_lowercase_with_allowed` (case-insensitivity)
- `test_multiple_ctes_allowed` (`WITH a AS (...), b AS (...) SELECT ...`)
- `test_recursive_cte_allowed`

**Rejection (4)** — security regression guards:

- `test_with_then_insert_rejected`
- `test_with_then_update_rejected`
- `test_with_then_delete_rejected`
- `test_with_then_merge_rejected`

**False-positive guards (3)** — verify the literal/comment stripping:

- `test_insert_keyword_in_string_literal_does_not_falsely_block`
- `test_insert_keyword_in_line_comment_does_not_falsely_block`
- `test_insert_keyword_in_block_comment_does_not_falsely_block`

All 7 pre-existing `TestFabricExecuteQuery` tests still pass.

Verified GREEN against the full suite: `93 passed, 3 skipped`.
`ruff check src/ tests/` is clean.

## Residual Risks

1. **`SELECT ... INTO new_table` is still accepted.** This T-SQL
   construct creates a new permanent / temp table from a SELECT
   result — it is technically a write. The pre-fix code already
   accepted it (starts with `SELECT`, no `INSERT/UPDATE/DELETE/MERGE`
   keyword) and the new code accepts it too. **This is a pre-existing
   gap, not introduced by this fix.** Closing it would require either
   parsing the SQL or adding a regex for `\bSELECT\b[^;]*\bINTO\b`
   guarded by negative lookahead for variable-assignment forms
   (`SELECT @v = col INTO ...` is not the same as
   `SELECT col INTO #t FROM ...`). Recommended as a follow-up bug
   with its own repro / spec discussion if write isolation is being
   tightened.

2. **`EXEC` / `EXECUTE` of stored procedures.** Not addressed by this
   fix and was not addressed before. Stored procedures can perform
   writes. Same pre-existing gap.

3. **Edge cases in the literal/comment stripper.** The regex-based
   stripper assumes well-formed SQL. Pathological inputs (unbalanced
   quotes, nested block comments — which T-SQL does not natively
   support) may strip too much or too little. In every case the
   failure mode is one of:
   - over-strip → cleaned text may falsely contain a write keyword
     fragment → false `INVALID_OPERATION` (safe-by-default).
   - under-strip → keyword inside a literal slips through → false
     `INVALID_OPERATION` (also safe-by-default).
   Either way the user gets a rejection on malformed SQL, never a
   wrongful execution. Acceptable.

4. **No SQL parser dependency added.** Tried-and-true libraries
   (`sqlparse`, `sqlglot`) would handle the above edge cases more
   precisely. Avoided here to keep the change minimal and
   zero-dependency. If false-positives become a real complaint,
   swap in `sqlglot.parse` and inspect the statement's expression
   type.

## Documentation Updated

- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md`
  - Line 13: `sql` parameter description now reads
    "Read-only SQL: a `SELECT`, or a CTE-prefixed `WITH ... SELECT`".
  - Line 41: Behavior bullet expanded to allow CTE reads and
    explicitly call out that CTE-prefixed writes still go to
    `INVALID_OPERATION`.

## Git Commits

Not committed yet — pending user confirmation. Staged for commit:

- `src/tools/query.py`
- `tests/unit/test_tools/test_query.py`
- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md`
- `.claude/bugfix/2026-05-22-with-cte-blocked/{repro,plan,report}.md`
