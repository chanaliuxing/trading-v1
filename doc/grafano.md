Perfect 🔥 — below is your **complete, final production-ready configuration set** for **Grafana + Loki + Promtail + Tempo** on **macOS (non-Docker, local filesystem)**.

All paths are customized for your username **`postgres`**, every service uses unique ports, retention is 7 days, and everything interconnects cleanly for Grafana dashboards.

---

## 🧩 Folder layout

```bash
~/grafana-stack/
├── loki/
│   └── config.yml
├── promtail/
│   └── config.yml
├── tempo/
│   └── config.yml
├── logs/
│   └── trader.log      # your Python app will write here
└── dashboards/         # optional Grafana JSON imports
```

Create if missing:

```bash
mkdir -p ~/grafana-stack/{loki,promtail,tempo,logs,dashboards}
```

---

## ⚙️ 1️⃣ **Loki – `~/grafana-stack/loki/config.yml`**

```yaml
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096

common:
  instance_addr: 127.0.0.1
  path_prefix: /workspace/grafana-stack/loki
  storage:
    filesystem:
      chunks_directory: /workspace/grafana-stack/loki/chunks
      rules_directory: /workspace/grafana-stack/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: memberlist

memberlist:
  join_members:
    - 127.0.0.1

schema_config:
  configs:
    - from: 2024-01-01
      store: boltdb-shipper
      object_store: filesystem
      schema: v12
      index:
        prefix: index_
        period: 24h

storage_config:
  boltdb_shipper:
    active_index_directory: /workspace/grafana-stack/loki/boltdb-shipper-active
    cache_location: /workspace/grafana-stack/loki/boltdb-shipper-cache
    shared_store: filesystem
  filesystem:
    directory: /workspace/grafana-stack/loki/chunks

limits_config:
  allow_structured_metadata: false
  ingestion_rate_mb: 16
  ingestion_burst_size_mb: 32
  reject_old_samples: true
  reject_old_samples_max_age: 168h
  retention_period: 7d

table_manager:
  retention_deletes_enabled: true
  retention_period: 7d

chunk_store_config:
  max_look_back_period: 168h

compactor:
  working_directory: /workspace/grafana-stack/loki/compactor
  shared_store: filesystem
  retention_enabled: true
  retention_delete_delay: 1h
  retention_delete_worker_count: 2

analytics:
  reporting_enabled: false
```

---

## ⚙️ 2️⃣ **Promtail – `~/grafana-stack/promtail/config.yml`**

```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 9095

positions:
  filename: /workspace/grafana-stack/promtail/positions.yaml

clients:
  - url: http://localhost:3100/loki/api/v1/push

scrape_configs:
  - job_name: macos_system
    static_configs:
      - targets: [localhost]
        labels:
          job: macos_system
          host: trading-workstation
          __path__: /var/log/*.log

  - job_name: loki
    static_configs:
      - targets: [localhost]
        labels:
          job: loki
          __path__: /workspace/grafana-stack/loki/*.log

  - job_name: tempo
    static_configs:
      - targets: [localhost]
        labels:
          job: tempo
          __path__: /workspace/grafana-stack/tempo/*.log

  - job_name: trader
    static_configs:
      - targets: [localhost]
        labels:
          job: trader
          __path__: /workspace/grafana-stack/logs/*.log
```

---

## ⚙️ 3️⃣ **Tempo – `~/grafana-stack/tempo/config.yml`**

```yaml
server:
  http_listen_port: 3200
  grpc_listen_port: 9097

distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:4318
        grpc:
          endpoint: 0.0.0.0:4317

storage:
  trace:
    backend: local
    wal:
      path: /workspace/grafana-stack/tempo/wal
    local:
      path: /workspace/grafana-stack/tempo/blocks

compactor:
  compaction:
    block_retention: 7d

overrides:
  defaults:
    metrics_generator_processors: []
```

---

## ⚙️ 4️⃣ **Grafana – (preinstalled via Homebrew)**

Start service:

```bash
brew services start grafana
```

Access:
👉 [http://localhost:3000](http://localhost:3000)
Login: `admin / admin`

Then configure **data sources** (once):

| Type                  | URL                     | Notes           |
| --------------------- | ----------------------- | --------------- |
| Loki                  | `http://localhost:3100` | Default ✅       |
| Tempo                 | `http://localhost:3200` | Trace view      |
| ClickHouse            | `http://localhost:8123` | your trading DB |
| Prometheus (optional) | none                    | can skip        |

---

## 🚀 Run Everything Together

Open three terminals:

```bash
# 1️⃣ Loki
/opt/homebrew/bin/loki -config.file ~/grafana-stack/loki/config.yml

# 2️⃣ Tempo
/opt/homebrew/bin/tempo -config.file ~/grafana-stack/tempo/config.yml

# 3️⃣ Promtail
/opt/homebrew/bin/promtail -config.file ~/grafana-stack/promtail/config.yml
```

Then start your Python trading system — logs written to
`~/grafana-stack/logs/trader.log` will instantly appear in Grafana.

---

## 🧠 Verify Everything

| Service  | Test Command                                   | Expected              |
| -------- | ---------------------------------------------- | --------------------- |
| Loki     | `curl http://localhost:3100/ready`             | `ready`               |
| Tempo    | `curl http://localhost:3200/ready`             | `ready`               |
| Promtail | Visit `http://localhost:9080`                  | “Promtail is running” |
| Grafana  | [http://localhost:3000](http://localhost:3000) | dashboard login       |

---

Would you like me to generate a **ready-to-import Grafana dashboard JSON** next, showing:

* Live Loki logs
* Tempo traces (clickable TraceID links)
* Trading app error pie-chart (from log fields)
* CPU/memory metrics if you add ClickHouse later?
