# Repro — Auth failure surfaces raw MSAL/AADSTS error_description to the client

## Symptom

On token-acquisition failure, `FabricAuth.get_token` embeds the raw MSAL `result["error_description"]`
and `result["error"]` into an `ErrorResponse` and raises `RuntimeError(error.model_dump_json())`. This
`RuntimeError` is not caught by the tools' `(ToolInputError, FabricQueryError)` handlers, so it
propagates; FastMCP wraps it and returns its `str()` to the client. MSAL/AADSTS diagnostics
(e.g. `AADSTS7000215`, correlation/trace IDs, timestamps, sometimes tenant/app GUIDs) reach the MCP
client verbatim — reconnaissance-grade identity information disclosure.

Identified by the deployment-aware codebase review as **P12 (Low, 3/3 votes)**.

## Environment

- Repo: `bb_fabric_finance_mcp_server`, branch `fix/auth-error-scrub` (off `master`)
- File: `src/auth.py` — `get_token` failure path (lines ~47-52)
- It does NOT leak `client_secret`; the leak is AADSTS diagnostic text + trace/correlation IDs.
- Deterministic with a mocked MSAL app.

## Reproduction Steps

```python
from unittest.mock import patch
from src.auth import FabricAuth

with patch("src.auth.msal.ConfidentialClientApplication"):
    a = FabricAuth(tenant_id="t", client_id="c", client_secret="s")
a._app.acquire_token_silent.return_value = None
a._app.acquire_token_for_client.return_value = {
    "error": "invalid_client",
    "error_description": "AADSTS7000215: Invalid client secret. Trace ID: abc-123 Correlation ID: def-456",
}
try:
    a.get_token()
except RuntimeError as e:
    print(str(e))
```

### Observed output (2026-06-03)

```
{"code":"AUTH_FAILED","message":"Authentication failed: AADSTS7000215: Invalid client secret. Trace ID: abc-123 Correlation ID: def-456","details":"invalid_client"}
leaks AADSTS? True | trace id? True
```

## Expected vs Actual Behavior

| Aspect | Expected | Actual |
|---|---|---|
| Client-facing message | generic ("authentication to upstream failed"), code `AUTH_FAILED` | embeds raw AADSTS + trace/correlation IDs |
| Server-side log | full MSAL `error` + `error_description` retained | only the failure is implied; raw text only on the wire |

**Why:** the failure message is built from `result.get("error_description")` and `result.get("error")`.

## Test feasibility

Stable unit test with a mocked MSAL app: assert the raised `RuntimeError` no longer contains the AADSTS
text / trace IDs, still carries code `AUTH_FAILED`, and that the full MSAL detail is logged server-side.
