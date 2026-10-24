#!/bin/bash
# AI Trader 监控脚本
# 用法: bash scripts/ai_trader/monitor.sh

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

TODAY=$(TZ='America/New_York' date +%Y-%m-%d)
NOW=$(TZ='America/New_York' date +"%H:%M:%S ET")
RUNS_DIR="runs/ai_trader/${TODAY}"

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}🤖 AI Trader 监控面板${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "📅 日期: ${TODAY}  ⏰ 时间: ${NOW}"
echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 进程状态检查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo -e "${BLUE}┌─ 📊 进程状态${NC}"
echo -e "${BLUE}└────────────────────────────────────────────────────────────────────────────${NC}"

# 检查 auto_loop
AUTO_LOOP_PID=$(pgrep -f "auto_loop.py" || echo "")
if [ -n "$AUTO_LOOP_PID" ]; then
    echo -e "  ${GREEN}✅ auto_loop.py:${NC} 运行中 (PID: ${AUTO_LOOP_PID})"
else
    echo -e "  ${RED}❌ auto_loop.py:${NC} 未运行"
fi

# 检查 run_single_bot
BOT_PIDS=$(pgrep -f "run_single_bot.py" || echo "")
if [ -n "$BOT_PIDS" ]; then
    BOT_COUNT=$(echo "$BOT_PIDS" | wc -l | tr -d ' ')
    echo -e "  ${GREEN}✅ run_single_bot.py:${NC} ${BOT_COUNT} 个实例运行中"
    echo "$BOT_PIDS" | while read pid; do
        PROVIDER=$(ps -p "$pid" -o command= | grep -oP '(?<=--provider )\w+' || echo "unknown")
        SYMBOLS=$(ps -p "$pid" -o command= | grep -oP '(?<=--symbols )[A-Z ]+' | tr '\n' ',' | sed 's/,$//' || echo "unknown")
        echo -e "     ${CYAN}→${NC} PID ${pid}: ${PROVIDER} [${SYMBOLS}]"
    done
else
    echo -e "  ${YELLOW}⚠️  run_single_bot.py:${NC} 无运行实例"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 心跳文件检查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo -e "${BLUE}┌─ 💓 心跳状态${NC}"
echo -e "${BLUE}└────────────────────────────────────────────────────────────────────────────${NC}"

if [ -d "${RUNS_DIR}/hb" ]; then
    HB_FILES=$(find "${RUNS_DIR}/hb" -name "llm_*.json" -mmin -5 2>/dev/null || echo "")
    if [ -n "$HB_FILES" ]; then
        echo "$HB_FILES" | while read hb_file; do
            CYCLE=$(jq -r '.cycle_count // "N/A"' "$hb_file" 2>/dev/null)
            STATUS=$(jq -r '.status // "unknown"' "$hb_file" 2>/dev/null)
            POSITIONS=$(jq -r '.positions // 0' "$hb_file" 2>/dev/null)
            TIMESTAMP=$(jq -r '.timestamp // "unknown"' "$hb_file" 2>/dev/null)
            FILE_NAME=$(basename "$hb_file")

            if [ "$STATUS" = "running" ]; then
                echo -e "  ${GREEN}✅ ${FILE_NAME}:${NC}"
            else
                echo -e "  ${YELLOW}⚠️  ${FILE_NAME}:${NC}"
            fi
            echo -e "     Cycle: ${CYCLE} | Positions: ${POSITIONS} | Updated: ${TIMESTAMP}"
        done
    else
        echo -e "  ${YELLOW}⚠️  无活跃心跳文件 (最近 5 分钟内)${NC}"
    fi
else
    echo -e "  ${RED}❌ 心跳目录不存在: ${RUNS_DIR}/hb${NC}"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. 今日计划检查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo -e "${BLUE}┌─ 📋 今日计划${NC}"
echo -e "${BLUE}└────────────────────────────────────────────────────────────────────────────${NC}"

if [ -d "${RUNS_DIR}" ]; then
    PLAN_FILES=$(find "${RUNS_DIR}" -maxdepth 1 -name "*_plan.json" -o -name "brain_plan_*.json" 2>/dev/null | sort -r | head -5)
    if [ -n "$PLAN_FILES" ]; then
        echo "$PLAN_FILES" | while read plan_file; do
            FILE_NAME=$(basename "$plan_file")
            MTIME=$(stat -f "%Sm" -t "%H:%M:%S" "$plan_file" 2>/dev/null || stat -c "%y" "$plan_file" 2>/dev/null | cut -d' ' -f2 | cut -d. -f1)
            echo -e "  ${CYAN}📄 ${FILE_NAME}${NC} (更新于 ${MTIME})"
        done
    else
        echo -e "  ${YELLOW}⚠️  无计划文件${NC}"
    fi
else
    echo -e "  ${RED}❌ 运行目录不存在: ${RUNS_DIR}${NC}"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 最近日志（最后 10 行）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo -e "${BLUE}┌─ 📝 最近日志 (最后 10 行)${NC}"
echo -e "${BLUE}└────────────────────────────────────────────────────────────────────────────${NC}"

LATEST_LOG=$(ls -t logs/deepseek_nvidia_*.log 2>/dev/null | head -1 || echo "")
if [ -n "$LATEST_LOG" ]; then
    echo -e "  ${CYAN}日志文件:${NC} ${LATEST_LOG}"
    echo ""
    tail -10 "$LATEST_LOG" | sed 's/^/  │ /'
else
    echo -e "  ${YELLOW}⚠️  未找到日志文件${NC}"
fi

echo ""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. 快捷命令提示
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo -e "${BLUE}┌─ 🛠️  快捷命令${NC}"
echo -e "${BLUE}└────────────────────────────────────────────────────────────────────────────${NC}"
echo -e "  ${CYAN}查看实时日志:${NC}"
echo -e "    tail -f logs/deepseek_nvidia_*.log"
echo ""
echo -e "  ${CYAN}查看心跳详情:${NC}"
echo -e "    watch -n 5 'cat ${RUNS_DIR}/hb/llm_*.json'"
echo ""
echo -e "  ${CYAN}查看计划详情:${NC}"
echo -e "    cat ${RUNS_DIR}/overnight_plan.json | jq ."
echo ""
echo -e "  ${CYAN}停止所有进程:${NC}"
echo -e "    pkill -f auto_loop.py && pkill -f run_single_bot.py"
echo ""
echo -e "  ${CYAN}重新监控:${NC}"
echo -e "    bash scripts/ai_trader/monitor.sh"
echo ""

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
