# Plan — Fix write-preview stacked-statement allowlist bypass

## Root Cause

`fabric_preview_write` validates the allowlist against only the first table token parsed by
`_parse_write_sql` (`^\s*INSERT\s+INTO\s+(\S+)` / `^\s*UPDATE\s+(\S+)`), but signs the **entire**
original SQL into the confirmation token. `fabric_execute_write` / `fabric_execute_write_batch`
execute `payload.sql` verbatim with no re-validation. The SQL is never constrained to a single
statement, so a `;`-stacked trailing statement (writing a non-allowlisted table, or arbitrary DDL)
passes preview and executes on redemption under the privileged service principal.

## Proposed Fix

Add a **single-statement rule** at preview time — the same hardening primitive used for P3, applied
to the write-preview path. Implement it as a reusable, pure validator in `_validators.py`
(consistent with that module's role and the codebase's seam-extraction convention):

- `validate_single_statement(sql)`: strip string/identifier literals and comments, then reject (with
  `INVALID_OPERATION`) if any non-whitespace content follows the first `;`. A single trailing `;` and
  semicolons confined to literals/comments remain allowed.

Call it in `fabric_preview_write` **before** `make_confirmation_token`, so no token is ever minted for
a multi-statement batch. Because tokens are HMAC-signed (un-forgeable without `client_secret`),
blocking minting at preview closes the redemption path without changing the execute tools.

This is minimal and self-contained: it touches `_validators.py` (new validator) and `write.py`
(one call + docstring wording). It does NOT change the execute/redeem tools, the token format, the
DB layer, or any tool signature.

## Files to Modify

- `src/tools/_validators.py` — add `validate_single_statement` (+ a private literal/comment stripper).
- `src/tools/write.py` — call `validate_single_statement(sql)` in `fabric_preview_write`; note the
  single-statement requirement in the tool docstring.

## Test Strategy

Failing tests added in `tests/unit/test_tools/test_write.py` (tool-level, mocked db):

- `TestPreviewWriteStackedStatementsRejected` — stacked UPDATE to a non-allowlisted table, stacked
  DROP, stacked second INSERT (even to an allowlisted table), and a `;`-in-literal masking case.
  Each asserts `INVALID_OPERATION` and that no `confirmation_token` is returned.
- `TestPreviewWriteSingleStatementSemicolonAllowed` — regression guard: a single statement with a
  trailing `;`, and a semicolon inside a string literal, must still mint a token.

All pre-existing write tests must continue to pass.
