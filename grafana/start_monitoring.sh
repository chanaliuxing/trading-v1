#!/bin/bash
mkdir -p ~/grafana-stack/logs

echo "Starting Loki..."
/opt/homebrew/bin/loki -config.file ~/grafana-stack/loki/config.yml > ~/grafana-stack/logs/loki.log 2>&1 &

echo "Starting Tempo..."
/opt/homebrew/bin/tempo -config.file ~/grafana-stack/tempo/config.yml > ~/grafana-stack/logs/tempo.log 2>&1 &

echo "Starting Promtail..."
/opt/homebrew/bin/promtail -config.file ~/grafana-stack/promtail/config.yml > ~/grafana-stack/logs/promtail.log 2>&1 &

echo "✅ All services started!"
