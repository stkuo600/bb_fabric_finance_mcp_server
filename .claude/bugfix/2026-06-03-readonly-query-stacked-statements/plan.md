# Plan — Fix read-only query stacked-statement bypass

## Root Cause

`src/tools/query.py::_is_read_only_query` validates only that (1) the SQL **starts**
with `SELECT`/`WITH` and (2) no `INSERT|UPDATE|DELETE|MERGE` keyword survives literal/comment
stripping. It **never rejects on the `;` statement separator**, and its blocklist omits all
DDL/DCL/EXEC verbs. Since SQL Server/pyodbc executes every `;`-separated statement in one batch,
a payload like `SELECT 1; DROP TABLE raw.Fact_Sch1X` passes both checks and runs the trailing
statement under the privileged service principal.

## Proposed Fix

Harden `_is_read_only_query` with a **single-statement rule** as the primary defense (it
generalizes to *every* trailing statement type, including verbs no blocklist would enumerate):

1. Strip literals and comments (reuse existing `_strip_literals_and_comments`).
2. Split the cleaned text on `;`. If any segment **after the first** contains non-whitespace,
   reject — the SQL contains more than one statement. A single trailing `;` (and semicolons
   confined to literals/comments) remains allowed.
3. Keep the existing `_READ_PREFIX` (must start with SELECT/WITH) and `_WRITE_DML`
   (INSERT/UPDATE/DELETE/MERGE) checks as-is for the single remaining statement — they still
   catch CTE-prefixed writes like `WITH ... INSERT`.

This is minimal: it adds one separator check to the existing function and does not touch the
DB layer, the write path, or any tool signature. A keyword-blocklist expansion is deliberately
NOT relied upon as the primary fix (it is brittle/enumerable); the single-statement rule is the
robust boundary. The error message is updated to mention the single-statement requirement.

## Files to Modify

- `src/tools/query.py` — add statement-separator rejection inside `_is_read_only_query`
  (and update the `INVALID_OPERATION` message / tool docstring wording to note single-statement).

## Test Strategy

Failing tests added in `tests/unit/test_tools/test_query.py` (behavior via the tool's
`INVALID_OPERATION` envelope, no DB):

- `TestStackedStatementsRejected` — DROP / TRUNCATE / ALTER / CREATE / GRANT / EXEC / INSERT
  stacked behind a leading SELECT, plus a `;`-in-literal masking case. Each asserts
  `code == INVALID_OPERATION` **and** `db.execute_query` is never called.
- `TestSingleStatementSemicolonAllowed` — regression guard against over-blocking: trailing
  `;`, trailing `;` with whitespace, semicolon inside a string literal, and CTE with trailing
  `;` must all remain allowed.

All pre-existing query tests must continue to pass.
