## Symptom

User reports: when two MCP clients connect, `fabric_execute_write` always
returns `TOKEN_INVALID` for confirmation tokens issued by
`fabric_preview_write`. User's own diagnosis:
> "MCP server 端 stateless/多 instance 問題, token 存在不同 instance,
>  無法跨 round-trip 找到"

The same flow works perfectly in single-process unit tests and against a
single-replica deployment.

## Environment

- Server: FastMCP `streamable-http` with `stateless_http=True`,
  `json_response=True` (`src/server.py:35-46`).
- Hosting: Azure Container Apps. Default scaling is multi-replica
  (concurrency-based), so a single client's `preview` and `execute`
  calls can land on different Python processes via the L7 load
  balancer.
- Token storage: module-level `_pending_writes: dict[str, ...]` in
  `src/tools/write.py:24` — lives **only in one Python process's
  memory** and is not shared across replicas or restarts.

## Reproduction Steps

The bug is deterministic at the unit-test level: simulate
"different replica" by creating **two independent FastMCP+write-tool
registrations**. They share the same `client_secret` (the production
env var that is identical across replicas), but each gets its own
fresh `_pending_writes` dict implicitly because we re-import or because
the dict is keyed per-process in production.

```python
# Tool functions from "replica A"
tools_a = _make_write_tools(config=_make_config())
preview_a, _ = tools_a["fabric_preview_write"]

# Tool functions from "replica B" — same config (env vars identical),
# but in production this would be a separate process whose
# _pending_writes is empty.
tools_b = _make_write_tools(config=_make_config())
execute_b, _ = tools_b["fabric_execute_write"]

# Replica A issues the token …
preview = json.loads(preview_a("INSERT INTO gold.transactions (id) VALUES (1)"))
token = preview["confirmation_token"]

# … but replica B has never seen it.
result = json.loads(execute_b(token))
assert result["code"] == "TOKEN_INVALID"   # ← reproduces production bug
```

In production this manifests when `fabric_preview_write` is dispatched
to replica A (writes into A's `_pending_writes`) and the follow-up
`fabric_execute_write` is dispatched to replica B (reads from B's
empty `_pending_writes`). Identical to the symptom even with one
client — multi-client just makes it more likely because more concurrent
requests trigger Container Apps to scale out.

## Expected vs Actual Behavior

**Expected:** A confirmation token issued by `fabric_preview_write` on
any replica must be redeemable by `fabric_execute_write` on any other
replica that shares the same configuration. The protocol is meant to
guard against accidental writes, not to bind the user to a specific
backend process.

**Actual:** Confirmation tokens are UUID4 keys into a process-local
`_pending_writes` dict. Any cross-process redemption fails with
`TOKEN_INVALID`. The same issue also occurs after a server restart
or scale-in event.
