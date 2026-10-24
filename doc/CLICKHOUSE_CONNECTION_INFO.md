# ClickHouse Connection Information

## Current Configuration

Your ClickHouse instance is running with default settings (no password authentication).

### Connection Details
- **Host**: `localhost`
- **HTTP Port**: `8123`
- **Native Port**: `9000`
- **Username**: `default`
- **Password**: `` (empty/no password)
- **Databases**: `trading`, `lake`

### Network Access
The default user is configured to accept connections from any IP (`::/0`). This is configured in `infra/clickhouse-users.xml` and mounted into the container at `/etc/clickhouse-server/users.d/default-user.xml`.

## Connection Methods

### 1. Command Line (clickhouse-client)

#### From Docker Container
```bash
# Interactive mode
docker exec -it trading_clickhouse clickhouse-client

# Execute single query
docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM trading.bars_1m_ibkr"

# Connect to specific database
docker exec -it trading_clickhouse clickhouse-client --database trading
```

#### From Host Machine (if clickhouse-client installed)
```bash
clickhouse-client --host localhost --port 9000 --user default

# With specific database
clickhouse-client --host localhost --port 9000 --user default --database trading
```

### 2. HTTP Interface

#### Using curl
```bash
# Simple query
curl 'http://localhost:8123/?query=SELECT+version()'

# Query with formatting
curl 'http://localhost:8123/?query=SELECT+*+FROM+trading.bars_1m_ibkr+LIMIT+5+FORMAT+JSONEachRow'

# Using POST with query in body
echo 'SELECT database, name, total_rows FROM system.tables WHERE database = "trading"' | \
  curl 'http://localhost:8123/' --data-binary @-

# With user parameter (optional, default is used)
curl 'http://localhost:8123/?user=default&query=SHOW+DATABASES'
```

#### Using wget
```bash
wget -qO- 'http://localhost:8123/?query=SELECT+version()'
```

### 3. Python Connections

#### Option A: clickhouse-driver (Native Protocol)
```bash
pip install clickhouse-driver
```

```python
from clickhouse_driver import Client

# Create client
client = Client(
    host='localhost',
    port=9000,
    user='default',
    password='',
    database='trading'
)

# Execute query
result = client.execute('SELECT * FROM bars_1m_ibkr LIMIT 10')
for row in result:
    print(row)

# With parameters
result = client.execute(
    'SELECT * FROM bars_1m_ibkr WHERE symbol = %(symbol)s LIMIT 10',
    {'symbol': 'SPY'}
)

# Get as DataFrame
import pandas as pd
result = client.execute('SELECT * FROM bars_1m_ibkr LIMIT 100')
df = pd.DataFrame(result, columns=[col[0] for col in client.execute('DESCRIBE bars_1m_ibkr')])
```

#### Option B: clickhouse-connect (HTTP Protocol)
```bash
pip install clickhouse-connect
```

```python
import clickhouse_connect

# Create client
client = clickhouse_connect.get_client(
    host='localhost',
    port=8123,
    username='default',
    password='',
    database='trading'
)

# Execute query
result = client.query('SELECT * FROM bars_1m_ibkr LIMIT 10')
print(result.result_rows)

# Get as DataFrame
df = client.query_df('SELECT * FROM bars_1m_ibkr WHERE symbol = %(symbol)s LIMIT 100',
                     parameters={'symbol': 'SPY'})

# Insert data
client.insert('bars_1m_ibkr', data, column_names=['symbol', 'ts_start', 'open', 'high', 'low', 'close'])
```

#### Option C: SQLAlchemy
```bash
pip install sqlalchemy clickhouse-sqlalchemy
```

```python
from sqlalchemy import create_engine

# Create engine
engine = create_engine('clickhouse://default:@localhost:8123/trading')

# Execute query
with engine.connect() as conn:
    result = conn.execute('SELECT * FROM bars_1m_ibkr LIMIT 10')
    for row in result:
        print(row)

# With pandas
import pandas as pd
df = pd.read_sql('SELECT * FROM bars_1m_ibkr LIMIT 100', engine)
```

### 4. GUI Tools

#### DBeaver
1. New Connection → ClickHouse
2. Settings:
   - Host: `localhost`
   - Port: `8123` (HTTP) or `9000` (Native)
   - Database: `trading`
   - Username: `default`
   - Password: (leave empty)

#### DataGrip / IntelliJ
1. New Data Source → ClickHouse
2. Settings:
   - Host: `localhost`
   - Port: `8123`
   - User: `default`
   - Password: (leave empty)
   - Database: `trading`
   - URL: `jdbc:clickhouse://localhost:8123/trading`

#### Tabix (Web UI)
1. Open: http://ui.tabix.io/
2. Settings:
   - Name: Trading ClickHouse
   - HTTP: `http://localhost:8123`
   - Login: `default`
   - Password: (leave empty)

## Connection String Examples

### JDBC
```
jdbc:clickhouse://localhost:8123/trading
```

### ODBC
```
Driver={ClickHouse ODBC Driver (Unicode)};
Server=localhost;
Port=9000;
Database=trading;
UID=default;
PWD=;
```

### Generic URL Format
```
# HTTP
clickhouse://default:@localhost:8123/trading

# Native
clickhouse://default:@localhost:9000/trading
```

## Testing Your Connection

