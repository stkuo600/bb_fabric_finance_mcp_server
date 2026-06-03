# Deployment — Azure Container Apps

Production runs on **Azure Container Apps**. This directory holds the real,
verified deployment convention for this repo. (The older
`docs/superpowers/plans/2026-04-03-deploy-azure-container-apps.md` is a planning
draft whose placeholder names do **not** match production — use this instead.)

## Actual resources (source of truth: `infra/config.env`)

| Thing | Value |
|-------|-------|
| Resource group | `mcp_resource_group` |
| Region | `eastasia` |
| ACR | `bluebellmcpregistry` (`bluebellmcpregistry.azurecr.io`) |
| Container Apps env | `managedEnvironment-mcpresourcegrou-b4c5` |
| Container App | `fabric-finance-mcp-server` |
| Image repository | `bb-fabric-finance-mcp-server` |
| **Image tag convention** | **git short SHA** (e.g. `34e5864`) |
| Ingress / target port | external, `8000` |
| Scale | min `0`, max `10` |
| Resources | `0.5` CPU, `1Gi` memory |
| App URL | `https://fabric-finance-mcp-server.politewave-d0846bec.eastasia.azurecontainerapps.io` |

## Routine deploy (after merging code to `master`)

```bash
az account show                 # ensure logged in to the HSO Azure subscription
bash infra/update.sh            # builds image tagged with the current git short SHA, then updates the app
# or pin a tag:
bash infra/update.sh 34e5864
```

`update.sh` only swaps the image; all env vars / secrets are preserved.
Verify after: `curl -s https://<app-url>/health` → `{"status":"ok"}`.

## Environment variables (set on the Container App)

`FABRIC_SERVER`, `FABRIC_DATABASE`, `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`,
`FABRIC_TENANT_ID`, `FABRIC_API_KEY`, `FABRIC_WRITE_ALLOWLIST`, `FABRIC_MAX_ROWS`,
`FABRIC_PORT`, `FABRIC_TOKEN_SIGNING_KEY`.

- **`FABRIC_TOKEN_SIGNING_KEY`** is injected as a Container App *secret*
  (`secretref:fabric-token-signing-key`). It signs write confirmation tokens
  independently of the rotated AAD `client_secret` (see
  `.claude/bugfix/2026-06-03-token-signing-key-rotation`). Keep it stable across
  replicas; rotating it invalidates in-flight confirmation tokens (≤15 min).
  Set / rotate it with:
  ```bash
  KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
  az containerapp secret set -n fabric-finance-mcp-server -g mcp_resource_group \
    --secrets fabric-token-signing-key="$KEY"
  az containerapp update -n fabric-finance-mcp-server -g mcp_resource_group \
    --set-env-vars FABRIC_TOKEN_SIGNING_KEY=secretref:fabric-token-signing-key
  ```

## Known gotcha: Windows `az acr build` log crash

On a Windows console using a non-UTF-8 code page (e.g. cp950), `az acr build`'s
client-side **log streamer** can crash with
`UnicodeEncodeError: 'cp950' codec can't encode character '━'`. The cloud
build still **succeeds** — only the local log display dies. `update.sh`/`deploy.sh`
tolerate this and verify the image tag with `az acr repository show-tags` instead
of trusting the build command's exit code. To avoid the crash entirely, run from a
UTF-8 console (`chcp 65001`) or PowerShell with
`[Console]::OutputEncoding=[Text.Encoding]::UTF8`.

## Cold start & the first write after idle

The app runs with `min-replicas = 0` (see `config.env`), so after a period of no
traffic it scales to zero and the next request cold-starts a replica. The first
**write** that lands on a freshly woken replica can fail with
`WRITE_STATE_UNKNOWN` because the Fabric connection pool is not yet warm and the
initial connection gets dropped during the statement.

This is **expected, not a bug**: the write path is deliberately *fail-safe* — on a
connection drop mid-write it does **not** auto-retry (a non-idempotent INSERT/UPDATE
could otherwise be applied twice), and instead returns `WRITE_STATE_UNKNOWN` so the
caller can verify state and retry explicitly. The retry then succeeds against the
now-warm connection. Verified live 2026-06-03: the first `fabric_execute_write` after
an idle period returned `WRITE_STATE_UNKNOWN`; an immediate re-run returned
`affected_rows: 0` cleanly.

Operational guidance:
- **Reads** are unaffected in practice (idempotent — they auto-retry on a dropped
  connection).
- **Writes**: if a caller's *first* write after idle returns `WRITE_STATE_UNKNOWN`,
  it should verify the table state (the statement almost certainly did **not**
  commit) and simply retry once; the second attempt hits a warm connection.
- To eliminate cold starts entirely (at the cost of one always-on replica), set
  `MIN_REPLICAS=1` in `config.env` and redeploy, or:
  ```bash
  az containerapp update -n fabric-finance-mcp-server -g mcp_resource_group --min-replicas 1
  ```

## First-time / disaster recovery

`infra/deploy.sh` recreates everything (RG, ACR, env, app, secrets, probes) from
exported `FABRIC_*` env vars. Not needed for routine updates.

## Useful operations

```bash
# Live logs
az containerapp logs show -n fabric-finance-mcp-server -g mcp_resource_group --type console --follow
# Active revision / image
az containerapp show -n fabric-finance-mcp-server -g mcp_resource_group \
  --query "{image:properties.template.containers[0].image,revision:properties.latestRevisionName}" -o table
# Roll back to a previous SHA
bash infra/update.sh <previous-short-sha>
```
