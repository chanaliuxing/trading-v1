#!/bin/bash
# AI Trader 详细状态脚本
# 用法: bash scripts/ai_trader/status.sh

set -euo pipefail

# Use NY timezone for all dates
TODAY=$(TZ='America/New_York' date +%Y-%m-%d)
RUNS_DIR="runs/ai_trader/${TODAY}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 AI Trader 详细状态 - $(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 进程信息
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "┌─ 📊 进程信息"
echo "└────────────────────────────────────────────────────────────────────────────"

if pgrep -f "auto_loop.py" > /dev/null; then
    echo "✅ auto_loop.py 运行中:"
    ps aux | grep "auto_loop.py" | grep -v grep | awk '{printf "   PID: %-8s CPU: %-6s MEM: %-6s START: %s\n", $2, $3"%", $4"%", $9}'
else
    echo "❌ auto_loop.py 未运行"
fi

echo ""

if pgrep -f "run_single_bot.py" > /dev/null; then
    echo "✅ run_single_bot.py 运行中:"
    ps aux | grep "run_single_bot.py" | grep -v grep | awk '{printf "   PID: %-8s CPU: %-6s MEM: %-6s START: %s\n", $2, $3"%", $4"%", $9}'
else
    echo "❌ run_single_bot.py 未运行"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 进程元数据（从 procs 目录）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "┌─ 🗂️  进程元数据"
echo "└────────────────────────────────────────────────────────────────────────────"

if [ -d "${RUNS_DIR}/procs" ]; then
    PROC_FILES=$(find "${RUNS_DIR}/procs" -name "llm_*.json" 2>/dev/null)
    if [ -n "$PROC_FILES" ]; then
        echo "$PROC_FILES" | while read proc_file; do
            echo "📄 $(basename "$proc_file"):"
            SESSION=$(jq -r '.session // "N/A"' "$proc_file" 2>/dev/null)
            PROVIDER=$(jq -r '.provider // "N/A"' "$proc_file" 2>/dev/null)
            SYMBOLS=$(jq -r '.symbols | join(", ")' "$proc_file" 2>/dev/null)
            INTERVAL=$(jq -r '.interval // 0' "$proc_file" 2>/dev/null)
            CYCLES=$(jq -r '.cycles // "∞"' "$proc_file" 2>/dev/null)
            CLOSE_AT=$(jq -r '.close_at // "N/A"' "$proc_file" 2>/dev/null)
            STATUS=$(jq -r '.status // "unknown"' "$proc_file" 2>/dev/null)
            RESTARTS=$(jq -r '.restarts // 0' "$proc_file" 2>/dev/null)
            STARTED_UTC=$(jq -r '.started_at // "N/A"' "$proc_file" 2>/dev/null)

            # Convert UTC to NY time using Python
            if [ "$STARTED_UTC" != "N/A" ]; then
                STARTED=$(python3 -c "
from datetime import datetime
import pytz
try:
    utc = datetime.fromisoformat('${STARTED_UTC%.*}')
    ny = pytz.timezone('America/New_York')
    ny_time = utc.replace(tzinfo=pytz.UTC).astimezone(ny)
    print(ny_time.strftime('%Y-%m-%d %H:%M:%S ET'))
except:
    print('$STARTED_UTC')
" 2>/dev/null)
            else
                STARTED="N/A"
            fi

            echo "   Session: ${SESSION} | Provider: ${PROVIDER} | Symbols: ${SYMBOLS}"
            echo "   Interval: ${INTERVAL}s | Cycles: ${CYCLES} | Close at: ${CLOSE_AT}"
            echo "   Status: ${STATUS} | Restarts: ${RESTARTS} | Started: ${STARTED}"
            echo ""
        done
    else
        echo "⚠️  无进程元数据文件"
    fi
else
    echo "❌ 进程目录不存在: ${RUNS_DIR}/procs"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. 心跳详情
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "┌─ 💓 心跳详情"
echo "└────────────────────────────────────────────────────────────────────────────"

if [ -d "${RUNS_DIR}/hb" ]; then
    HB_FILES=$(find "${RUNS_DIR}/hb" -name "llm_*.json" 2>/dev/null)
    if [ -n "$HB_FILES" ]; then
        echo "$HB_FILES" | while read hb_file; do
            FILE_NAME=$(basename "$hb_file")
            AGE_SEC=$(( $(date +%s) - $(stat -f "%m" "$hb_file" 2>/dev/null || stat -c "%Y" "$hb_file" 2>/dev/null) ))
            AGE_MIN=$(( AGE_SEC / 60 ))

            echo "💓 ${FILE_NAME} (更新于 ${AGE_MIN} 分钟前):"
            cat "$hb_file" | jq '.' 2>/dev/null || echo "   解析失败"
            echo ""
        done
    else
        echo "⚠️  无心跳文件"
    fi
else
    echo "❌ 心跳目录不存在: ${RUNS_DIR}/hb"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 计划文件
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "┌─ 📋 计划文件"
echo "└────────────────────────────────────────────────────────────────────────────"

if [ -d "${RUNS_DIR}" ]; then
    PLAN_FILES=$(find "${RUNS_DIR}" -maxdepth 1 \( -name "*_plan.json" -o -name "brain_plan_*.json" \) 2>/dev/null | sort -r)
    if [ -n "$PLAN_FILES" ]; then
        echo "$PLAN_FILES" | while read plan_file; do
            FILE_NAME=$(basename "$plan_file")
            echo "📄 ${FILE_NAME}:"
            cat "$plan_file" | jq '.' 2>/dev/null || echo "   解析失败"
            echo ""
        done
    else
        echo "⚠️  无计划文件"
    fi
else
    echo "❌ 运行目录不存在: ${RUNS_DIR}"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. 数据库会话（从 ClickHouse）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "┌─ 📊 数据库会话（ClickHouse）"
echo "└────────────────────────────────────────────────────────────────────────────"

# Try to find python with clickhouse-driver
if command -v python &> /dev/null && python -c "import clickhouse_driver" 2>/dev/null; then
    python scripts/ai_trader/check_sessions.py 2>/dev/null | tail -n +2
elif [ -f "venv/bin/python" ]; then
    venv/bin/python scripts/ai_trader/check_sessions.py 2>/dev/null | tail -n +2
else
    echo "⚠️  clickhouse-driver 未安装或虚拟环境未激活"
    echo "   安装: pip install clickhouse-driver"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. 最近错误日志
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo "┌─ ⚠️  最近错误日志 (最后 5 行)"
echo "└────────────────────────────────────────────────────────────────────────────"

LATEST_LOG=$(ls -t logs/deepseek_nvidia_*.log 2>/dev/null | head -1 || echo "")
if [ -n "$LATEST_LOG" ]; then
    ERRORS=$(grep -i "error\|exception\|failed\|critical" "$LATEST_LOG" 2>/dev/null | tail -5 || echo "")
    if [ -n "$ERRORS" ]; then
        echo "$ERRORS" | sed 's/^/   /'
    else
        echo "✅ 无错误日志"
    fi
else
    echo "⚠️  未找到日志文件"
fi

echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "💡 提示: 使用 'bash scripts/ai_trader/monitor.sh' 查看实时监控面板"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
