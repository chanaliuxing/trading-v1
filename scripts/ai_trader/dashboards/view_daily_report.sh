#!/bin/bash
# 每日战报查看器 - AI Trader Daily Report Viewer
# 用途：展示每天的诊断、处方、HPO/RL 结果、守护者活动等高层总结

set -euo pipefail

TODAY=$(date +%Y-%m-%d)
RUNS_DIR="./runs/ai_trader"
REPORT_FILE="${RUNS_DIR}/${TODAY}/daily_report.md"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 AI Trader 每日战报 - $(date '+%Y-%m-%d %H:%M:%S')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 功能：选择日期查看
function select_date() {
    echo "📅 可用的战报日期："
    echo ""

    # 列出最近 10 天的报告
    local reports=$(find "$RUNS_DIR" -maxdepth 2 -name "daily_report.md" 2>/dev/null | sort -r | head -10)

    if [ -z "$reports" ]; then
        echo "   ⚠️  未找到任何战报"
        echo "   提示：请先运行 evening_learning 生成报告"
        return 1
    fi

    local count=1
    declare -A date_map

    echo "$reports" | while read report_path; do
        report_date=$(basename $(dirname "$report_path"))
        report_size=$(stat -f%z "$report_path" 2>/dev/null || stat -c%s "$report_path" 2>/dev/null)
        echo "   [$count] $report_date ($(numfmt --to=iec-i --suffix=B $report_size 2>/dev/null || echo \"${report_size}B\"))"
        date_map[$count]=$report_date
        count=$((count + 1))
    done

    echo ""
    echo -n "选择日期 [1-10] 或按 Enter 查看今日: "
    read -r choice

    if [ -z "$choice" ]; then
        selected_date="$TODAY"
    else
        selected_date="${date_map[$choice]}"
    fi

    REPORT_FILE="${RUNS_DIR}/${selected_date}/daily_report.md"
}

