# ClickHouse Backup & Restore Guide

## Overview
This guide documents the process of restoring ClickHouse databases using the official RESTORE command. This was successfully used to migrate the trading system from an old Mac to a new Mac.

## Prerequisites

### 1. Backup Files Structure
Your backup should have this structure:
```
backup/official_backup/
├── trading_backup/
│   ├── .backup
│   ├── metadata/
│   └── data/
└── lake_backup/
    ├── .backup
    ├── metadata/
    └── data/
```

### 2. ClickHouse Version Compatibility
**CRITICAL**: The ClickHouse version must support the mark file format used in your backup.

- **Check your backup format**: Look at the `.backup` file or data files
  - `.cmrk3` files = Compact Mark v3 (older format)
  - `.cmrk4` files = Compact Mark v4 (newer format, requires ClickHouse 24.1+)

- **Version requirements**:
  - cmrk4 format requires ClickHouse 24.1 or higher
  - If your backup has cmrk4 files, use `clickhouse/clickhouse-server:latest`
  - Using an older version will cause error: `Code: 79. DB::Exception: Unknown mark file extension: '4'`

### 3. ClickHouse Backup Configuration
ClickHouse needs backup disk configuration to use the RESTORE command.

Create `clickhouse-backup-config.xml`:
```xml
<clickhouse>
    <backups>
        <allowed_disk>backups</allowed_disk>
        <allowed_path>/var/lib/clickhouse/backups/</allowed_path>
    </backups>
    <storage_configuration>
        <disks>
            <backups>
                <type>local</type>
                <path>/var/lib/clickhouse/backups/</path>
            </backups>
        </disks>
    </storage_configuration>
</clickhouse>
```

## Docker Compose Configuration

### ClickHouse Service Setup
```yaml
clickhouse:
  image: clickhouse/clickhouse-server:latest  # Use latest for cmrk4 support
  container_name: trading_clickhouse
  restart: unless-stopped
  environment:
    CLICKHOUSE_DB: trading  # Optional: can be set, won't conflict with RESTORE
  ports:
    - "8123:8123"  # HTTP interface
    - "9000:9000"  # Native protocol
  volumes:
    - ch_data:/var/lib/clickhouse
    - ./clickhouse-backup-config.xml:/etc/clickhouse-server/config.d/backup-config.xml
  healthcheck:
    test: ["CMD", "clickhouse-client", "--query", "SELECT 1"]
    interval: 10s
    timeout: 5s
    retries: 5
    start_period: 30s
  mem_limit: 4g
  cpu_shares: 1024
```

**Important notes**:
- Do NOT mount init scripts (like `ch_init.sql`) that create databases/tables
- Init scripts will create databases with different UUIDs, causing conflicts
- Let the RESTORE command handle all database/table creation

## Restore Process

### Step 1: Start ClickHouse
```bash
cd /path/to/your/project
docker-compose up -d clickhouse

# Verify it's running
docker exec trading_clickhouse clickhouse-client --query "SELECT version()"
```

### Step 2: Copy Backup Files to Container
```bash
# Create backup directory
docker exec trading_clickhouse mkdir -p /var/lib/clickhouse/backups

# Copy backup files
docker cp /path/to/backup/trading_backup/. trading_clickhouse:/var/lib/clickhouse/backups/trading_backup/
docker cp /path/to/backup/lake_backup/. trading_clickhouse:/var/lib/clickhouse/backups/lake_backup/

# Set correct ownership
docker exec trading_clickhouse chown -R clickhouse:clickhouse /var/lib/clickhouse/backups/
```

### Step 3: Clean Existing Databases (if any)
If databases already exist with different UUIDs, you must drop them first.

```bash
# Use SYNC to bypass atomic delay and remove UUID mappings immediately
docker exec trading_clickhouse clickhouse-client --query "DROP DATABASE IF EXISTS lake SYNC"
docker exec trading_clickhouse clickhouse-client --query "DROP DATABASE IF EXISTS trading SYNC"
```

### Step 4: Restore Databases
**Order matters**: Restore databases with dependencies first.

