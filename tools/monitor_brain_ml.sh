#!/bin/bash

# AI Brain ML 监控脚本
# 用途: 实时监控 Brain Consumer 和 ML Scheduler 的运行状态
# 使用: ./tools/monitor_brain_ml.sh [选项]
#
# 选项:
#   --once        只显示一次,不循环刷新
#   --interval N  刷新间隔秒数 (默认 5秒)
#   --tail N      显示最近 N 条 action 历史 (默认 5)

set -euo pipefail

# 配置
TRADING_DIR="."
TODAY=$(date +%Y-%m-%d)
RUNS_DIR="$TRADING_DIR/runs/ai_trader/$TODAY"
REFRESH_INTERVAL=5
ONCE_MODE=false
TAIL_COUNT=5

# 解析参数
while [[ $# -gt 0 ]]; do
  case $1 in
    --once)
      ONCE_MODE=true
      shift
      ;;
    --interval)
      REFRESH_INTERVAL="$2"
      shift 2
      ;;
    --tail)
      TAIL_COUNT="$2"
      shift 2
      ;;
    -h|--help)
      echo "使用方法: $0 [选项]"
      echo ""
      echo "选项:"
      echo "  --once          只显示一次,不循环刷新"
      echo "  --interval N    刷新间隔秒数 (默认 5秒)"
      echo "  --tail N        显示最近 N 条 action 历史 (默认 5)"
      echo "  -h, --help      显示此帮助信息"
      exit 0
      ;;
    *)
      echo "未知选项: $1"
      echo "使用 -h 或 --help 查看帮助"
      exit 1
      ;;
  esac
