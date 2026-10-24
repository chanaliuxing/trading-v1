# Trading System Migration Summary

## Overview
Successfully migrated trading system infrastructure (Kafka, Redis, ClickHouse) from old Mac to new Mac on 2025-10-16.

## What Was Done

### 1. Kafka Migration: Zookeeper → KRaft Mode
**Why**: Zookeeper is deprecated, KRaft is the modern standard with better performance and simpler architecture.

**Changes**:
- Replaced `confluentinc/cp-kafka` + `confluentinc/cp-zookeeper` with `apache/kafka:latest`
- Removed Zookeeper service entirely
- Configured KRaft combined mode (broker + controller in single node)
- Updated Schema Registry connection from `zookeeper:2181` to `kafka:9092`

**Files Modified**: `infra/compose.yml` lines 21-69

**Result**: Single Kafka service running on port 9092, no Zookeeper dependency

### 2. ClickHouse Restore
**Why**: Needed to restore trading data from backup on old Mac.

**Challenges Encountered**:
1. Initial version mismatch: Backup contained cmrk4 mark files, but ClickHouse 23.3 doesn't support them
2. UUID conflicts from init scripts pre-creating databases
3. Database dependency ordering (lake must be restored before trading)

**Solution**:
- Upgraded ClickHouse from `23.3` to `latest` (v25.9.3.48) to support cmrk4 format
- Created backup configuration file: `infra/clickhouse-backup-config.xml`
- Removed init script mount to prevent UUID conflicts
- Used official `RESTORE DATABASE` command with proper settings
- Restored lake database first (dependency), then trading database

**Files Modified**:
- `infra/compose.yml` lines 37-60
- Created: `infra/clickhouse-backup-config.xml`

**Result**:
- 2 databases restored: lake (10 tables, 6M+ rows) and trading (93 tables, 3.7M+ rows)
- All data intact, materialized views working

### 3. Redis Setup
**Why**: Standard Redis setup for caching and pub/sub.

**Changes**: Already configured in compose.yml, just needed to start

**Result**: Redis running on port 6379

## Infrastructure Status

All services running and healthy:

| Service | Port(s) | Status | Version |
|---------|---------|--------|---------|
| Kafka | 9092 | ✓ Running | apache/kafka:latest |
| Schema Registry | 8081 | ✓ Running | cp-schema-registry:latest |
| Redis | 6379 | ✓ Running | redis:alpine |
| ClickHouse | 8123, 9000 | ✓ Running | 25.9.3.48 |

## Key Technical Decisions

### 1. Why Apache Kafka instead of Confluent?
- Better KRaft mode support out of the box
- Simpler configuration for single-node setup
- Official Apache image has faster updates

### 2. Why upgrade to ClickHouse latest?
- Old version (23.3) didn't support cmrk4 mark file format
- Backup was created with newer version containing cmrk4 files
- Using latest ensures forward compatibility with future backups

### 3. Why remove ClickHouse init scripts?
- Init scripts create databases with their own UUIDs
- RESTORE command brings databases with different UUIDs from backup
- UUID conflicts cause restore failures
- Solution: Let RESTORE create everything from scratch

### 4. Why use SYNC drops?
- Regular `DROP DATABASE` has atomic delay
- SYNC bypasses delay and removes UUID mappings immediately
- Prevents UUID conflicts during restore

## Files Changed

### Modified Files
```
infra/compose.yml
- Lines 21-69: Kafka service (Zookeeper → KRaft)
- Lines 37-60: ClickHouse service (version upgrade, backup config mount)
- Lines 79-105: Removed Zookeeper service entirely
- Volumes section: Removed zk_data and zk_logs
```

### Created Files
```
infra/clickhouse-backup-config.xml (new)
- Configures backup disk for RESTORE command
- Maps /var/lib/clickhouse/backups/ path

CLICKHOUSE_RESTORE_GUIDE.md (new)
- Complete guide for ClickHouse backup/restore
- Troubleshooting common errors
- Version compatibility information

KAFKA_MIGRATION_GUIDE.md (new)
- Complete guide for Zookeeper → KRaft migration
- Configuration explanations
- Multi-node setup examples

SYSTEM_MIGRATION_SUMMARY.md (this file)
- High-level summary of what was done
- Key decisions and rationale
```

## Commands Used

### Kafka Migration
```bash
# Generate cluster ID
docker run --rm apache/kafka:latest /opt/kafka/bin/kafka-storage.sh random-uuid

# Start Kafka
docker-compose up -d kafka

# Verify
docker exec trading_kafka /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092
```

### ClickHouse Restore
```bash
# Upgrade and clean start
docker-compose down clickhouse
docker volume rm infra_ch_data
docker-compose up -d clickhouse

# Copy backups
docker exec trading_clickhouse mkdir -p /var/lib/clickhouse/backups
docker cp backup/official_backup/trading_backup/. trading_clickhouse:/var/lib/clickhouse/backups/trading_backup/
docker cp backup/official_backup/lake_backup/. trading_clickhouse:/var/lib/clickhouse/backups/lake_backup/
docker exec trading_clickhouse chown -R clickhouse:clickhouse /var/lib/clickhouse/backups/

# Restore databases
docker exec trading_clickhouse clickhouse-client --query \
  "RESTORE DATABASE lake FROM Disk('backups', 'lake_backup')
   SETTINGS allow_different_database_def=1, allow_different_table_def=1"

docker exec trading_clickhouse clickhouse-client --query \
  "RESTORE DATABASE trading FROM Disk('backups', 'trading_backup')
   SETTINGS allow_different_database_def=1, allow_different_table_def=1"

# Verify
docker exec trading_clickhouse clickhouse-client --query \
  "SELECT database, name, total_rows
   FROM system.tables
   WHERE database IN ('trading', 'lake')
   ORDER BY database, name"
```

