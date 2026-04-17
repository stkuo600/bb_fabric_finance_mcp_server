# report.md

## Root Cause

`FabricSettings.write_allowlist` was typed `list[str]`. `pydantic-settings`'
`EnvSettingsSource` treats any `list[*]` as "complex" and calls `json.loads()`
on the raw env-var string inside `prepare_field_value` BEFORE any
`@field_validator(mode="before")` runs. The Azure Container App supplies
`FABRIC_WRITE_ALLOWLIST` as a comma-separated list (our documented / validated
format), which is not valid JSON — `json.loads` raises `JSONDecodeError`,
pydantic-settings wraps it as `SettingsError`, and `src/server.py:19`
aborts before the server can start, producing "1/1 Container crashing" in
the Azure revision.

The bug escaped unit tests because `test_write_allowlist_from_string`
passes the CSV string through the init-kwarg path, which bypasses the
`EnvSettingsSource` JSON-decode step.

## Fix Description

`src/config.py`:

- Import `Annotated` and `pydantic_settings.NoDecode`.
- Change `write_allowlist: list[str] = []` to
  `write_allowlist: Annotated[list[str], NoDecode] = []`.

`NoDecode` is the documented pydantic-settings (>=2.2) idiom to opt a single
field out of JSON decoding. The existing
`@field_validator("write_allowlist", mode="before")` now receives the raw
string and splits on `,`, matching the behaviour the init-kwarg path already had.

No behavior change for the JSON config-file path (values still arrive as lists),
and no change to defaults or other fields.

## Tests Added

`tests/unit/test_config.py::TestFabricSettings::test_write_allowlist_from_env_var_csv`

- Injects `FABRIC_WRITE_ALLOWLIST="raw.Dim_Entity,raw.Fact_ExchangeRate, gold.Dim_Entity"` via `os.environ`.
- Constructs `FabricSettings()` with no init kwargs, so `EnvSettingsSource` is exercised.
- Asserts the parsed list, including trimmed whitespace.
- Fails on the pre-fix code with the exact `SettingsError` seen in Azure; passes on the fixed code.

Full suite after fix: `74 passed, 3 skipped` (skips are integration tests gated on a live Fabric connection), `ruff check` clean.

## Residual Risks

- Low. `NoDecode` only changes env-var decoding for this single field. The
  config-file source doesn't touch `decode_complex_value`, so JSON arrays in
  `config.json` still work.
- If someone later switches `FABRIC_WRITE_ALLOWLIST` to JSON (e.g.
  `'["a","b"]'`), the validator's `.split(",")` would treat it as a single
  malformed item — but no existing caller does this, and the documented
  format in `config.example.json` is the CSV form.
- Secret hygiene: the live Azure revision's env vars (incl. `FABRIC_CLIENT_SECRET`
  and `FABRIC_API_KEY`) were surfaced to this diagnostic session via `az containerapp show`.
  Consider rotating if the session transcript is sensitive. Not a defect in the fix.

## Git Commits

Pending — to be created by the user (or the redeploy step below) once the fix is reviewed.
Suggested message:

```
fix(config): skip JSON decoding for write_allowlist env var

pydantic-settings treats list[str] as complex and json-loads env values
before @field_validator runs, crashing the Azure container on a CSV
FABRIC_WRITE_ALLOWLIST. Annotate with NoDecode so parse_allowlist handles
the split, matching the init-kwarg path.

Regression test: test_write_allowlist_from_env_var_csv.
```
