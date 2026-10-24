#!/bin/bash
# ============================================================================
# Apply Phase 2 SQL Changes to ClickHouse
# ============================================================================
# This script applies the Phase 2 schema changes:
# - Creates bars_1m_ibkr table
# - Creates bars_1m_agg view
# - Creates bars_1m_current unified view
#
# Usage: ./scripts/apply_phase2_sql.sh [--dry-run]
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ClickHouse connection details (from environment or defaults)
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-9000}"
CH_USER="${CLICKHOUSE_USER:-default}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-}"
CH_DATABASE="${CLICKHOUSE_DATABASE:-trading}"

SQL_FILE="infra/init/sql/phase2_1m_tables_views.sql"
VERIFY_FILE="infra/init/sql/phase2_verify.sql"

# Parse arguments
DRY_RUN=false
if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# Check if SQL file exists
if [[ ! -f "$SQL_FILE" ]]; then
    echo -e "${RED}❌ Error: SQL file not found: $SQL_FILE${NC}"
    exit 1
fi

echo "============================================================================"
echo "Phase 2: ClickHouse Schema Migration"
echo "============================================================================"
echo ""
echo "ClickHouse Connection:"
echo "  Host: $CH_HOST:$CH_PORT"
echo "  Database: $CH_DATABASE"
echo "  User: $CH_USER"
echo ""
echo "SQL File: $SQL_FILE"
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo -e "${YELLOW}🔍 DRY RUN MODE - No changes will be applied${NC}"
    echo ""
    echo "SQL to be executed:"
    echo "============================================================================"
    cat "$SQL_FILE"
    echo "============================================================================"
    exit 0
fi

# Confirm before proceeding
read -p "Apply Phase 2 SQL changes? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
    echo -e "${YELLOW}⏭️  Aborted by user${NC}"
    exit 0
fi

echo ""
echo "📝 Applying SQL changes..."

# Build clickhouse-client command
CH_CMD="clickhouse-client --host=$CH_HOST --port=$CH_PORT --user=$CH_USER"
if [[ -n "$CH_PASSWORD" ]]; then
    CH_CMD="$CH_CMD --password=$CH_PASSWORD"
fi

# Execute SQL file
if $CH_CMD --multiquery < "$SQL_FILE"; then
    echo -e "${GREEN}✅ Phase 2 SQL applied successfully${NC}"
else
    echo -e "${RED}❌ Error applying SQL changes${NC}"
    exit 1
fi

echo ""
echo "🔍 Running verification queries..."
echo ""

# Run verification queries
if [[ -f "$VERIFY_FILE" ]]; then
    $CH_CMD --multiquery < "$VERIFY_FILE"
else
    echo -e "${YELLOW}⚠️  Verification file not found: $VERIFY_FILE${NC}"
    echo "Running basic checks..."

    # Basic verification
    echo ""
    echo "Tables and views:"
    $CH_CMD --query="SHOW TABLES FROM $CH_DATABASE LIKE 'bars_1m%'"

    echo ""
    echo "Row counts:"
    $CH_CMD --query="
        SELECT 'bars_1m' AS table_name, count() AS rows FROM $CH_DATABASE.bars_1m
        UNION ALL
        SELECT 'bars_1m_ibkr', count() FROM $CH_DATABASE.bars_1m_ibkr
        UNION ALL
        SELECT 'bars_1m_agg', count() FROM $CH_DATABASE.bars_1m_agg
        UNION ALL
        SELECT 'bars_1m_current', count() FROM $CH_DATABASE.bars_1m_current
        FORMAT Pretty
    "
fi

echo ""
echo "============================================================================"
echo -e "${GREEN}✅ Phase 2 Complete${NC}"
echo "============================================================================"
echo ""
echo "Next steps:"
echo "  1. Enable bars_1m_ibkr_writer in config.yml (set enabled: true)"
echo "  2. Restart the system to start shadow writing to bars_1m_ibkr"
echo "  3. Monitor for 1-2 hours to verify data flow"
echo "  4. Run verification queries to check data quality"
echo ""
echo "Rollback (if needed):"
echo "  - Set bars_1m_ibkr_writer enabled: false in config.yml"
echo "  - DROP VIEW bars_1m_current; DROP VIEW bars_1m_agg; (optional)"
echo "  - DROP TABLE bars_1m_ibkr; (optional, to remove shadow data)"
echo ""