done

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# 绘制分隔线
print_separator() {
  printf "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

# 绘制标题
print_header() {
  local title="$1"
  printf "\n${BOLD}${BLUE}▓▓▓ %s ▓▓▓${NC}\n" "$title"
  print_separator
}

# 显示监控数据
show_dashboard() {
  clear
  printf "${BOLD}${MAGENTA}"
  cat << 'EOF'
   ___    ___   ____                _         __  __ _        __  __             _ _
  / _ \  |_ _| | __ ) _ __ __ _  (_)_ __   |  \/  | |      |  \/  | ___  _ __ (_) |_ ___  _ __
 / /_\ \  | |  |  _ \| '__/ _` | | | '_ \  | |\/| | |      | |\/| |/ _ \| '_ \| | __/ _ \| '__|
/ /_\\ \ | |  | |_) | | | (_| | | | | | | | |  | | |___   | |  | | (_) | | | | | || (_) | |
\____/_||___| |____/|_|  \__,_| |_|_| |_| |_|  |_|_____|  |_|  |_|\___/|_| |_|_|\__\___/|_|

EOF
  printf "${NC}"
  printf "${CYAN}监控时间: ${YELLOW}%s${NC}  |  ${CYAN}运行目录: ${YELLOW}%s${NC}\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$RUNS_DIR"
  print_separator

  # ========== 1. 进程状态 ==========
  print_header "1. 进程状态"

  local consumer_pid=$(ps aux | grep -v grep | grep "control_event_consumer.py" | awk '{print $2}' | head -1)
  local scheduler_pid=$(ps aux | grep -v grep | grep "ml_scheduler.py" | awk '{print $2}' | head -1)

  if [ -n "$consumer_pid" ]; then
    local consumer_mem=$(ps -o rss= -p "$consumer_pid" | awk '{printf "%.1f MB", $1/1024}')
    local consumer_cpu=$(ps -o %cpu= -p "$consumer_pid" | awk '{printf "%.1f%%", $1}')
    printf "  ${GREEN}✓${NC} Brain Consumer     ${GREEN}[运行中]${NC}  PID: ${YELLOW}%-7s${NC}  CPU: ${CYAN}%-6s${NC}  MEM: ${CYAN}%s${NC}\n" \
      "$consumer_pid" "$consumer_cpu" "$consumer_mem"
  else
    printf "  ${RED}✗${NC} Brain Consumer     ${RED}[未运行]${NC}\n"
  fi

  if [ -n "$scheduler_pid" ]; then
    local scheduler_mem=$(ps -o rss= -p "$scheduler_pid" | awk '{printf "%.1f MB", $1/1024}')
    local scheduler_cpu=$(ps -o %cpu= -p "$scheduler_pid" | awk '{printf "%.1f%%", $1}')
    printf "  ${GREEN}✓${NC} ML Scheduler       ${GREEN}[运行中]${NC}  PID: ${YELLOW}%-7s${NC}  CPU: ${CYAN}%-6s${NC}  MEM: ${CYAN}%s${NC}\n" \
      "$scheduler_pid" "$scheduler_cpu" "$scheduler_mem"
  else
    printf "  ${RED}✗${NC} ML Scheduler       ${RED}[未运行]${NC}\n"
  fi

  # ========== 2. 最新 Brain 决策 ==========
  print_header "2. 最新 Brain 决策"

  if [ -f "$RUNS_DIR/ml_review.json" ]; then
    local job_id=$(jq -r '.job_id // "N/A"' "$RUNS_DIR/ml_review.json")
    local job_type=$(jq -r '.job_type // "N/A"' "$RUNS_DIR/ml_review.json")
    local action=$(jq -r '.review.next_actions[0].action // "N/A"' "$RUNS_DIR/ml_review.json")
    local confidence=$(jq -r '.review.next_actions[0].confidence // 0' "$RUNS_DIR/ml_review.json")
    local params=$(jq -c '.review.next_actions[0].params // {}' "$RUNS_DIR/ml_review.json")
    local rationale=$(jq -r '.review.action_rationale // "N/A"' "$RUNS_DIR/ml_review.json" | sed 's/^/    /')

    printf "  ${CYAN}Job ID:${NC}        %s\n" "$job_id"
    printf "  ${CYAN}Job Type:${NC}      %s\n" "$job_type"
    printf "  ${CYAN}Action:${NC}        ${YELLOW}%s${NC}  ${CYAN}(confidence: %.2f)${NC}\n" "$action" "$confidence"
    printf "  ${CYAN}Params:${NC}        %s\n" "$params"
    printf "  ${CYAN}Rationale:${NC}\n%s\n" "$rationale"

    # 显示 diagnosis
    local diagnosis=$(jq -r '.review.diagnosis[]? // empty' "$RUNS_DIR/ml_review.json" | sed 's/^/    • /')
    if [ -n "$diagnosis" ]; then
      printf "  ${CYAN}Diagnosis:${NC}\n%s\n" "$diagnosis"
    fi

    # 显示 history_delta
    local prev_ic=$(jq -r '.review.history_delta.ic.prev // "N/A"' "$RUNS_DIR/ml_review.json")
    local cur_ic=$(jq -r '.review.history_delta.ic.cur // "N/A"' "$RUNS_DIR/ml_review.json")
    local prev_auc=$(jq -r '.review.history_delta.auc.prev // "N/A"' "$RUNS_DIR/ml_review.json")
    local cur_auc=$(jq -r '.review.history_delta.auc.cur // "N/A"' "$RUNS_DIR/ml_review.json")

    if [ "$prev_ic" != "N/A" ] && [ "$cur_ic" != "N/A" ]; then
      printf "  ${CYAN}IC Delta:${NC}      %s → %s\n" "$prev_ic" "$cur_ic"
    fi
    if [ "$prev_auc" != "N/A" ] && [ "$cur_auc" != "N/A" ]; then
      printf "  ${CYAN}AUC Delta:${NC}     %s → %s\n" "$prev_auc" "$cur_auc"
    fi
  else
    printf "  ${YELLOW}⚠${NC} 还没有 ml_review.json\n"
  fi

  # ========== 3. ML Action 历史 (最近 N 条) ==========
  print_header "3. ML Action 历史 (最近 ${TAIL_COUNT} 条)"

  if [ -f "$RUNS_DIR/ml_actions.jsonl" ]; then
    tail -"$TAIL_COUNT" "$RUNS_DIR/ml_actions.jsonl" | while IFS= read -r line; do
      local ts=$(echo "$line" | jq -r '.ts // "N/A"' | sed 's/T/ /' | cut -d'.' -f1)
      local action=$(echo "$line" | jq -r '.action // "N/A"')
      local status=$(echo "$line" | jq -r '.status // "N/A"')
      local confidence=$(echo "$line" | jq -r '.confidence_final // .confidence // 0')
      local params=$(echo "$line" | jq -c '.params // {}')

      # 根据 status 设置颜色
      local status_color=$NC
      case $status in
        ok) status_color=$GREEN ;;
        skipped) status_color=$YELLOW ;;
        failed) status_color=$RED ;;
      esac

      printf "  ${CYAN}%s${NC}  ${status_color}%-15s${NC}  ${YELLOW}%-18s${NC}  conf=%.2f  %s\n" \
        "$ts" "[$status]" "$action" "$confidence" "$params"
    done
  else
    printf "  ${YELLOW}⚠${NC} 还没有 ml_actions.jsonl\n"
  fi

  # ========== 4. 数据可用性 ==========
  print_header "4. 数据可用性"

  # 查询 ClickHouse
  if command -v docker &> /dev/null; then
    local ch_query="
      SELECT
        symbol,
        dateDiff('day', MIN(ts), MAX(ts)) as days_available,
        formatReadableQuantity(COUNT(*)) as bars
      FROM trading.features_1m_with_labels
      WHERE symbol IN ('TSLA', 'AAPL', 'NVDA', 'SPY')
      GROUP BY symbol
      FORMAT TSV
    "

    local ch_result=$(docker exec -i trading_clickhouse clickhouse-client --query "$ch_query" 2>/dev/null || echo "")

    if [ -n "$ch_result" ]; then
      printf "  ${CYAN}%-10s${NC}  ${CYAN}%15s${NC}  ${CYAN}%15s${NC}\n" "Symbol" "Days Available" "Total Bars"
      print_separator
      echo "$ch_result" | while IFS=$'\t' read -r symbol days bars; do
        printf "  ${YELLOW}%-10s${NC}  ${GREEN}%15s${NC}  ${CYAN}%15s${NC}\n" "$symbol" "$days" "$bars"
      done
    else
      printf "  ${RED}✗${NC} 无法连接 ClickHouse\n"
    fi
  else
    printf "  ${YELLOW}⚠${NC} Docker 未安装,无法查询数据\n"
  fi

  # ========== 5. 近期错误日志 ==========
  print_header "5. 近期错误 (最近 3 条)"

  local error_count=0

  # 检查 Brain Consumer 错误
  if [ -f "$RUNS_DIR/brain_consumer.log" ] || [ -f "$RUNS_DIR/brain_cycle_fixed.log" ]; then
    local log_file="$RUNS_DIR/brain_consumer.log"
    [ -f "$RUNS_DIR/brain_cycle_fixed.log" ] && log_file="$RUNS_DIR/brain_cycle_fixed.log"

    local errors=$(grep -i -E "error|exception|failed|validation_failed" "$log_file" 2>/dev/null | tail -3 || echo "")
    if [ -n "$errors" ]; then
      printf "  ${RED}Brain Consumer 错误:${NC}\n"
      echo "$errors" | while IFS= read -r line; do
        printf "    %s\n" "$line"
        error_count=$((error_count + 1))
      done
    fi
  fi

  # 检查 ML Scheduler 错误
  if [ -f "$RUNS_DIR/ml_scheduler.log" ]; then
    local errors=$(grep -i -E "error|exception|failed" "$RUNS_DIR/ml_scheduler.log" 2>/dev/null | tail -3 || echo "")
    if [ -n "$errors" ]; then
      printf "  ${RED}ML Scheduler 错误:${NC}\n"
      echo "$errors" | while IFS= read -r line; do
        printf "    %s\n" "$line"
        error_count=$((error_count + 1))
      done
    fi
  fi

  if [ $error_count -eq 0 ]; then
    printf "  ${GREEN}✓${NC} 没有近期错误\n"
  fi

  # ========== 6. 提示信息 ==========
  print_separator
  if [ "$ONCE_MODE" = false ]; then
    printf "\n${CYAN}监控模式: 循环刷新 (每 %ds)${NC}  |  ${YELLOW}按 Ctrl+C 退出${NC}\n" "$REFRESH_INTERVAL"
  else
    printf "\n${CYAN}监控模式: 单次显示${NC}\n"
  fi
  print_separator
}

# 主循环
main() {
  # 检查运行目录是否存在
  if [ ! -d "$RUNS_DIR" ]; then
    echo -e "${RED}错误: 运行目录不存在: $RUNS_DIR${NC}"
    echo -e "${YELLOW}请先启动 Brain Consumer 或创建目录:${NC}"
    echo -e "  mkdir -p $RUNS_DIR/{locks,cache}"
    exit 1
  fi

  if [ "$ONCE_MODE" = true ]; then
    show_dashboard
  else
    while true; do
      show_dashboard
      sleep "$REFRESH_INTERVAL"
    done
  fi
}

# 捕获 Ctrl+C
trap 'echo -e "\n${YELLOW}监控已停止${NC}"; exit 0' INT

# 运行主函数
main