```bash
# Restore lake database first (if trading has dependencies on it)
docker exec trading_clickhouse clickhouse-client --query \
  "RESTORE DATABASE lake FROM Disk('backups', 'lake_backup')
   SETTINGS allow_different_database_def=1, allow_different_table_def=1"

# Then restore trading database
docker exec trading_clickhouse clickhouse-client --query \
  "RESTORE DATABASE trading FROM Disk('backups', 'trading_backup')
   SETTINGS allow_different_database_def=1, allow_different_table_def=1"
```

**RESTORE Settings Explained**:
- `allow_different_database_def=1`: Allows database UUID to differ from backup
- `allow_different_table_def=1`: Allows table UUIDs to differ from backup
- These settings are essential when restoring to a fresh ClickHouse instance

### Step 5: Verify Restoration
```bash
# List databases
docker exec trading_clickhouse clickhouse-client --query "SHOW DATABASES"

# Check tables and row counts
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT database, name, total_rows
   FROM system.tables
   WHERE database IN ('trading', 'lake')
   ORDER BY database, name
   FORMAT Pretty"

# Test a simple query
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT count() FROM trading.bars_1m_ibkr"
```

## Troubleshooting

### Error: Unknown mark file extension
```
Code: 79. DB::Exception: Unknown mark file extension: '4'
```

**Cause**: Your backup contains cmrk4 mark files, but your ClickHouse version doesn't support them.

**Solution**: Upgrade ClickHouse to latest version:
```bash
# Update docker-compose.yml
image: clickhouse/clickhouse-server:latest

# Recreate container
docker-compose down clickhouse
docker volume rm infra_ch_data
docker-compose up -d clickhouse
```

### Error: Database UUID conflicts
```
Code: 607. DB::Exception: The database has a different definition
```

**Cause**: Database already exists with a different UUID.

**Solution**: Drop databases with SYNC before restoring:
```bash
docker exec trading_clickhouse clickhouse-client --query "DROP DATABASE IF EXISTS lake SYNC"
docker exec trading_clickhouse clickhouse-client --query "DROP DATABASE IF EXISTS trading SYNC"
```

### Error: Table doesn't exist (dependency issue)
```
Code: 60. DB::Exception: Table lake.ticks_trades doesn't exist
```

**Cause**: Restoring in wrong order. Trading database has materialized views depending on lake database.

**Solution**: Always restore dependency databases first (lake before trading).

### Error: Permission denied
```
Code: 76. DB::ErrnoException: Cannot open file, errno: 13, strerror: Permission denied
```

**Cause**: Backup files have incorrect ownership.

**Solution**: Fix ownership:
```bash
docker exec trading_clickhouse chown -R clickhouse:clickhouse /var/lib/clickhouse/backups/
```

## Creating Backups (for future reference)

To create backups that can be restored using this process:

```bash
# Backup a single database
docker exec trading_clickhouse clickhouse-client --query \
  "BACKUP DATABASE trading TO Disk('backups', 'trading_backup')"

# Backup multiple databases
docker exec trading_clickhouse clickhouse-client --query \
  "BACKUP DATABASE lake TO Disk('backups', 'lake_backup')"

# Copy backups from container
docker cp trading_clickhouse:/var/lib/clickhouse/backups/trading_backup/. /path/to/save/trading_backup/
docker cp trading_clickhouse:/var/lib/clickhouse/backups/lake_backup/. /path/to/save/lake_backup/
```

## Key Lessons Learned

1. **Always use latest ClickHouse version for restores** - This ensures compatibility with newer mark file formats
2. **Never use init scripts with RESTORE** - They create UUID conflicts
3. **Use SYNC drops** - Regular DROP has atomic delay and doesn't clear UUID mappings
4. **Order matters** - Restore dependency databases first
5. **Use official RESTORE command** - Don't use third-party tools or manual file copying
6. **Check backup format first** - Look for cmrk3/cmrk4 files to determine version requirements
7. **Allow different definitions** - Always use `allow_different_database_def=1` and `allow_different_table_def=1` settings

## Version Information

This guide was tested with:
- Source ClickHouse: Unknown version (created cmrk4 backups)
- Target ClickHouse: 25.9.3.48 (clickhouse/clickhouse-server:latest as of 2025-10)
- Docker Compose: v2.x
- macOS: Darwin 24.6.0

## References

- ClickHouse BACKUP/RESTORE documentation: https://clickhouse.com/docs/en/operations/backup
- ClickHouse mark file formats: https://clickhouse.com/docs/en/operations/settings/merge-tree-settings#min_compress_block_size
