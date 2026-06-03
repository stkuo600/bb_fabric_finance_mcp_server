# Plan — Render precision for zero-scale numeric types

## Root Cause

`fabric_describe_table`'s numeric branch tests truthiness:
`elif row.get("NUMERIC_PRECISION") and row.get("NUMERIC_SCALE"):`. `NUMERIC_SCALE` is `0` for
integer-scale numerics (`DECIMAL(18,0)`), and `0` is falsy, so the precision/scale render is skipped and
the type is reported as bare `decimal`.

## Proposed Fix

Test for presence, not truthiness: `is not None` on both `NUMERIC_PRECISION` and `NUMERIC_SCALE`.
One-line change in `src/tools/schema.py`.

## Files to Modify

- `src/tools/schema.py` — numeric-type formatting branch.

## Test Strategy

`tests/unit/test_tools/test_schema.py::TestFabricDescribeTable::test_zero_scale_numeric_keeps_precision`
— `DECIMAL(18,0)` → `decimal(18,0)`, `DECIMAL(18,6)` → `decimal(18,6)`. Existing describe tests
(char length, int with no precision) must still pass.
