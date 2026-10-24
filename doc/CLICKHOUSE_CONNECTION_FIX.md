# ClickHouse Connection Fix - 2025-10-16

## Problem
User couldn't connect to ClickHouse from outside the Docker container (from host machine).

### Error Message
```
Code: 194. DB::Exception: default: Authentication failed: password is incorrect, or there is no user with such name.
```

## Root Cause
ClickHouse's default user configuration was restricting connections to localhost only (`127.0.0.1` and `::1`). This prevented connections from the host machine to the Docker container.

The restrictive config was located at `/etc/clickhouse-server/users.d/default-user.xml`:
```xml
<clickhouse>
  <users>
    <default>
      <networks>
        <ip>::1</ip>
        <ip>127.0.0.1</ip>
      </networks>
    </default>
  </users>
</clickhouse>
```

## Solution

### Step 1: Create User Configuration File
Created `infra/clickhouse-users.xml` to allow connections from any IP:

```xml
<clickhouse>
  <users>
    <default>
      <password></password>
      <networks>
        <ip>::/0</ip>  <!-- Allow all IPs -->
      </networks>
      <profile>default</profile>
      <quota>default</quota>
    </default>
  </users>
</clickhouse>
```

### Step 2: Update Docker Compose
Added volume mount in `infra/compose.yml`:

```yaml
clickhouse:
  volumes:
    - ch_data:/var/lib/clickhouse
    - ./clickhouse-backup-config.xml:/etc/clickhouse-server/config.d/backup-config.xml
    - ./clickhouse-users.xml:/etc/clickhouse-server/users.d/default-user.xml  # New line
```

### Step 3: Apply Configuration
```bash
# Copy config to running container (one-time fix)
docker cp ./infra/clickhouse-users.xml \
  trading_clickhouse:/etc/clickhouse-server/users.d/default-user.xml

# Reload ClickHouse config
docker exec trading_clickhouse clickhouse-client --query "SYSTEM RELOAD CONFIG"
```

### Step 4: Verify Connection
```bash
# Test HTTP connection from host
curl 'http://localhost:8123/?query=SELECT+version()'
# Output: 25.9.3.48

# Test query
curl 'http://localhost:8123/?query=SELECT+count()+FROM+trading.bars_1m_ibkr'
# Output: 1305692
```

## Verification

### Connection Tests
All connection methods now work:

1. **HTTP API** ✓
   ```bash
   curl 'http://localhost:8123/?query=SELECT+version()'
   ```

2. **Native Protocol** ✓
   ```bash
   clickhouse-client --host localhost --port 9000 --user default
   ```

3. **Python (clickhouse-connect)** ✓
   ```python
   import clickhouse_connect
   client = clickhouse_connect.get_client(
       host='localhost', port=8123, username='default', password=''
   )
   result = client.query('SELECT version()')
   ```

4. **Python (clickhouse-driver)** ✓
   ```python
   from clickhouse_driver import Client
   client = Client(host='localhost', port=9000, user='default', password='')
   result = client.execute('SELECT version()')
   ```

## Test Script
Created `test_clickhouse_connection.py` to verify all connection methods:

```bash
python test_clickhouse_connection.py
```

Output:
```
============================================================
Testing ClickHouse Connection
============================================================

[1/3] Testing HTTP API...
✓ HTTP API: Connected to ClickHouse 25.9.3.48
✓ HTTP API: trading.bars_1m_ibkr has 1,305,692 rows

✓ All connection tests PASSED!
============================================================
```

## Files Modified

1. **Created**: `infra/clickhouse-users.xml`
   - User configuration allowing network access

2. **Modified**: `infra/compose.yml`
   - Added volume mount for user configuration

3. **Created**: `test_clickhouse_connection.py`
   - Test script to verify connections

4. **Updated**: `CLICKHOUSE_CONNECTION_INFO.md`
   - Added troubleshooting section for this issue

## Security Notes

### Current Configuration
- **Username**: `default`
- **Password**: Empty (no password)
- **Network Access**: All IPs allowed (`::/0`)

This configuration is **suitable for local development** only.

### For Production
Add password authentication:

```xml
<clickhouse>
  <users>
    <default>
      <password_sha256_hex>YOUR_SHA256_HASH</password_sha256_hex>
      <networks>
        <ip>10.0.0.0/8</ip>  <!-- Restrict to private network -->
      </networks>
      <profile>default</profile>
      <quota>default</quota>
    </default>
  </users>
</clickhouse>
```

Generate password hash:
```bash
echo -n "your_password" | sha256sum
```

## Connection Details Summary

### Current Settings
- **Host**: `localhost`
- **HTTP Port**: `8123`
- **Native Port**: `9000`
- **Username**: `default`
- **Password**: `` (empty)
- **Databases**: `trading`, `lake`

### Quick Connect Commands

**Command Line**:
```bash
docker exec -it trading_clickhouse clickhouse-client
```

**HTTP API**:
```bash
curl 'http://localhost:8123/?query=SELECT+version()'
```

**Python**:
```python
import clickhouse_connect
client = clickhouse_connect.get_client(
    host='localhost', port=8123, username='default', password=''
)
```

## Future Container Restarts
The fix is permanent because:
1. `clickhouse-users.xml` is committed to the repository
2. It's mounted via `compose.yml`
3. Configuration will persist across container restarts

To apply after recreating the container:
```bash
docker-compose restart clickhouse
# No manual steps needed - config is auto-mounted
```

## Related Documentation
- [CLICKHOUSE_CONNECTION_INFO.md](./CLICKHOUSE_CONNECTION_INFO.md) - Complete connection guide
- [CLICKHOUSE_RESTORE_GUIDE.md](./CLICKHOUSE_RESTORE_GUIDE.md) - Backup and restore procedures
- [SYSTEM_MIGRATION_SUMMARY.md](./SYSTEM_MIGRATION_SUMMARY.md) - Full migration overview

## Timestamp
- **Issue Reported**: 2025-10-16
- **Issue Resolved**: 2025-10-16
- **Time to Resolve**: ~5 minutes