## Lessons Learned

### Critical Issues and Solutions

1. **Mark File Format Compatibility**
   - Problem: cmrk4 files not supported by old ClickHouse
   - Solution: Always use latest ClickHouse version for restores
   - Prevention: Document ClickHouse version when creating backups

2. **UUID Conflicts**
   - Problem: Init scripts create databases with different UUIDs
   - Solution: Remove init scripts, use RESTORE exclusively
   - Prevention: Never mix init scripts with RESTORE command

3. **Database Dependencies**
   - Problem: Trading database has materialized views referencing lake tables
   - Solution: Restore in dependency order (lake first)
   - Prevention: Document database dependencies

4. **Kafka Healthcheck Path**
   - Problem: Script path incorrect in apache/kafka image
   - Solution: Use full path `/opt/kafka/bin/...`
   - Prevention: Always test healthchecks after image changes

### Best Practices Established

1. **Version Management**
   - Use latest versions for new setups to avoid compatibility issues
   - Pin versions in production after testing

2. **Backup Strategy**
   - Use official ClickHouse BACKUP/RESTORE commands
   - Store backups with version information
   - Test restore process before needing it

3. **Infrastructure as Code**
   - Keep docker-compose.yml in git
   - Document all configuration changes
   - Create guides for future migrations

4. **Clean Migration**
   - For development environments, clean slate is better than in-place upgrade
   - Remove old volumes to avoid state conflicts
   - Verify each service independently before starting dependent services

## Verification Checklist

- [x] Kafka running and accepting connections on port 9092
- [x] Kafka topics can be created and messages produced/consumed
- [x] Schema Registry connected to Kafka and accessible on port 8081
- [x] Redis running and accepting connections on port 6379
- [x] ClickHouse running on ports 8123 (HTTP) and 9000 (Native)
- [x] Lake database restored with all 10 tables
- [x] Trading database restored with all 93 tables
- [x] Row counts match expected data volume
- [x] Materialized views created successfully
- [x] All services have healthchecks passing
- [x] All services have restart policies configured

## Next Steps

### For Application Integration
1. Update application connection strings if needed:
   - Kafka: `localhost:9092` (unchanged)
   - ClickHouse: `localhost:8123` or `localhost:9000` (unchanged)
   - Redis: `localhost:6379` (unchanged)

2. Test application connectivity:
   - Verify Kafka producers can send messages
   - Verify ClickHouse queries return expected data
   - Verify Redis cache operations work

3. Monitor services:
   - Check `docker stats` for resource usage
   - Review logs for any errors: `docker-compose logs -f`
   - Set up monitoring/alerting if needed

### For Future Migrations
1. Create regular backups:
   ```bash
   # ClickHouse
   docker exec trading_clickhouse clickhouse-client --query \
     "BACKUP DATABASE trading TO Disk('backups', 'trading_backup_$(date +%Y%m%d)')"

   # Copy out
   docker cp trading_clickhouse:/var/lib/clickhouse/backups/. /backup/location/
   ```

2. Document any schema changes in git

3. Keep these guides updated with new learnings

## Time Spent

- Initial Kafka migration: ~30 minutes
- ClickHouse troubleshooting and restore: ~2 hours
  - Version compatibility investigation: ~45 minutes
  - UUID conflict resolution: ~30 minutes
  - Final restore and verification: ~45 minutes
- Documentation creation: ~1 hour

**Total**: ~3.5 hours

## References

- [CLICKHOUSE_RESTORE_GUIDE.md](./CLICKHOUSE_RESTORE_GUIDE.md) - Detailed ClickHouse restore instructions
- [KAFKA_MIGRATION_GUIDE.md](./KAFKA_MIGRATION_GUIDE.md) - Detailed Kafka KRaft migration guide
- [MIGRATION_GUIDE.md](./MIGRATION_GUIDE.md) - Original migration plan
- [infra/compose.yml](./infra/compose.yml) - Docker Compose configuration
- [infra/clickhouse-backup-config.xml](./infra/clickhouse-backup-config.xml) - ClickHouse backup configuration

## Environment Information

- **Platform**: macOS (Darwin 24.6.0)
- **Docker Compose**: v2.x
- **Migration Date**: 2025-10-16
- **Source System**: Old Mac (ClickHouse version unknown, created cmrk4 backups)
- **Target System**: New Mac with fresh Docker setup

## Questions or Issues?

If you encounter issues with this setup:
1. Check service logs: `docker-compose logs -f [service_name]`
2. Verify service health: `docker ps`
3. Review the detailed guides for specific services
4. Check that volumes have correct permissions
5. Ensure Docker has sufficient resources allocated (4GB+ RAM recommended)
