#!/bin/bash
# HPO Memory Monitor - 实时监控内存和关键日志

LOG_FILE="/tmp/hpo_memory_monitored.log"
PID_FILE="/tmp/hpo_monitor.pid"

echo "====== HPO Memory Monitor ======"
echo "开始时间: $(date)"
echo "配置文件: alpha_config_minimal.json"
echo "================================"
echo ""

# 启动HPO进程
PYTHONPATH=. venv/bin/python alpha_loop.py \
  --config alpha_config_minimal.json \
  --provider optuna \
  --gens 1 \
  --out_dir ./test_monitored_hpo \
  > "$LOG_FILE" 2>&1 &

HPO_PID=$!
echo $HPO_PID > "$PID_FILE"
echo "HPO进程已启动: PID=$HPO_PID"
echo ""

# 监控函数
monitor_memory() {
    local start_time=$(date +%s)
    local trial_count=0

    while kill -0 $HPO_PID 2>/dev/null; do
        local current_time=$(date +%s)
        local elapsed=$((current_time - start_time))

        # 获取内存使用
        local mem_info=$(ps -p $HPO_PID -o rss=,vsz=,%mem=,%cpu= 2>/dev/null)
        if [ -n "$mem_info" ]; then
            local rss=$(echo $mem_info | awk '{print $1}')
            local rss_mb=$(echo "scale=1; $rss/1024" | bc)
            local cpu=$(echo $mem_info | awk '{print $4}')

            # 检查是否有新的trial完成
            local current_trials=$(grep -c "evaluate_trial_memory.*after_cleanup" "$LOG_FILE" 2>/dev/null || echo 0)
            if [ $current_trials -gt $trial_count ]; then
                trial_count=$current_trials
                echo ""
                echo ">>> Trial #$trial_count 完成 <<<"

                # 显示最近的内存日志
                grep "evaluate_trial_memory" "$LOG_FILE" | tail -2
            fi

            # 每5秒输出内存状态
            printf "\r[%03ds] 内存: %6.1f MB | CPU: %5s%% | Trials: %d" \
                   $elapsed $rss_mb "$cpu" $trial_count
        fi

        sleep 5

        # 超过10分钟或内存超过2GB就停止
        if [ $elapsed -gt 600 ] || [ $(echo "$rss_mb > 2000" | bc) -eq 1 ]; then
            echo ""
            echo ""
            if [ $(echo "$rss_mb > 2000" | bc) -eq 1 ]; then
                echo "⚠️  内存超过2GB，停止测试"
            else
                echo "⏱️  10分钟测试完成"
            fi
            kill $HPO_PID 2>/dev/null
            break
        fi
    done

    # 进程结束
    wait $HPO_PID 2>/dev/null
    local exit_code=$?

    echo ""
    echo ""
    echo "================================"
    echo "测试结束: $(date)"
    echo "退出码: $exit_code"
    echo "运行时间: ${elapsed}秒"
    echo "完成Trials: $trial_count"

    # 显示最终统计
    echo ""
    echo "--- 内存清理统计 ---"
    grep "evaluate_trial_memory.*after_cleanup" "$LOG_FILE" | tail -5

    echo ""
    echo "--- Optuna试验状态 ---"
    if [ -f alpha_minimal_test.db ]; then
        sqlite3 alpha_minimal_test.db "SELECT trial_id, state, value FROM trials ORDER BY trial_id;" 2>/dev/null || echo "无法读取数据库"
    fi

    echo ""
    echo "完整日志: $LOG_FILE"
}

# 执行监控
monitor_memory
