#!/bin/bash
#
# Brain IC Integration Test - Real-time Monitor
# Usage: ./tools/brain_test_monitor.sh [symbol]
#

SYMBOL="${1:-TSLA}"
TODAY=$(date +%Y-%m-%d)
RUNS_DIR="runs/ai_trader/$TODAY"

echo "======================================================================"
echo "Brain IC Integration Test - Monitor"
echo "Symbol: $SYMBOL"
echo "Date: $TODAY"
echo "======================================================================"
echo ""

# Function to display section
section() {
    echo ""
    echo "======================================================================"
    echo "$1"
    echo "======================================================================"
}

# Function to check file and display
check_file() {
    local file=$1
    local desc=$2
    if [ -f "$file" ]; then
        echo "✅ $desc"
        return 0
    else
        echo "❌ $desc (NOT FOUND)"
        return 1
    fi
}

# Check Prerequisites
section "1. Prerequisites Check"

echo -n "ClickHouse Schema: "
docker exec trading_clickhouse clickhouse-client --query \
    "SELECT count() FROM system.tables WHERE database='trading' AND name LIKE 'ml_%'" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "✅ ClickHouse accessible"
else
    echo "❌ ClickHouse not accessible"
fi

echo ""
echo -n "ML Scheduler: "
if pgrep -f "ml_scheduler.py" > /dev/null; then
    echo "✅ Running (PID: $(pgrep -f ml_scheduler.py))"
else
    echo "❌ NOT RUNNING"
fi

echo -n "Consumer: "
if pgrep -f "control_event_consumer.py" > /dev/null; then
    echo "✅ Running (PID: $(pgrep -f control_event_consumer.py))"
else
    echo "❌ NOT RUNNING"
fi

# Check Brain Outputs
section "2. Brain Outputs"

check_file "$RUNS_DIR/ml_review.json" "ml_review.json"
if [ -f "$RUNS_DIR/ml_review.json" ]; then
    echo ""
    echo "Latest Review:"
    cat "$RUNS_DIR/ml_review.json" | jq '{
        symbol,
        action,
        action_rationale,
        confidence_final,
        success_gates_met: .success_gates_met // false
    }' 2>/dev/null || echo "  (Invalid JSON or missing fields)"
fi

echo ""
check_file "$RUNS_DIR/ml_actions.jsonl" "ml_actions.jsonl"
if [ -f "$RUNS_DIR/ml_actions.jsonl" ]; then
    echo ""
    echo "Last 3 Actions:"
    tail -3 "$RUNS_DIR/ml_actions.jsonl" | jq '{
        symbol,
        action,
        status,
        confidence
    }' 2>/dev/null
fi

# Check OOF Eval
section "3. OOF Evaluation"

check_file "$RUNS_DIR/oof_eval/$SYMBOL.json" "oof_eval/$SYMBOL.json"
if [ -f "$RUNS_DIR/oof_eval/$SYMBOL.json" ]; then
    echo ""
    echo "Current OOF Metrics:"
    cat "$RUNS_DIR/oof_eval/$SYMBOL.json" | jq '{
        symbol,
        n_bars: .metrics.n_bars,
        oos_avg_sharpe: .metrics.oos_avg_sharpe,
        oos_avg_ic: .metrics.oos_avg_ic,
        oos_avg_pf: .metrics.oos_avg_profit_factor
    }' 2>/dev/null
fi

# Check Queue
section "4. Job Queue"

check_file "runs/ai_trader/queue/ml_jobs.jsonl" "ml_jobs.jsonl"
if [ -f "runs/ai_trader/queue/ml_jobs.jsonl" ]; then
    echo ""
    echo "Recent Jobs for $SYMBOL:"
    grep "$SYMBOL" runs/ai_trader/queue/ml_jobs.jsonl | tail -3 | jq '{
        job_id,
        action,
        status,
        params: .params | {symbols, n_trials, timeout_hours}
    }' 2>/dev/null
fi

# Check ML Events
section "5. ML Events"

if [ -f "$RUNS_DIR/ml_events.jsonl" ]; then
    echo "✅ ml_events.jsonl"
    echo ""
    echo "Recent ML Job Events:"
    grep "ml.job" "$RUNS_DIR/ml_events.jsonl" | tail -3 | jq '{
        event,
        status,
        symbol,
        metrics: .metrics // {}
    }' 2>/dev/null
else
    echo "❌ ml_events.jsonl (NOT FOUND)"
fi

# Check ClickHouse
section "6. ClickHouse Data"

echo "Latest Evaluations for $SYMBOL:"
docker exec trading_clickhouse clickhouse-client --query "
SELECT
    trial_id,
    oos_avg_sharpe,
    oos_avg_ic,
    oos_avg_profit_factor,
    delta_sharpe,
    delta_ic,
    is_success,
    is_big_success,
    updated_at
FROM trading.ml_eval_with_delta
WHERE symbol = '$SYMBOL'
ORDER BY updated_at DESC
LIMIT 3
FORMAT PrettyCompact" 2>/dev/null

echo ""
echo "30-Day Stats for $SYMBOL:"
docker exec trading_clickhouse clickhouse-client --query "
SELECT
    symbol,
    attempts_30d,
    successes_30d,
    success_rate_30d,
    big_successes_30d,
    latest_sharpe,
    latest_pf
FROM trading.ml_model_stats_30d
WHERE symbol = '$SYMBOL'
ORDER BY last_eval_time DESC
LIMIT 1
FORMAT PrettyCompact" 2>/dev/null

# Success Gates Check
section "7. Success Gates Check"

echo "Checking latest evaluation against four-dimensional gates..."
echo ""

GATES_QUERY="
SELECT
    symbol,
    oos_avg_sharpe >= 1.2 as sharpe_ok,
    oos_avg_profit_factor >= 1.2 as pf_ok,
    oos_avg_ic >= 0.02 as ic_ok,
    (delta_sharpe >= 0.02 OR delta_ic >= 0.0) as delta_ok,
    delta_max_dd <= 0.03 as dd_ok,
    is_success,
    is_big_success
FROM trading.ml_eval_with_delta
WHERE symbol = '$SYMBOL'
ORDER BY updated_at DESC
LIMIT 1
FORMAT PrettyCompact"

docker exec trading_clickhouse clickhouse-client --query "$GATES_QUERY" 2>/dev/null

# Logs
section "8. Recent Logs"

if [ -f "$RUNS_DIR/ml_scheduler.log" ]; then
    echo "Scheduler Log (last 5 lines):"
    tail -5 "$RUNS_DIR/ml_scheduler.log"
else
    echo "❌ Scheduler log not found"
fi

echo ""
echo "======================================================================"
echo "Monitor Complete"
echo "======================================================================"
echo ""
echo "Useful Commands:"
echo "  Watch Brain review:    watch -n 5 cat $RUNS_DIR/ml_review.json | jq ."
echo "  Watch actions:         tail -f $RUNS_DIR/ml_actions.jsonl | jq ."
echo "  Watch events:          tail -f $RUNS_DIR/ml_events.jsonl | jq ."
echo "  Watch scheduler:       tail -f $RUNS_DIR/ml_scheduler.log"
echo "  Re-run this monitor:   ./tools/brain_test_monitor.sh $SYMBOL"
echo ""
