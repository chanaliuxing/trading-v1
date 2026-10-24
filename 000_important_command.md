pgrep -fl "auto_loop|ml_scheduler"

pkill -f run_system.py || true
#停止ai大脑
pkill -f auto_loop.py
pkill -f ml_scheduler.py
pkill -f llm_trader.py

#停止llm_trader
## 监测 AI
bash scripts/ai_trader/status.sh 
  1. MLflow (策略学习与进化)
    - 🌐 http://localhost:5001
    - 功能：HPO 参数进化、RL 性能趋势、实验对比
  2. Grafana (系统实时状态)
    - 🌐 http://localhost:3000 (admin/admin)
    - 推荐仪表盘：http://localhost:3000/d/hybrid-trading-3ds
    - 功能：实时 P&L、LLM 决策、进程心跳
  3. Daily Report (每日战报)
    - 💻 bash scripts/ai_trader/dashboards/view_daily_report.sh
## 启动AI
python scripts/ai_trader/auto_loop.py


## 启动docker
 管理命令

  # 启动所有服务
  make up                    # 或: cd infra && docker-compose up -d

  # 停止所有服务
  make down                  # 或: cd infra && docker-compose down

  # 查看状态
  docker ps
  docker compose ls

  # 查看日志
  docker logs trading_clickhouse
  docker logs trading_kafka
## load history data in bulk:
python tool/load_hist_multi.py --symbols AAPL --bar-size 1m --sessions 200 --fill-mode backfill --chunk-days 7


backfill 昨天数据:

## google news
python ./llm_trader/scripts/googleNews.py

## 🔁 **4️⃣ Backup ClickHouse**
for example, use the name full_backup_20251010_043000 to backup:
makefile recon-all

## 如果不 push 到 github
git push origin main

## 清理 git 误判
git config core.fileMode false
git config core.ignorecase false
echo "* text=auto" > .gitattributes
git rm --cached -r .
git reset --hard



##生成 requirements
uv pip freeze > full_requirements.txt

## start services:
docker-compose up -d

## run codex cli
codex --sandbox danger-full-access
codex resume --last --sandbox danger-full-access  
codex exec "你的高层次任务描述"

## run claude code cli
claude -p "你的高层次任务描述" --allowedTools "Bash,Edit"
claude -p "你的高层次任务描述" --dangerously-skip-permissions
## run gemini cli
gemini -p "你的高层次任务描述"
-------------------------------------------
-----------LLL trader start----------------
## 开盘一小时交易
  python llm_trader/run_single_bot.py \
      --provider deepseek \
      --symbols TSLA \
      --interval 60 \
      --wait-for-market \
      --start-trading-at "9:40" \
      --stop-trading-at "10:30" \
      --close-at "10:45"


## 日交易
  python llm_trader/run_single_bot.py \
      --provider deepseek_nvidia \
      --symbols AAPL NVDA \
      --interval 600 \
      --cycles 33 \
      --close-at "15:59" \
      --wait-for-market
## 盘前交易
  方法 1: 把命令写成一行(推荐)

  python llm_trader/run_single_bot.py --provider deepseek_nvidia --symbols TSLA --interval 600 --cycles 33
   --close-at "09:35" --forbid-new-positions-before 9 --wait-for-premarket

  方法 2: 如果要用多行,反斜杠后面不能有空格

  python llm_trader/run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols TSLA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:35" \
  --forbid-new-positions-before 9 \
  --wait-for-premarket

## 只做隔夜盘

  python llm_trader/run_single_bot.py \
      --provider deepseek_nvidia \
      --symbols TSLA \
      --interval 600 \
      --cycles 50 \
      --wait-for-overnight \
      --close-at "03:50" \
      --forbid-new-positions-before 5
  # 1. 等待盘前交易 (4:00)
  python llm_trader/run_single_bot.py \
      --provider deepseek_nvidia \
      --symbols TSLA \
      --wait-for-premarket

  # 2. 等待常规市场 (9:30)
  python llm_trader/run_single_bot.py \
      --provider deepseek_nvidia \
      --symbols TSLA \
      --wait-for-market

  # 3. 等待盘后交易 (16:00)
  python llm_trader/run_single_bot.py \
      --provider deepseek_nvidia \
      --symbols TSLA \
      --wait-for-afterhours

  # 4. 等待隔夜交易 (20:00)
  python llm_trader/run_single_bot.py \
      --provider deepseek_nvidia \
      --symbols TSLA \
      --wait-for-overnight
