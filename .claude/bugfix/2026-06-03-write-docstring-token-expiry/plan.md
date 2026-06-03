# Plan — Correct fabric_execute_write expiry docstring

## Root Cause

`fabric_execute_write`'s docstring hard-codes "(5-minute validity)", but the real expiry is
`config.write_token_expiry_minutes` (default 15, range 1–60, `FABRIC_WRITE_TOKEN_EXPIRY_MINUTES`).

## Proposed Fix

Update the docstring to reference the configurable expiry instead of "5-minute". One-line docstring
change in `src/tools/write.py` (covers both P11 and P13 — same docstring).

## Files to Modify

- `src/tools/write.py` — `fabric_execute_write` docstring.

## Test Strategy

`test_write.py::test_docstring_does_not_claim_fixed_five_minute_expiry` — docstring no longer says
"5-minute" and references the configurable expiry. No runtime behavior change.
