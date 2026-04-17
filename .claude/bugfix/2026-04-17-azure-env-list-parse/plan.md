# plan.md

## Root Cause

`FabricSettings.write_allowlist` is declared as `list[str]` in `src/config.py`.
`pydantic-settings`' `EnvSettingsSource` treats `list[*]` as a "complex" type and
calls `json.loads()` on the env var value in `prepare_field_value ->
decode_complex_value` BEFORE any `@field_validator(mode="before")` is invoked.

The Azure revision provides `FABRIC_WRITE_ALLOWLIST` as a comma-separated
string (our documented format, matching the existing `parse_allowlist`
validator). `json.loads("raw.Dim_Entity,...")` raises `JSONDecodeError`,
which `EnvSettingsSource` wraps as `SettingsError`, aborting startup before
`parse_allowlist` can run. The container therefore crashes immediately.

The existing `test_write_allowlist_from_string` unit test never caught this
because it bypasses `EnvSettingsSource` by passing `write_allowlist=...` as
an init kwarg, where pydantic does not attempt JSON decoding.

## Proposed Fix

Annotate the field with `pydantic_settings.NoDecode` so `EnvSettingsSource`
skips JSON decoding, letting `parse_allowlist(mode="before")` handle the
comma-separated string (the behaviour already covered by
`test_write_allowlist_from_string`).

```python
from typing import Annotated
from pydantic_settings import NoDecode, ...

write_allowlist: Annotated[list[str], NoDecode] = []
```

This preserves all three existing input shapes the validator handles:

- List (from JSON config file path / init kwargs) → passthrough
- CSV string (from env var / init kwargs) → split on `,`
- Empty string → `[]`

## Files to Modify

- `src/config.py` — add `NoDecode` import, annotate `write_allowlist`
- `tests/unit/test_config.py` — add regression test covering the env-var CSV path
  (already added as `test_write_allowlist_from_env_var_csv`)

## Test Strategy

1. New failing test `test_write_allowlist_from_env_var_csv` reproduces the
   Azure crash against `EnvSettingsSource`.
2. Apply the `NoDecode` annotation.
3. Re-run the full suite (`pytest`) to confirm:
   - New test passes
   - Existing `test_write_allowlist_from_string`, `test_write_allowlist_from_list`,
     `test_write_allowlist_empty_string`, and `test_load_from_config_file` still pass
4. Manual sanity: rebuild image, exec `python -m src.config` style check, or
   rely on the test suite (sufficient because failure mode is deterministic at
   `FabricSettings()` construction).
