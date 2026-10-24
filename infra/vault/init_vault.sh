#!/bin/sh
# Initialize Vault with trading system secrets

set -e

echo "Waiting for Vault to be ready..."
sleep 5

# Enable KV v2 secrets engine
vault secrets enable -path=trading kv-v2

# Store PostgreSQL secrets
vault kv put trading/postgres \
    username=postgres \
    password="${POSTGRES_PASSWORD:-postgres}" \
    database=test \
    host=postgres-primary \
    port=5432 \
    replication_user=replicator \
    replication_password="${REPLICATION_PASSWORD:-replicator_pass}"

# Store ClickHouse secrets
vault kv put trading/clickhouse \
    user=default \
    password="${CLICKHOUSE_PASSWORD:-}" \
    database=trading \
    host=clickhouse-01 \
    port=8123

# Store Redis secrets
vault kv put trading/redis \
    host=redis-master \
    port=6379 \
    password="${REDIS_PASSWORD:-}"

# Store Kafka secrets
vault kv put trading/kafka \
    bootstrap_servers="kafka-1:9093,kafka-2:9095,kafka-3:9097" \
    schema_registry="http://schema-registry:8081"

# Store IB credentials
vault kv put trading/ib \
    username="${IB_USERNAME:-}" \
    password="${IB_PASSWORD:-}" \
    trading_mode="${IB_TRADING_MODE:-paper}" \
    gateway_host="${IB_GATEWAY_HOST:-localhost}" \
    gateway_port="${IB_GATEWAY_PORT:-4002}"

# Store monitoring credentials
vault kv put trading/monitoring \
    prometheus_url="http://prometheus:9090" \
    grafana_admin_user="${GRAFANA_ADMIN_USER:-admin}" \
    grafana_admin_password="${GRAFANA_ADMIN_PASSWORD:-admin}" \
    pagerduty_service_key="${PAGERDUTY_SERVICE_KEY:-}" \
    slack_webhook_url="${SLACK_WEBHOOK_URL:-}"

# Store API keys
vault kv put trading/api \
    alpha_vantage_key="${ALPHA_VANTAGE_KEY:-}" \
    polygon_key="${POLYGON_KEY:-}" \
    quandl_key="${QUANDL_KEY:-}"

# Create policy for trading services
vault policy write trading-service - <<EOF
path "trading/*" {
  capabilities = ["read", "list"]
}

path "trading/postgres" {
  capabilities = ["read"]
}

path "trading/ib" {
  capabilities = ["read"]
}
EOF

# Enable AppRole auth
vault auth enable approle

# Create AppRole for trading services
vault write auth/approle/role/trading-service \
    token_policies="trading-service" \
    token_ttl=1h \
    token_max_ttl=4h \
    secret_id_ttl=0

# Get RoleID and SecretID
ROLE_ID=$(vault read -field=role_id auth/approle/role/trading-service/role-id)
SECRET_ID=$(vault write -field=secret_id -f auth/approle/role/trading-service/secret-id)

echo "Vault initialized successfully"
echo "RoleID: $ROLE_ID"
echo "SecretID: $SECRET_ID"
echo ""
echo "Store these in your environment:"
echo "export VAULT_ROLE_ID=$ROLE_ID"
echo "export VAULT_SECRET_ID=$SECRET_ID"
