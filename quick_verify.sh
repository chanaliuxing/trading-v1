#!/bin/bash
# quick_verify.sh - 快速验证单个模型
# 使用方法: ./quick_verify.sh xgb

MODEL=$1

if [ -z "$MODEL" ]; then
  echo "用法: $0 <model_type>"
  echo "模型类型: xgb, lstm, transformer, etsformer, itransformer, sizing"
  exit 1
fi

LOG_FILE="/tmp/test_${MODEL}_$(date +%Y%m%d_%H%M%S).log"
OUTPUT_DIR="./test_results/${MODEL}"

echo "=========================================="
echo "测试模型: $MODEL"
echo "日志文件: $LOG_FILE"
echo "输出目录: $OUTPUT_DIR"
echo "=========================================="

# 清理旧的输出目录
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# 根据模型类型选择配置
case $MODEL in
  xgb)
    CONFIG="alpha_config_minimal.json"
    ;;
  lstm|transformer|etsformer|itransformer)
    CONFIG="alpha_config_enhanced.json"
    ;;
  sizing)
    CONFIG="alpha_config_minimal.json"
    ;;
  *)
    echo "❌ 未知模型类型: $MODEL"
    exit 1
    ;;
esac

echo "使用配置: $CONFIG"
echo ""

# 运行测试
export PYTHONPATH=.
venv/bin/python alpha_loop.py \
  --config $CONFIG \
  --provider optuna \
  --gens 1 \
  --out_dir "$OUTPUT_DIR" \
  2>&1 | tee $LOG_FILE &

PID=$!
echo "进程 PID: $PID"
echo ""

# 监控内存 (30秒, 每3秒检查一次)
echo "内存监控 (30秒):"
START_MEM=""
for i in {1..10}; do
  sleep 3
  if ps -p $PID > /dev/null 2>&1; then
    MEM=$(ps -o rss= -p $PID 2>/dev/null | awk '{printf "%.1f", $1/1024}')
    CPU=$(ps -o %cpu= -p $PID 2>/dev/null | awk '{print $1}')

    if [ -z "$START_MEM" ]; then
      START_MEM=$MEM
    fi

    echo "[$i] 内存: ${MEM}MB | CPU: ${CPU}%"
  else
    echo "[$i] 进程已结束"
    break
  fi
done

echo ""
echo "等待进程完成..."
wait $PID
EXIT_CODE=$?

# 最终内存
FINAL_MEM=$(echo "$MEM" | awk '{print $1}')
if [ ! -z "$START_MEM" ] && [ ! -z "$FINAL_MEM" ]; then
  MEM_GROWTH=$(echo "$FINAL_MEM - $START_MEM" | bc)
else
  MEM_GROWTH="N/A"
fi

# 检查结果
echo ""
echo "=========================================="
echo "验证结果"
echo "=========================================="
echo "退出代码: $EXIT_CODE"

ERROR_COUNT=$(grep -c 'Error\|Exception\|Traceback' $LOG_FILE | head -1)
echo "错误数量: $ERROR_COUNT"

OUTPUT_FILES=$(ls -1 "$OUTPUT_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
echo "生成文件: $OUTPUT_FILES"

echo "内存增长: ${MEM_GROWTH}MB (${START_MEM}MB → ${FINAL_MEM}MB)"

RUNTIME=$(grep -E "elapsed|completed" $LOG_FILE | tail -1)
echo "运行信息: $RUNTIME"

# 判断是否通过
if [ $EXIT_CODE -eq 0 ] && [ $ERROR_COUNT -eq 0 ] && [ $OUTPUT_FILES -gt 0 ]; then
  echo ""
  echo "状态: ✅ PASS"
  echo ""

  # 显示关键指标
  if [ -f "$OUTPUT_DIR/alpha_best.json" ]; then
    echo "最佳结果:"
    python3 -c "import json; data=json.load(open('$OUTPUT_DIR/alpha_best.json')); print(f\"  Sharpe: {data.get('oos_sharpe', 'N/A')}\"); print(f\"  Trades: {data.get('oos_trades', 'N/A')}\"); print(f\"  Return: {data.get('oos_total_return', 'N/A')}\")" 2>/dev/null || echo "  无法解析结果"
  fi
else
  echo ""
  echo "状态: ❌ FAIL"
  echo ""
  echo "最后50行日志:"
  echo "----------------------------------------"
  tail -50 $LOG_FILE
fi

echo ""
echo "完整日志: $LOG_FILE"
echo "=========================================="