---------------------------------------------

整的自动化流程

  你运行命令:
  venv/bin/python alpha_loop.py --config alpha_config_tsla_robust.json --provider coop_optuna --gens 2

  系统自动执行:

  📍 Generation 1 开始
    ├─ [1] Optuna TPE: 运行50次试验（自动）
    │    └─ 每次试验 → 回测 → 记录Sharpe
    │
    ├─ [2] 选择Top 10结果（自动）
    │
    ├─ [3] 调用LLM（DeepSeek R1）（自动）
    │    ├─ 发送: base_grid + top_is + objective
    │    ├─ 接收: llm_grid + rationale
    │    └─ 保存: gen_01/llm_refine.json ✓
    │
    ├─ [4] 调用ML（Random Forest）（自动）
    │    ├─ 训练: 使用前20个试验的数据
    │    ├─ 预测: 哪些参数范围有潜力
    │    └─ 保存: gen_01/ml_refine.json ✓
    │
    ├─ [5] 合并LLM + ML建议（自动）
    │    ├─ 50%权重 LLM grid
    │    ├─ 50%权重 ML grid
    │    ├─ 限制: 每参数≤6个值
    │    └─ 保存: gen_01/coop_merged.json ✓
    │
  📍 Generation 2 开始
    ├─ [1] Optuna TPE: 使用精炼网格，运行50次试验（自动）
    │    └─ 参数空间已从976,562,500 → ~3,000组合
    │
    ├─ [2] 找到更好的参数！（自动）
    │
    └─ [3] 输出最佳结果（自动）

  全程自动，无需人工干预！✨

  ---
  🎯 你只需要做的事

  1. 运行命令（一次）：
  venv/bin/python alpha_loop.py \
    --config alpha_config_tsla_robust.json \
    --provider coop_optuna \
    --gens 2 \
    --out_dir alpha_out/test \
    --k-top 10

  2. 等待完成（自动）：
  - Generation 1: ~30分钟（50次回测）
  - LLM+ML分析: ~2分钟
  - Generation 2: ~30分钟（50次回测）
  - 总计: ~1小时全自动运行

  3. 查看结果（完成后）：
  # 查看最佳参数
  cat alpha_out/test/best_params.json

  # 查看LLM建议
  cat alpha_out/test/gen_01/llm_refine.json

  # 查看合并后的网格
  cat alpha_out/test/gen_01/coop_merged.json

  ---
  💡 关键点

  - ✅ 三层全自动：Optuna TPE → LLM+ML → 合并 → Optuna TPE
  - ✅ 无需干预：你只需要运行命令，系统处理一切
  - ✅ 可追踪：每个步骤都保存JSON文件，可以审查
  - ✅ 可调节：通过配置文件控制权重、试验次数等



三大屏配置文件完整清单：

  🎯 大屏一：MLflow

  - ✅ start_mlflow.sh - 启动脚本
  - ✅ MLflow 自带 Web UI（无需 JSON 配置）
  - ✅ 数据存储：./mlruns/

  🎯 大屏二：Grafana - AI Trader Brain Monitor

  - ✅ ai_trader_dashboard.json - Dashboard 配置
  - ✅ clickhouse_datasource.json - 数据源配置
  - ✅ GRAFANA_SETUP.md - 部署文档

  🎯 大屏三：Daily Report

  - ✅ view_daily_report.sh - 查看器脚本
  - ✅ 数据来源：scripts/ai_trader/memory/daily_reports/*.md

  🛠️ 辅助工具

  - ✅ open_dashboards.sh - 一键打开 MLflow + Grafana
  - ✅ start_all_dashboards.sh - 统一启动脚本
  - ✅ README_DASHBOARDS.md - 完整使用指南
  - ✅ QUICK_REFERENCE.md - 快速参考
  - ✅ DEPLOYMENT_SUMMARY.md - 部署总结