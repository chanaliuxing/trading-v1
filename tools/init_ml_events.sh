#!/bin/bash
# Initialize ml.events table for Meta v3.4
# Usage: ./tools/init_ml_events.sh

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
SCHEMA_FILE="$PROJECT_ROOT/schema/clickhouse/ml/ml_events.sql"

echo "=== Meta v3.4: Initializing ml.events table ==="
echo ""

if [ ! -f "$SCHEMA_FILE" ]; then
    echo "❌ Schema file not found: $SCHEMA_FILE"
    exit 1
fi

echo "✅ Found schema file"
echo "📋 Executing schema..."
echo ""

# Execute schema
cat "$SCHEMA_FILE" | clickhouse-client --multiquery 2>&1

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ ml.events table initialized successfully"
    echo ""
    echo "📊 Verifying schema..."
    clickhouse-client --query "DESCRIBE ml.events" --format Vertical
    echo ""
    echo "🔍 Checking table stats..."
    clickhouse-client --query "SELECT count() as total_events FROM ml.events"
    echo ""
    echo "🎉 Setup complete! You can now:"
    echo "   1. Run integration tests: PYTHONPATH=. venv/bin/pytest tests/integration/test_meta_v34_self_healing_events.py -v"
    echo "   2. Query events: clickhouse-client --query 'SELECT * FROM ml.events LIMIT 10'"
    echo "   3. Monitor self-healing: clickhouse-client --query 'SELECT action_type, count() FROM ml.events WHERE event LIKE \"self_heal.%\" GROUP BY action_type'"
else
    echo ""
    echo "❌ Failed to initialize ml.events table"
    echo "   Check ClickHouse is running: docker ps | grep clickhouse"
    exit 1
fi
