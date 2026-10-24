#!/bin/bash
#
# Export all Grafana dashboards to JSON files
#
# Purpose:
# - Backup all dashboards as JSON files
# - Enable version control for dashboards
# - Support disaster recovery (rebuild Grafana from JSONs)
#
# Usage:
#   ./scripts/export_all_grafana_dashboards.sh
#

set -e

# Configuration
GRAFANA_URL="http://localhost:3000"
GRAFANA_USER="admin"
GRAFANA_PASS="admin"
EXPORT_DIR="grafana/dashboards/exported"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "================================================================================"
echo "🔄 Grafana Dashboard Exporter"
echo "================================================================================"
echo "📍 Grafana: $GRAFANA_URL"
echo "📂 Export Dir: $EXPORT_DIR"
echo "================================================================================"
echo ""

# Create export directory
mkdir -p "$EXPORT_DIR"

# Get all dashboards
echo "🔍 Fetching dashboard list..."
DASHBOARDS=$(curl -s -u "$GRAFANA_USER:$GRAFANA_PASS" "$GRAFANA_URL/api/search?type=dash-db")

# Count dashboards
DASHBOARD_COUNT=$(echo "$DASHBOARDS" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
echo "   Found $DASHBOARD_COUNT dashboards"
echo ""

# Export each dashboard
EXPORTED=0
FAILED=0

echo "$DASHBOARDS" | python3 -c "
import sys, json

dashboards = json.load(sys.stdin)
for dashboard in dashboards:
    print(f\"{dashboard['uid']}|{dashboard['title']}\")
" | while IFS='|' read -r uid title; do
    # Safe filename (remove special characters)
    safe_title=$(echo "$title" | tr ' ' '_' | tr -cd '[:alnum:]_-')
    filename="$EXPORT_DIR/${uid}_${safe_title}.json"

    # Export dashboard
    HTTP_CODE=$(curl -s -u "$GRAFANA_USER:$GRAFANA_PASS" \
        -w "%{http_code}" \
        -o "$filename" \
        "$GRAFANA_URL/api/dashboards/uid/$uid")

    if [ "$HTTP_CODE" = "200" ]; then
        echo -e "   ${GREEN}✅${NC} $title → ${uid}_${safe_title}.json"
        EXPORTED=$((EXPORTED + 1))
    else
        echo -e "   ${YELLOW}❌${NC} $title (HTTP $HTTP_CODE)"
        FAILED=$((FAILED + 1))
        rm -f "$filename"  # Remove failed export
    fi
done

echo ""
echo "================================================================================"
echo "📊 Export Summary:"
echo "   Total: $DASHBOARD_COUNT"
echo "   ✅ Exported: $EXPORTED"
if [ $FAILED -gt 0 ]; then
    echo "   ❌ Failed: $FAILED"
fi
echo "================================================================================"
echo ""
echo "💡 Tips:"
echo "   - Review exported files: ls -lh $EXPORT_DIR/"
echo "   - Commit to Git: git add $EXPORT_DIR/ && git commit -m 'Export Grafana dashboards'"
echo "   - Restore dashboard: Import JSON in Grafana UI"
echo ""
echo "✅ Done!"
