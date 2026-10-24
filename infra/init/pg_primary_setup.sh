#!/bin/bash
# PostgreSQL Primary Replication Setup

set -e

# Create replication user
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Create replication user
    CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD '${POSTGRES_REPLICATION_PASSWORD:-replicator_pass}';

    -- Grant necessary permissions
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO replicator;
EOSQL

# Configure pg_hba.conf for replication
cat >> "$PGDATA/pg_hba.conf" <<-EOF
# Replication connections
host    replication     replicator      0.0.0.0/0               md5
EOF

# Reload configuration
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "SELECT pg_reload_conf();"

echo "PostgreSQL primary replication setup complete"
