# Report — Write-preview stacked-statement allowlist bypass (P4, High)

## Root Cause

`fabric_preview_write` checked the write allowlist against only the first table token parsed by
`_parse_write_sql`, but signed the **entire** original SQL into the confirmation token.
`fabric_execute_write` / `fabric_execute_write_batch` execute `payload.sql` verbatim with no
re-validation, and the SQL was never constrained to a single statement. Because pyodbc/SQL Server
runs all `;`-separated statements in one batch under the privileged service principal, a payload like
`INSERT INTO gold.transactions (...) VALUES (...); UPDATE secret.audit_log SET ...` passed preview
(first token `gold.transactions` is allowlisted), minted a valid token, and would execute the trailing
statement against a non-allowlisted table on redemption — defeating the write allowlist.

## Fix Description

Added a reusable pure validator `validate_single_statement(sql)` to `src/tools/_validators.py`: after
stripping string/identifier literals and comments, it rejects (`INVALID_OPERATION`) any non-whitespace
content following the first `;`. A single trailing `;` and semicolons confined to literals/comments
remain allowed.

`fabric_preview_write` now calls `validate_single_statement(sql)` **before** minting the token, so no
confirmation token is ever issued for a multi-statement batch. Since tokens are HMAC-signed and
un-forgeable without `client_secret`, blocking at preview closes the redemption path without changing
the execute/redeem tools or the token format. The tool docstring was updated to state the
single-statement requirement.

This is the same hardening primitive used for P3 (read path), applied to the write-preview path, per
the review's recommendation.

Scope: `src/tools/_validators.py` (new validator) and `src/tools/write.py` (one call + docstring).
The execute/redeem tools, token format, DB layer, and all tool signatures are untouched. The
server-composing tools (`fabric_insert_sch1x_rows`, `fabric_delete_period`) build SQL from validated
parameters and were never affected.

## Tests Added

In `tests/unit/test_tools/test_write.py` (tool-level, mocked db):

- `TestPreviewWriteStackedStatementsRejected` — stacked UPDATE to a non-allowlisted table, stacked
  DROP, stacked second INSERT (even to an allowlisted table), and a `;`-inside-literal masking case.
  Each asserts `INVALID_OPERATION` and that no `confirmation_token` is returned.
- `TestPreviewWriteSingleStatementSemicolonAllowed` — regression guard: a single statement with a
  trailing `;`, and a semicolon inside a string literal, still mint a token.

Verification:
- New stacked-statement tests failed before the fix, pass after.
- Full suite (this branch): **201 passed, 6 skipped** (the 6 skips are pre-existing async tests).
- `ruff check` on changed files: clean.

## Residual Risks

- **Defense at preview only:** the fix blocks token minting for multi-statement SQL. It deliberately
  does not add redemption-time re-validation (unnecessary because un-forgeable tokens can only be
  minted via the now-hardened preview). If a future change ever mints tokens by another path, add a
  single-statement + allowlist re-check at redemption too.
- **Duplicated literal-stripper:** `_validators._strip_sql_literals_and_comments` duplicates
  `query._strip_literals_and_comments` (P3, on a separate branch). After both P3 and P4 merge,
  consolidate into `_validators.py` and have `query.py` import the shared primitive.
- The stripper is regex-based (handles `'...'` with `''` escape, `[...]`, `"..."`, `--`, `/* */`); a
  true T-SQL parser would be strictly safer but is out of scope for this minimal fix.
- Broader hardening (least-privilege read/write DB principals) remains the strongest defense in depth
  and is tracked separately.

## Git Commits

- Branch: `fix/write-preview-stacked-statements` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
