# Report — fabric_describe_table zero-scale numeric precision loss (P7, Medium)

## Root Cause

`fabric_describe_table`'s numeric branch used truthiness
(`elif row.get("NUMERIC_PRECISION") and row.get("NUMERIC_SCALE"):`). `NUMERIC_SCALE` is `0` for
integer-scale numerics (`DECIMAL(18,0)`), and `0` is falsy, so the precision/scale render was skipped and
the column type was reported as bare `decimal`, losing declared precision.

## Fix Description

Changed the branch to test presence: `NUMERIC_PRECISION is not None and NUMERIC_SCALE is not None`.
One-line change in `src/tools/schema.py` (with an explanatory comment). Metadata-only fix; no data path
affected.

## Tests Added

`tests/unit/test_tools/test_schema.py::TestFabricDescribeTable::test_zero_scale_numeric_keeps_precision`
— `DECIMAL(18,0)` → `decimal(18,0)` and `DECIMAL(18,6)` → `decimal(18,6)`.

Verification: failed before, passes after. Schema suite: 13 passed. Full suite: **196 passed, 6 skipped**.
`ruff` clean.

## Residual Risks

- None of note. Char-length and no-precision (e.g. `int`) paths are unchanged and still covered by
  existing tests.

## Git Commits

- Branch: `fix/describe-table-numeric-scale-zero` (off `master`)
- (Commit hash recorded on commit — see `git log`.)
