# Dashboard QA Artifacts

Use Grafana's render API (or the image renderer plugin) to capture evidence that
each consolidated dashboard renders with fresh data.

Example script (requires `GRAFANA_TOKEN` with `viewer` + `alerts:create` scope):

```bash
#!/usr/bin/env bash
set -eo pipefail

BASE_URL=${GRAFANA_BASE_URL:-http://localhost:3000}
TOKEN=${GRAFANA_TOKEN:?set GRAFANA_TOKEN}
OUT_DIR=doc/dashboard/qa
mkdir -p "$OUT_DIR"

render() {
  local uid=$1
  local panel=$2
  local name=$3
  curl -sS \
    -H "Authorization: Bearer $TOKEN" \
    "$BASE_URL/render/d-solo/${uid}/_?orgId=1&from=now-6h&to=now&panelId=${panel}" \
    -o "${OUT_DIR}/${name}.png"
}

render infra-platform-health 1 infra_status
render trading-system-ops 20 ops_throughput
render strategy-pipeline 50 strategy_rejections
render financial-market-overview 8 financial_positions
```

After running the script, verify images and reference them from
`doc/dashboard/IMPLEMENTATION_STATUS.md`.
