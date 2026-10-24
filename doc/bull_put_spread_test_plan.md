# Bull Put Spread Test Plan

该测试计划用于指导 AI/脚本执行对 Bull Put Spread 策略的完整验证，覆盖本地单测、stub 集成测试、IBKR paper 实测、以及运维脚本的健康检查。所有命令均需在仓库根目录执行（已激活 `venv`）。

---

## 1. 单元测试 (G1)
- **目的**：快速验证策略内部逻辑（趋势判断、spread 选择、TP/SL 触发、available funds 守卫等）。
- **命令**：
  ```bash
  pytest -q tests/unit/strategies/test_bull_put_spread_strategy.py
  ```
- **期望**：31 个测试全部通过；若失败会打印具体失败用例，需修复后重跑。

---

## 2. Stub 集成测试 (G2)
利用 stub provider（`provider.backend=stub`）在本地模拟 entry→exit 流程，确认 audit / Postgres 写入正确。

步骤：
1. 将 `config.yml -> bull_put_spread_qqq -> provider.backend` 暂时设置为 `stub`，`transmit=true`（或直接在环境变量里覆盖）。
2. 运行策略一次（可限制在 few cycles）：
   ```bash
  python run_system.py --max-cycles 1 --strategy bull_put_spread_qqq
  ```
3. 检查审计文件与数据库：
   - 审计：`runs/ai_trader/<today>/bull_put_spread.jsonl` 应包含 `enter_submitted`, `entry_filled`, `state_change` 等事件，含 `order_id/order_status/state`。
   - 数据库：执行
     ```bash
     PG_PASSWORD=... make apply-option-schema  # 若未执行过
     python scripts/check_positions_consistency.py --symbol QQQ
     python scripts/check_bull_put_health.py --date <today>
     ```
4. 确认 `trading.option_spread_trades` 中出现 `OPEN_SUBMITTED`, `OPEN_FILLED`, `STATE_OPEN`, `STATE_CLOSING`, `STATE_CLOSED` 等记录，字段 `order_id/order_status/filled_*` 有值。

---

## 3. IBKR Paper 实测 (G3)
在纸面 IBKR 环境中运行策略，验证实时数据、订单流、以及对账链路。

### 3.1 准备
- `.env` 已设置 `IB_HOST/IB_PAPER_PORT/IB_ACCOUNT` 等变量；`TRADING_MODE=paper`.
- 确保 `make apply-option-schema` 已执行（表字段存在）。
- Kafka/Redis/ClickHouse 处于启动状态（`make up`、`make recon-all`）。

### 3.2 执行
1. 启动策略（建议白天纸面模拟）：
   ```bash
   python run_system.py --strategy bull_put_spread_qqq --max-cycles 10
   ```
2. 期间观察：
   - `logs/trader.log`：应出现 `bull_put_enter`, `enter_submitted`, `entry_filled`, `state_change` 等日志。
   - `runs/ai_trader/<today>/bull_put_spread.jsonl`：事件包含 `order_id/order_status/state`，TP/SL/close 也有 `state_change`。
   - `check_positions_consistency.py`：确认 IB vs DB 仓位一致。
   - `check_bull_put_health.py`：观察 `entry_filled/enter_submitted` 比例。

3. 结束后，用 Grafana dashboard（`dashboards/bull_put_spread_dashboard.json` 导入）查看：
   - Entry Fill Ratio 是否 >= 0.5（可设置阈值告警）。
   - State Distribution 中是否存在异常状态（如大量 `PENDING` 或 `CLOSING` 长时间滞留）。

---

## 4. 运维脚本与对账
这些脚本已挂入 `recon-all`，也可单独执行：

| 脚本/命令 | 作用 |
|-----------|------|
| `make check-positions` | IB vs DB 仓位对账，防止幽灵仓位 |
| `make check-bull-put-health` | 审计文件健康检查，输出 `entry_filled/enter_submitted` 比例与 state 分布 |
| `make apply-option-schema` | 数据库迁移（已执行过可跳过；需 `PG_PASSWORD`） |

执行顺序建议：`make check-bull-put-health` → `make check-positions`；若出现异常（退出码非 0），根据日志排查。

---

## 5. 验收标准
1. 单元测试全通过。
2. Stub 集成测试生成的审计 / DB 记录符合预期，`pending_exit` / `state_change` 事件正常。
3. IBKR Paper 运行中无 ghost position、Kafka / ClickHouse 错误，`state_change` 能完整反映 `OPEN→CLOSING→CLOSED`。
4. 对账脚本和健康检查脚本返回成功（退出码 0），Grafana 面板可直接导入并显示数据。

如任一环节失败，请整理日志（`logs/trader.log`、`runs/ai_trader/...`、`option_spread_trades` 中对应记录）并回滚/修复后重测。
***
