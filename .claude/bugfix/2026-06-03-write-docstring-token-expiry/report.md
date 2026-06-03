# Report — fabric_execute_write stale 5-minute expiry docstring (P11 + P13, Low)

## Root Cause

`fabric_execute_write`'s docstring hard-coded "(5-minute validity)", while the real expiry is
`config.write_token_expiry_minutes` (default 15, range 1–60, `FABRIC_WRITE_TOKEN_EXPIRY_MINUTES`). The
docstring is the LLM-facing tool contract, so the wrong figure could mislead retry/timing decisions.

## Fix Description

Updated the docstring to reference the configurable expiry (default 15, via
`FABRIC_WRITE_TOKEN_EXPIRY_MINUTES`) instead of the hard-coded "5-minute". One docstring change covering
both P11 and P13 (same docstring). No runtime behavior change — enforcement already uses the embedded
`exp`.

## Tests Added

`test_write.py::TestFabricExecuteWrite::test_docstring_does_not_claim_fixed_five_minute_expiry` — the
registered tool's docstring no longer says "5-minute" and references the configurable expiry.

Verification: failed before, passes after. write suite: 53 passed. Full suite: **196 passed, 6 skipped**.
`ruff` clean.

## Residual Risks

- None. Documentation-only change; runtime enforcement unchanged.

## Git Commits

- Branch: `fix/write-docstring-token-expiry` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
