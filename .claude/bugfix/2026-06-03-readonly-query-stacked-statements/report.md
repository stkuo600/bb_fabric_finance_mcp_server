# Report — Read-only query stacked-statement bypass (P3, Critical)

## Root Cause

`src/tools/query.py::_is_read_only_query` validated only that the SQL **starts** with
`SELECT`/`WITH` and that no `INSERT|UPDATE|DELETE|MERGE` keyword survived literal/comment
stripping. It never rejected on the `;` statement separator. Because SQL Server/pyodbc executes
every `;`-separated statement in a single batch under the privileged service principal, a payload
such as `SELECT 1; DROP TABLE raw.Fact_Sch1X` passed the "read-only" guard and would have executed
arbitrary DDL/DCL/EXEC, bypassing the write allowlist and confirmation-token flow.

## Fix Description

Added a **single-statement rule** to `_is_read_only_query` as the primary, non-enumerable defense:
after stripping literals and comments, the cleaned SQL is partitioned on the first `;`; if any
non-whitespace content follows, the query is rejected. The existing `_READ_PREFIX`
(must start with `SELECT`/`WITH`) and `_WRITE_DML` (rejects `INSERT/UPDATE/DELETE/MERGE`, incl.
CTE-prefixed writes) checks are retained for the single remaining statement.

A single trailing `;`, and semicolons confined to string literals/comments, remain allowed (they
are not statement separators). The tool's `INVALID_OPERATION` message and docstring were updated to
state the single-statement requirement.

Scope: `src/tools/query.py` only — DB layer, write path, and all tool signatures untouched.

Why the single-statement rule over a keyword blocklist: a blocklist of DDL/DCL verbs
(`DROP/TRUNCATE/ALTER/...`) is brittle and enumerable. Rejecting any second statement closes the
whole class of stacked-statement attacks regardless of the trailing verb.

## Tests Added

In `tests/unit/test_tools/test_query.py` (behavior-level, asserts the `INVALID_OPERATION`
envelope and that `db.execute_query` is never called — no DB dependency):

- `TestStackedStatementsRejected` — DROP, TRUNCATE, ALTER, CREATE, GRANT, EXEC, and INSERT
  stacked behind a leading SELECT, plus a `;`-inside-literal masking case (`SELECT 'a;b' AS c; DROP TABLE t`).
- `TestSingleStatementSemicolonAllowed` — regression guard against over-blocking: trailing `;`,
  trailing `;` with whitespace, semicolon inside a string literal, and a CTE with trailing `;`.

Verification:
- New stacked-statement tests failed before the fix, pass after.
- Full suite: **207 passed, 6 skipped** (the 6 skips are pre-existing async tests unrelated to this change).
- `ruff check` on the changed files: clean.

## Residual Risks

- **P4 shares the same root mechanism** (`;`-stacked statements) on the write-preview path
  (`fabric_preview_write` validates only the parsed table token, not the full SQL, then signs the
  original SQL for later verbatim execution). This fix does **not** cover P4 — it must be addressed
  separately (reject multi-statement preview SQL + re-validate the table at token redemption).
- The guard is a string-level defense. The most robust mitigation remains running read queries under
  a **least-privilege read-only DB principal** instead of the write-capable service principal
  (defense in depth; out of scope for this minimal fix).
- The literal/comment stripper is regex-based; it handles `'...'` (with `''` escape), `[...]`,
  `"..."`, `--`, and `/* */`. Exotic constructs are not a new risk introduced here (the stripper is
  reused as-is), but a true T-SQL parser would be strictly safer.

## Git Commits

- Branch: `fix/readonly-query-stacked-statements`
- (Commit hash recorded on commit — see `git log`.)
