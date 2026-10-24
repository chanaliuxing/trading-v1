import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from libs.db.clickhouse import ClickHouseDB

db = ClickHouseDB()
query = "SELECT count() FROM trading.features_1m WHERE ts >= '2025-09-29 00:00:00' AND ts < '2025-11-29 00:00:00'"
result = db.query(query)
print(f"ClickHouse query result: {result}")