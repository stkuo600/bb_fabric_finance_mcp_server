## Root Cause

`src/tools/query.py:17` defines
`_SELECT_PATTERN = re.compile(r"^\s*SELECT\b", re.IGNORECASE)`.
Line 35 rejects any SQL whose first non-whitespace token is not
`SELECT`. CTE-prefixed queries begin with `WITH`, so they fail the
prefix match and are rejected with `INVALID_OPERATION` even when
the eventual operation is a pure read.

The spec faithfully captures this restriction:
`specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md:41` —
"Only SELECT statements are accepted." The spec phrasing predates
considering CTEs and needs to be relaxed alongside the code.

## Proposed Fix

Replace the prefix-only check with a two-step validator:

1. **Prefix check** — the statement must start with `SELECT` or `WITH`:
   ```python
   _READ_PREFIX = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)
   ```

2. **Outer-statement DML guard** — strip SQL comments and string /
   identifier literals, then reject if any of
   `INSERT`/`UPDATE`/`DELETE`/`MERGE` appears at word boundary. This
   blocks the T-SQL `WITH cte AS (...) INSERT|UPDATE|DELETE|MERGE`
   composition and keeps writes flowing through the
   `fabric_preview_write` / `fabric_execute_write` confirmation path.

   The CTE body grammar itself only permits SELECT, so a write keyword
   stripped of literals/comments unambiguously signals a write at the
   outer statement.

Helper functions added in `src/tools/query.py`:

```python
def _strip_literals_and_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)                  # line comments
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL) # block comments
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)           # 'string'
    sql = re.sub(r"\[[^\]]*\]", "[]", sql)               # [identifier]
    sql = re.sub(r'"[^"]*"', '""', sql)                  # "identifier"
    return sql


def _is_read_only_query(sql: str) -> bool:
    if not _READ_PREFIX.match(sql):
        return False
    cleaned = _strip_literals_and_comments(sql)
    return _WRITE_DML.search(cleaned) is None
```

`fabric_execute_query` calls `_is_read_only_query(sql)` in place of
the direct `_SELECT_PATTERN.match(sql)` check. The error message is
updated to mention CTE support.

### Out of scope

- `SELECT ... INTO new_table FROM ...` — creates a new table (a write).
  The previous code already accepted this (it starts with SELECT); the
  new code accepts it too. This is an existing, separate gap and is
  not the bug under investigation. Flagged in `report.md` for follow-up.
- Detecting `EXEC` / `EXECUTE` of stored procedures that perform
  writes. Same situation: not introduced by this fix.
- Robust SQL parsing via a library (`sqlparse` / `sqlglot`). Avoided
  to keep the change zero-dependency and reviewable.

## Files to Modify

- `src/tools/query.py`
  - Rename `_SELECT_PATTERN` → `_READ_PREFIX` and broaden to
    `SELECT|WITH`.
  - Add `_WRITE_DML`, `_strip_literals_and_comments`,
    `_is_read_only_query`.
  - `fabric_execute_query`: replace the prefix check with
    `_is_read_only_query(sql)`; update the rejection message.

- `tests/unit/test_tools/test_query.py`
  - New `TestCommonTableExpression` class — already added in the RED
    phase. Covers WITH→SELECT (lowercase, multiple, recursive),
    WITH→INSERT/UPDATE/DELETE/MERGE rejection, and false-positive
    guards for write keywords inside string literals / line comments
    / block comments.

- `specs/001-fabric-sql-mcp-server/contracts/mcp-tools.md`
  - Line 13 ("SQL SELECT statement…") and line 41 ("Only SELECT
    statements are accepted…") expanded to include CTE-prefixed
    read-only queries.

## Test Strategy

The `TestCommonTableExpression` suite (11 tests) added in the RED
phase covers:

- **Acceptance** (4 tests): simple CTE→SELECT, lowercase `with`,
  multiple CTEs joined, recursive CTE. All four are RED against the
  current code and prove the bug.
- **Rejection** (4 tests): WITH→INSERT, WITH→UPDATE, WITH→DELETE,
  WITH→MERGE. Pass today (because all `WITH` is blocked) and must
  continue to pass after the fix — these are the security regression
  guards.
- **False-positive guards** (3 tests): write keywords appearing only
  inside a string literal, line comment, or block comment. Pass
  today (because the query starts with SELECT so the WITH branch is
  never taken) and must continue to pass after the fix — they verify
  the literal/comment stripping works.

Existing tests in `TestFabricExecuteQuery` must continue to pass
without modification.
