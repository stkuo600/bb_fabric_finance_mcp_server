# repro.md

## Symptom

Azure Container Apps revision `fabric-finance-mcp-server--doh62t6` reports
`1/1 Container crashing`. Container exits immediately on startup with:

```
pydantic_settings.exceptions.SettingsError: error parsing value for field
"write_allowlist" from source "EnvSettingsSource"
```

caused by:

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

Stack origin: `src/server.py:19 -> config.load_config() -> FabricSettings()`.

## Environment

- Azure Container App: `fabric-finance-mcp-server` (rg `mcp_resource_group`, East Asia)
- Image: `bluebellmcpregistry.azurecr.io/bb-fabric-finance-mcp-server:latest`
- Python 3.12 (image), project targets 3.11+
- `pydantic-settings>=2.3.0`
- Relevant env var provisioned on the revision:
  - `FABRIC_WRITE_ALLOWLIST="raw.Dim_Entity,raw.Fact_ExchangeRate,raw.Fact_Sch1X,gold.Dim_Entity,raw.Fact_BudgetRate"`

## Reproduction Steps

Locally, with project installed:

```bash
export FABRIC_SERVER="test.datawarehouse.fabric.microsoft.com"
export FABRIC_DATABASE="db"
export FABRIC_CLIENT_ID="cid"
export FABRIC_CLIENT_SECRET="secret"
export FABRIC_TENANT_ID="tid"
export FABRIC_API_KEY="key"
export FABRIC_WRITE_ALLOWLIST="raw.Dim_Entity,raw.Fact_ExchangeRate"
python -m src.server
```

The process aborts with the SettingsError / JSONDecodeError pair shown above.

Automated reproduction added in `tests/unit/test_config.py::test_write_allowlist_from_env_var_csv`.

## Expected vs Actual Behavior

- Expected: `FABRIC_WRITE_ALLOWLIST` treated as a comma-separated list (same as
  the `init`-kwarg path and the pre-existing `@field_validator("write_allowlist", mode="before")`
  already handles), parsed into `["raw.Dim_Entity", "raw.Fact_ExchangeRate", ...]`, server starts.
- Actual: pydantic-settings' `EnvSettingsSource` sees `list[str]` as a "complex"
  type and attempts `json.loads(value)` in `decode_complex_value` BEFORE any
  `mode="before"` field validator runs. The comma string is not JSON, so it
  raises immediately and `load_config()` propagates the failure — the server
  never starts.
