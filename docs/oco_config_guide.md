一、如何修改 OCO 配置（超简单）

  只需要修改一个文件：策略的 JSON 配置文件

  # 例如修改 TSLA 策略的风控参数
  vim strategies/configs/enhanced_ml_TSLA.json

  修改这个部分：
  {
    "oco": {
      "take_profit_bp": 250,    // 改这里：止盈 (250 = 2.5%)
      "hard_stop_bp": 180,      // 改这里：止损 (180 = 1.8%)
      "trail_atr_mult": 2.5     // 改这里：追踪止损倍数
    }
  }

  重启策略生效：
  pkill -f run_system.py
  python run_system.py

  就这么简单！不需要动 config.yml，不需要改代码。

  ---
  二、系统层面风控架构

  系统有三道防线管理风险：

  1️⃣ Guardian（守护者）

  位置: services/risk/guardian.py作用: 系统级熔断机制

  # config.yml 中配置
  risk_management:
    guardian:
      dd_intraday_pct: 0.02        # 当日回撤 > 2% → 触发
      max_trades_per_min: 10       # 每分钟交易 > 10 → 触发
      max_gross_exposure_pct: 0.5  # 总仓位 > 50% → 触发
      on_trip: "reduce"            # 触发后动作: reduce/flat/pause

  触发后行为:
  - reduce: 减仓到 60%
  - flat: 全部平仓
  - pause: 暂停交易 (cooldown 期间)

  2️⃣ OCO（One-Cancels-Other）

  位置: services/exec/safety_kernel.py作用: 订单级别的止盈止损保护

  工作流程:
  主订单成交 → SafetyKernel监听 → 自动下OCO订单 (< 100ms)

  OCO包含:
  - LMT 止盈单 (entry_price × (1 + take_profit_bp/10000))
  - STP 止损单 (entry_price × (1 - hard_stop_bp/10000))

  配置优先级:
  1. 策略 JSON 中的 oco 字段（最高优先级）
  2. 系统默认值 (config.yml 或 translator.py 中的 defaults)

  3️⃣ RiskValidator（风险验证器）

  位置: services/decision/risk_validator.py作用: 订单提交前的最后一道检查

  检查项:
  - 单笔订单大小是否超限
  - 持仓集中度是否过高
  - 杠杆倍数是否合规
  - 是否在冷却期内 (Guardian触发后)

  ---
  三、完整风控流程图

  策略信号
     ↓
  DecisionPipeline
     ↓
  RiskValidator ← Guardian (检查是否在熔断/冷却期)
     ↓
  Translator (读取 oco_cfg)
     ↓
  ExecutionService (下主订单)
     ↓
  SafetyKernel (监听成交 → 自动挂OCO)
     ↓
  OCO订单激活 (止盈/止损保护生效)

  ---
  四、不同层次管理不同风险

  | 层次  | 组件            | 管什么           | 配置在哪里                                 |
  |-----|---------------|---------------|---------------------------------------|
  | 策略级 | Strategy JSON | 每个策略的 OCO 参数  | strategies/configs/*.json             |
  | 系统级 | Guardian      | 整体账户风控（回撤、暴露） | config.yml → risk_management.guardian |
  | 订单级 | RiskValidator | 单笔订单合规性       | config.yml → risk_management.limits   |
  | 执行级 | SafetyKernel  | 自动止盈止损保护      | 自动执行（读取 oco_cfg）                      |

  ---
  五、实际例子

  场景：TSLA 策略需要更激进的止盈止损

  1. 修改文件:
  vim strategies/configs/enhanced_ml_TSLA.json

  2. 修改内容:
  "oco": {
    "take_profit_bp": 300,    // 从 250 改到 300 (2.5% → 3.0%)
    "hard_stop_bp": 200,      // 从 180 改到 200 (1.8% → 2.0%)
    "trail_atr_mult": 3.0     // 从 2.5 改到 3.0
  }

  3. 重启:
  pkill -f run_system.py && python run_system.py

  4. 验证:
  # 查看日志确认加载
  tail -f logs/trading_system.log | grep "OCO config loaded"

  # 应该看到：
  # strategy_id=enhanced_ml_001 take_profit_bp=300 hard_stop_bp=200

  ---
  总结：你只需要关心 JSON 文件，系统会自动处理风控流程。