# 功能：渲染 Markdown 到终端
function render_report() {
    local file="$1"

    if [ ! -f "$file" ]; then
        echo "❌ 战报文件不存在: $file"
        echo ""
        echo "💡 可能原因："
        echo "   1. 今天还未运行 evening_learning"
        echo "   2. 运行失败，未生成报告"
        echo ""
        echo "🔧 解决方法："
        echo "   运行: PYTHONPATH=. python scripts/ai_trader/cli.py run --mode learn"
        return 1
    fi

    # 如果安装了 glow (Markdown 终端渲染器)，使用它
    if command -v glow &> /dev/null; then
        glow -p "$file"
    # 如果安装了 bat (带高亮的 cat)
    elif command -v bat &> /dev/null; then
        bat --style=grid --language=markdown "$file"
    # 否则使用原生 cat
    else
        cat "$file"
    fi

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# 功能：生成 HTML 版本并在浏览器打开
function open_in_browser() {
    local md_file="$1"
    local html_file="/tmp/ai_trader_daily_report_$(date +%s).html"

    echo ""
    echo "🌐 正在生成 HTML 版本..."

    # 简单的 Markdown → HTML 转换
    cat > "$html_file" <<'EOF'
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AI Trader Daily Report</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', sans-serif;
            max-width: 1200px;
            margin: 40px auto;
            padding: 0 20px;
            line-height: 1.6;
            color: #333;
        }
        h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }
        h2 { color: #34495e; margin-top: 30px; border-left: 4px solid #3498db; padding-left: 15px; }
        h3 { color: #7f8c8d; }
        code {
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Monaco', 'Courier New', monospace;
        }
        pre {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
            border-left: 3px solid #3498db;
        }
        table {
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        th, td {
            border: 1px solid #ddd;
            padding: 12px;
            text-align: left;
        }
        th {
            background: #3498db;
            color: white;
            font-weight: bold;
        }
        tr:nth-child(even) { background: #f8f9fa; }
        .emoji { font-size: 1.2em; }
        .metric {
            background: #e8f5e9;
            padding: 10px;
            border-radius: 5px;
            margin: 10px 0;
        }
    </style>
</head>
<body>
EOF

    # 如果安装了 pandoc，使用它转换
    if command -v pandoc &> /dev/null; then
        pandoc "$md_file" -f markdown -t html >> "$html_file"
    else
        # 否则使用简单的 sed 替换（基础转换）
        sed -e 's/^# \(.*\)/<h1>\1<\/h1>/' \
            -e 's/^## \(.*\)/<h2>\1<\/h2>/' \
            -e 's/^### \(.*\)/<h3>\1<\/h3>/' \
            -e 's/^\- \(.*\)/<li>\1<\/li>/' \
            -e 's/\*\*\(.*\)\*\*/<strong>\1<\/strong>/g' \
            -e 's/\`\([^\`]*\)\`/<code>\1<\/code>/g' \
            "$md_file" >> "$html_file"
    fi

    echo "</body></html>" >> "$html_file"

    echo "✅ HTML 已生成: $html_file"

    # 在默认浏览器中打开
    if [[ "$OSTYPE" == "darwin"* ]]; then
        open "$html_file"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        xdg-open "$html_file"
    else
        echo "   请手动打开: $html_file"
    fi
}

# 功能：显示相关文件
function show_related_files() {
    local run_dir=$(dirname "$REPORT_FILE")

    echo ""
    echo "📁 相关文件（位于 $run_dir）："
    echo ""

    # HPO 决策
    if [ -f "$run_dir/hpo_decision.json" ]; then
        echo "   ✅ HPO 决策: $run_dir/hpo_decision.json"
    else
        echo "   ⚠️  HPO 决策: 未生成"
    fi

    # RL 决策
    if [ -f "$run_dir/rl_decision.json" ]; then
        echo "   ✅ RL 决策: $run_dir/rl_decision.json"
    else
        echo "   ⚠️  RL 决策: 未生成"
    fi

    # 反思日志
    if [ -f "$run_dir/reflection.json" ]; then
        echo "   ✅ 反思日志: $run_dir/reflection.json"
    else
        echo "   ⚠️  反思日志: 未生成"
    fi

    # Brain 计划
    brain_plans=$(find "$run_dir" -name "brain_plan_*.json" 2>/dev/null | wc -l)
    if [ "$brain_plans" -gt 0 ]; then
        echo "   ✅ Brain 计划: $brain_plans 个文件"
    else
        echo "   ⚠️  Brain 计划: 未生成"
    fi

    # Tools 日志
    if [ -f "$run_dir/tools_ledger.jsonl" ]; then
        tool_events=$(wc -l < "$run_dir/tools_ledger.jsonl")
        echo "   ✅ Tools 日志: $tool_events 条事件"
    else
        echo "   ⚠️  Tools 日志: 无事件"
    fi
}

# 主菜单
function main_menu() {
    while true; do
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "📋 战报操作菜单"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "   [1] 查看今日战报"
        echo "   [2] 选择历史战报"
        echo "   [3] 在浏览器中打开"
        echo "   [4] 显示相关文件"
        echo "   [5] 查看最近 5 天汇总"
        echo "   [q] 退出"
        echo ""
        echo -n "请选择 [1-5/q]: "
        read -r action

        case "$action" in
            1)
                REPORT_FILE="${RUNS_DIR}/${TODAY}/daily_report.md"
                render_report "$REPORT_FILE"
                ;;
            2)
                select_date
                render_report "$REPORT_FILE"
                ;;
            3)
                if [ -f "$REPORT_FILE" ]; then
                    open_in_browser "$REPORT_FILE"
                else
                    echo "❌ 请先选择一个有效的战报"
                fi
                ;;
            4)
                show_related_files
                ;;
            5)
                echo ""
                echo "📊 最近 5 天战报汇总："
                echo ""
                for i in {0..4}; do
                    past_date=$(date -v-${i}d +%Y-%m-%d 2>/dev/null || date -d "$i days ago" +%Y-%m-%d 2>/dev/null)
                    past_report="${RUNS_DIR}/${past_date}/daily_report.md"
                    if [ -f "$past_report" ]; then
                        echo "   ✅ $past_date: $(grep -c '^##' "$past_report" 2>/dev/null || echo 0) 个章节"
                        # 提取第一个 H2 标题作为概要
                        summary=$(grep -m 1 '^## ' "$past_report" 2>/dev/null || echo "无概要")
                        echo "      └─ $summary"
                    else
                        echo "   ⚠️  $past_date: 无战报"
                    fi
                done
                ;;
            q|Q)
                echo ""
                echo "👋 再见!"
                exit 0
                ;;
            *)
                echo "❌ 无效选择"
                ;;
        esac
    done
}

# 如果提供了参数 --auto，直接显示今日战报
if [ "${1:-}" = "--auto" ]; then
    render_report "$REPORT_FILE"
    show_related_files
    exit 0
fi

# 否则进入交互式菜单
main_menu
