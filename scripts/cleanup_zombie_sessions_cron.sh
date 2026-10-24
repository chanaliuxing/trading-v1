#!/bin/bash
#
# 僵尸会话自动清理 - Cron Job Wrapper
#
# 用法: 添加到 crontab
#   0 */6 * * * ./scripts/cleanup_zombie_sessions_cron.sh
#
# 说明:
#   - 每 6 小时运行一次
#   - 清理 > 24小时的僵尸会话
#   - 日志输出到 logs/zombie_cleanup.log
#

# 项目根目录
PROJECT_ROOT="."
cd "$PROJECT_ROOT" || exit 1

# 日志目录
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"

# 日志文件
LOG_FILE="$LOG_DIR/zombie_cleanup.log"

# Python 环境
PYTHON="$PROJECT_ROOT/venv/bin/python"

# 清理脚本
CLEANUP_SCRIPT="$PROJECT_ROOT/scripts/cleanup_zombie_sessions.py"

# 运行清理
echo "========================================" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting zombie session cleanup" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

# 执行清理 (阈值24小时)
$PYTHON "$CLEANUP_SCRIPT" --threshold 24 >> "$LOG_FILE" 2>&1

EXIT_CODE=$?

echo "" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleanup finished with exit code: $EXIT_CODE" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# 保持日志文件大小 (只保留最近 1000 行)
if [ -f "$LOG_FILE" ]; then
    tail -n 1000 "$LOG_FILE" > "$LOG_FILE.tmp"
    mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

exit $EXIT_CODE