### Quick Test Script (Python)
```python
import clickhouse_connect

def test_connection():
    try:
        client = clickhouse_connect.get_client(
            host='localhost',
            port=8123,
            username='default',
            password=''
        )

        # Test query
        result = client.query('SELECT version()')
        print(f"✓ Connected to ClickHouse {result.first_item[0]}")

        # Check databases
        dbs = client.query('SHOW DATABASES')
        print(f"✓ Available databases: {', '.join([row[0] for row in dbs.result_rows])}")

        # Check trading tables
        tables = client.query('SELECT name FROM system.tables WHERE database = \'trading\' LIMIT 5')
        print(f"✓ Sample tables in trading: {', '.join([row[0] for row in tables.result_rows])}")

        return True
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False

if __name__ == '__main__':
    test_connection()
```

### Quick Test (Bash)
```bash
#!/bin/bash

echo "Testing ClickHouse connection..."

# Test 1: HTTP ping
if curl -s http://localhost:8123/ping | grep -q "Ok"; then
    echo "✓ HTTP interface responding"
else
    echo "✗ HTTP interface not responding"
    exit 1
fi

# Test 2: Version check
VERSION=$(docker exec trading_clickhouse clickhouse-client --query "SELECT version()")
echo "✓ ClickHouse version: $VERSION"

# Test 3: Database check
DATABASES=$(docker exec trading_clickhouse clickhouse-client --query "SHOW DATABASES" | tr '\n' ', ')
echo "✓ Databases: $DATABASES"

# Test 4: Row count check
ROWS=$(docker exec trading_clickhouse clickhouse-client --query "SELECT count() FROM trading.bars_1m_ibkr")
echo "✓ trading.bars_1m_ibkr has $ROWS rows"

echo "All connection tests passed!"
```

## Adding Password Authentication (Optional)

If you want to secure your ClickHouse instance with a password:

### Step 1: Create users.xml
```xml
<!-- infra/clickhouse-users.xml -->
<clickhouse>
    <users>
        <default>
            <password>your_password_here</password>
            <networks>
                <ip>::/0</ip>
            </networks>
            <profile>default</profile>
            <quota>default</quota>
        </default>

        <!-- Read-only user -->
        <readonly>
            <password>readonly_password</password>
            <networks>
                <ip>::/0</ip>
            </networks>
            <profile>readonly</profile>
            <quota>default</quota>
        </readonly>
    </users>

    <profiles>
        <readonly>
            <readonly>1</readonly>
        </readonly>
    </profiles>
</clickhouse>
```

### Step 2: Update docker-compose.yml
```yaml
clickhouse:
  image: clickhouse/clickhouse-server:latest
  volumes:
    - ch_data:/var/lib/clickhouse
    - ./clickhouse-backup-config.xml:/etc/clickhouse-server/config.d/backup-config.xml
    - ./clickhouse-users.xml:/etc/clickhouse-server/users.d/users.xml  # Add this line
```

### Step 3: Restart ClickHouse
```bash
docker-compose restart clickhouse
```

### Step 4: Connect with password
```bash
docker exec -it trading_clickhouse clickhouse-client --user default --password your_password_here
```

```python
client = clickhouse_connect.get_client(
    host='localhost',
    port=8123,
    username='default',
    password='your_password_here'
)
```

## Common Connection Issues

### Issue 1: Connection refused
```
Error: Connection refused
```
**Solution**: Check if ClickHouse is running:
```bash
docker ps | grep clickhouse
docker logs trading_clickhouse
```

### Issue 2: Port already in use
```
Error: bind: address already in use
```
**Solution**: Another service is using ports 8123 or 9000. Change ports in compose.yml:
```yaml
ports:
  - "18123:8123"
  - "19000:9000"
```

### Issue 3: Database not found
```
Error: Database trading doesn't exist
```
**Solution**: Check available databases:
```bash
docker exec trading_clickhouse clickhouse-client --query "SHOW DATABASES"
```

### Issue 4: Permission denied
```
Error: Not enough privileges
```
**Solution**: Check user permissions:
```bash
docker exec trading_clickhouse clickhouse-client --query "SHOW GRANTS FOR default"
```

### Issue 5: Authentication failed from host
```
Code: 194. DB::Exception: default: Authentication failed: password is incorrect, or there is no user with such name.
```
**Cause**: The default user is restricted to localhost connections only.

**Solution**: Update user network configuration to allow all connections. This is already configured in `infra/clickhouse-users.xml`:
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

Then reload config:
```bash
docker exec trading_clickhouse clickhouse-client --query "SYSTEM RELOAD CONFIG"
```

## Best Practices

1. **Use connection pooling** in production applications
2. **Set read/write timeouts** to prevent hanging connections
3. **Use prepared statements** to prevent SQL injection
4. **Monitor connection counts**: `SELECT count() FROM system.processes`
5. **Use compression** for large result sets
6. **Batch inserts** instead of single-row inserts
7. **Use async queries** for long-running operations

## References

- ClickHouse HTTP Interface: https://clickhouse.com/docs/en/interfaces/http/
- ClickHouse CLI Client: https://clickhouse.com/docs/en/interfaces/cli/
- Python Libraries:
  - clickhouse-driver: https://github.com/mymarilyn/clickhouse-driver
  - clickhouse-connect: https://github.com/ClickHouse/clickhouse-connect
- JDBC Driver: https://github.com/ClickHouse/clickhouse-java
