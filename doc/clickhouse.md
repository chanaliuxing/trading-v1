通过 **Claude Code** 或任何其他应用程序连接它。

## 连接 ClickHouse 服务的核心信息

无论是 Claude Code 还是任何编程语言的客户端，连接 ClickHouse 都需要以下几个关键信息。由于您是通过 Homebrew Cask 安装的，您的配置都将是默认值：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| **主机地址 (Host)** | `127.0.0.1` 或 `localhost` | ClickHouse 服务器运行在您本地电脑上。 |
| **TCP 端口 (Native Port)** | `9000` | 用于 ClickHouse 原生客户端（如 `clickhouse-client` 或大多数驱动）的端口。 |
| **HTTP 端口 (HTTP Port)** | `8123` | 用于 HTTP API 的端口（通常用于 Web 界面或 RESTful 连接）。 |
| **用户名 (User)** | `default` | 默认的用户。 |
| **密码 (Password)** | *(无密码)* | 默认用户没有密码。 |

-----

## 💻 在 Claude Code 中连接 ClickHouse

**Claude Code** 本身是一个集成开发环境（IDE）或代码编辑器，它**不会直接**启动或连接数据库。您需要在 Claude Code 中**编写代码**，使用特定的 **ClickHouse 客户端库**（驱动程序）来连接和操作数据库。

如何连接取决于您在 Claude Code 中使用哪种编程语言。最常见的比如 Python、Node.js (JavaScript) 或 Java。

### 示例：Python 连接 (最常用)

如果您在 Claude Code 中使用 Python，这是最推荐和最简单的连接方式。

#### 1\. 安装 Python 驱动

首先，在您的终端（不是 Claude Code 的控制台）中安装官方推荐的 ClickHouse Python 客户端：

```bash
pip install clickhouse-connect
# 或者使用旧版本：pip install clickhouse-driver
```

#### 2\. 编写连接代码

在 Claude Code 中创建一个 Python 文件（例如 `connect.py`），并输入以下代码：

```python
import clickhouse_connect

# 使用默认配置连接 ClickHouse 服务器
client = clickhouse_connect.get_client(
    host='localhost',
    port=9000,
    username='default',
    password='' # 默认无密码
)

# 1. 验证连接
print("成功连接到 ClickHouse 服务器!")

# 2. 执行查询
result = client.query("SELECT 'Hello from Claude Code' AS message, now() AS current_time")

# 3. 打印结果
print("--- 查询结果 ---")
print(result.result_rows[0][0])
print(f"Time: {result.result_rows[0][1]}")

# 4. 创建和插入数据的示例 (可选)
# client.command('CREATE DATABASE IF NOT EXISTS my_db')
# client.command("CREATE TABLE IF NOT EXISTS my_db.test_table (id UInt64, value String) ENGINE = TinyLog")
# client.insert('my_db.test_table', [[1, 'test_data'], [2, 'more_data']])
```

#### 3\. 运行代码

在 Claude Code 中运行这个 Python 文件，您将看到连接成功的消息和查询结果。

-----
Perfect question 👍 — controlling ClickHouse (start / stop / restart) on macOS depends on how you installed it (in your case: **via Homebrew Cask manually**).
So you’ll be using **direct commands**, not `brew services`.
Here’s the full, clean guide 👇

---

## 🧩 **1️⃣ Check if ClickHouse is running**

```bash
ps aux | grep clickhouse
```

If you see a line like:

```
/opt/homebrew/bin/clickhouse server --config-file=/opt/homebrew/etc/clickhouse-server/config.xml --daemon
```

then it’s running ✅

You can also test:

```bash
curl http://localhost:8123/?query=SELECT%201
```

If it returns `1`, it’s alive.

---

## 🛑 **2️⃣ Stop ClickHouse**

There are two clean ways:

### a) Use ClickHouse client shutdown command:

```bash
/opt/homebrew/bin/clickhouse client --query "SYSTEM SHUTDOWN"
```

This sends a graceful shutdown signal to the server.

### b) Or kill the process manually (if hung):

```bash
pkill -f clickhouse
```

*(Safe because ClickHouse auto-saves metadata before exiting)*

---

## ▶️ **3️⃣ Start ClickHouse**

Start it as a background daemon:

```bash
/opt/homebrew/bin/clickhouse server --config-file=/opt/homebrew/etc/clickhouse-server/config.xml --daemon
```

✅ You’ll know it worked if:

```bash
ps aux | grep clickhouse
```

shows the server process again, and this works:

```bash
/opt/homebrew/bin/clickhouse client --query "SELECT 'Server is running OK';"
```

---
## 🔁 **4️⃣ Backup ClickHouse**
for example, use the name full_backup_20251010_043000 to backup:
/opt/homebrew/bin/clickhouse-backup --config=config/clickhouse_backup.yml create full_backup_20251010_043000

backup schema:
You can re-run them any time, e.g.:

  make backup-pg-schema
  make backup-ch-schema CLICKHOUSE_CLIENT_BIN=/nonexistent CH_HTTP_PORT=8123
## 🔁 **4️⃣ Restart ClickHouse**

Restart = stop + start:

```bash
/opt/homebrew/bin/clickhouse client --query "SYSTEM SHUTDOWN"
sleep 3
/opt/homebrew/bin/clickhouse server --config-file=/opt/homebrew/etc/clickhouse-server/config.xml --daemon
```

or simply:

```bash
pkill -f clickhouse
sleep 2
/opt/homebrew/bin/clickhouse server --config-file=/opt/homebrew/etc/clickhouse-server/config.xml --daemon
```

---

## 💡 **5️⃣ (Optional) If you set up LaunchAgent auto-start**

Then you can manage it like a service:

```bash
launchctl stop homebrew.mxcl.clickhouse
launchctl start homebrew.mxcl.clickhouse
launchctl unload ~/Library/LaunchAgents/homebrew.mxcl.clickhouse.plist
launchctl load ~/Library/LaunchAgents/homebrew.mxcl.clickhouse.plist
```


---

### ✅ Quick Summary

| Action      | Command                                                                                                                                       |                  |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| **Check**   | `ps aux                                                                                                                                       | grep clickhouse` |
| **Stop**    | `/opt/homebrew/bin/clickhouse client --query "SYSTEM SHUTDOWN"`                                                                               |                  |
| **Start**   | `/opt/homebrew/bin/clickhouse server --config-file=/opt/homebrew/etc/clickhouse-server/config.xml --daemon`                                   |                  |
| **Restart** | `pkill -f clickhouse && sleep 2 && /opt/homebrew/bin/clickhouse server --config-file=/opt/homebrew/etc/clickhouse-server/config.xml --daemon` |                  |
