# Learning Pipeline Plugins

## 目标
- 把学习链路从“单策略硬编码”升级为“模块化、可插拔、多策略兼容”。
- 任何策略都通过 `learning_profile` 接入，不再要求修改 Orchestrator 核心逻辑。

## 核心概念
- `learning_profile`:
  - 策略在 `config.yml -> strategies[*]` 中声明的学习类型。
  - 例如：`enhanced_ml`、`qqq_intraday`、`options_spread`。
- `LearningAdapter`:
  - 每个 profile 对应一个 adapter，封装该策略的学习细节。
- `learning_targets`:
  - 本轮学习要处理的策略 ID 列表；可单策略，也可多策略并行。

## 推荐配置
`config.yml` 示例：

```yaml
strategies:
  - id: qqq_intraday_001
    enabled: true
    learning_profile: qqq_intraday
    learning:
      apply_params_cmd: "venv/bin/python apply_extras_params.py --config-yaml config.yml --strategy-id qqq_intraday_001"
    params:
      extras: {}

  - id: enhanced_ml_001
    enabled: false
    learning_profile: enhanced_ml
    params:
      extras: {}
```

`scripts/ai_trader/config/ai_trader.yaml` 示例：

```yaml
policies:
  learning:
    default_targets:
      - qqq_intraday_001
    fallback_profile: generic
```

## Adapter 接口（建议）
```python
class LearningAdapter(Protocol):
    def discover_symbols(self, strategy_cfg: dict) -> list[str]: ...
    def build_training_context(self, strategy_cfg: dict, run_ctx: dict) -> dict: ...
    def run_hpo(self, strategy_cfg: dict, run_ctx: dict) -> dict: ...
    def apply_params(self, strategy_cfg: dict, best_params: dict, run_ctx: dict) -> dict: ...
    def validate_metrics(self, learning_output: dict, gates_cfg: dict) -> dict: ...
    def emit_artifacts(self, out_dir: str, payload: dict) -> None: ...
```

## 注册机制（建议）
- 文件：`scripts/ai_trader/learning/registry.py`
- 规则：
  - `profile -> adapter class` 显式注册。
  - 未命中 profile 时使用 `fallback_profile`。
  - adapter 初始化失败时仅影响该策略，不应阻断其它策略学习。

## 运行与观测约定
- 每个策略单独产物：
  - `runs/ai_trader/YYYY-MM-DD/learning/<strategy_id>/...`
- MLflow tags 最低要求：
  - `strategy_id`
  - `learning_profile`
  - `adapter_name`
  - `pipeline_version`
- 日志事件最低要求：
  - `learn_started`
  - `learn_completed`
  - `learn_failed`
  - `apply_completed`

## 兼容策略
- 向后兼容原则：
  - 未声明 `learning_profile` 的策略走 `fallback_profile`。
  - 旧 `apply_extras_params.py` 可作为 `generic` adapter 的默认回写实现。
  - 若策略声明 `learning.apply_params_cmd`，则优先使用该命令进行参数回写。

## 验收标准（文档与实现一致）
- 新增策略只需改配置（`id` + `learning_profile`），无需改 Orchestrator 主流程。
- 多策略学习时，单策略失败不会导致整轮学习失败。
- 文档中不再出现“学习链路仅支持某一个固定 strategy_id”的描述。

## 升级 TODO（可执行）
- [x] Phase-1: 抽象 `LearningPipeline` / `LearningAdapter` / `registry`，替换 Orchestrator 中硬编码 strategy_id。
- [x] Phase-2: `apply_params` 路由支持按策略覆盖（`learning.apply_params_cmd` + `--strategy-id` 动态注入）。
- [x] Phase-3: Brain 决策上下文升级为多目标（`targets` + `primary_strategy_id`），并保留 `strategy_id` 兼容。
- [x] Phase-4: 学习产物目录按策略拆分（`runs/.../learning/<strategy_id>/`）并统一索引。
- [x] Phase-5: 多策略并发学习编排（隔离失败、聚合结果、统一回写策略）。
- [x] Phase-6: 可观测性完善（MLflow tags/event schema 增加 `learning_profile/adapter_name/pipeline_version`）。

## 分阶段验收标准
- Phase-1 验收:
  - `scripts/ai_trader/learning/` 存在 `base.py/registry.py/pipeline.py` 且被 Orchestrator 使用。
  - 代码中不再把 `enhanced_ml_001` 作为学习链路唯一目标。
- Phase-2 验收:
  - `RunSystemController.apply_params` 支持 `cmd_override/strategy_id`。
  - 配置 `learning.apply_params_cmd` 后可对指定策略生效。
- Phase-3 验收:
  - 所有 Orchestrator 内的 Brain 决策通过统一入口注入 `targets`。
  - 输入上下文同时包含 `primary_strategy_id` 与兼容字段 `strategy_id`。
  - 单测覆盖 `_decision_context` 与 `_brain_decide`。
- Phase-4 验收:
  - 每个策略学习产物独立落盘，支持按策略回放与审计。
  - 生成 `runs/.../learning/index.json`，可统一检索每个策略的关键产物路径。
- Phase-5 验收:
  - 多策略并发时，某策略失败只影响该策略，不阻断其它策略完成。
  - 回写支持 `policies.learning.writeback_scope: all_targets`；默认 `primary` 保持旧行为。
  - 并发度支持 `policies.learning.max_parallel_targets` 控制。

## 新增配置（Phase-5）
`scripts/ai_trader/config/ai_trader.yaml` 示例：

```yaml
policies:
  learning:
    pipeline_version: learning-pipeline-v1
    writeback_scope: all_targets   # primary | all_targets
    max_parallel_targets: 2
```
- Phase-6 验收:
  - MLflow 与事件日志可按 `strategy_id + learning_profile + adapter_name` 维度检索。
  - 关键事件（如 `hpo_complete`、`rl_eval_complete`）包含 `pipeline_version` 与 `learning_targets`。
