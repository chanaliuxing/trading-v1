# ML Brain Agent

## 1. 角色定位

ML Brain Agent 不是新建的角色，而是把现有 `review_ml` 和 `evening_learning` 能力纳入统一研究工厂。

## 2. 职责

- 分析 label、feature、OOF、HPO 结果
- 提出特征组合和参数搜索建议
- 提供 signal diagnostics 与 robustness evidence
- 给 Stocks / Options agent 提供搜索空间 patch
- 不直接越权执行策略回测

## 3. 输入

- `review_ml` 上下文
- HPO / OOF 结果
- label profile
- 可用特征与 schema 摘要

## 4. 输出

```json
{
  "agent_role": "ml_brain",
  "status": "completed",
  "search_space_patch": {
    "preferred_features": ["vwap", "atr_eff", "vol_surge"],
    "rejected_features": ["unstable_feature_x"],
    "parameter_hints": {
      "entry_threshold": [0.55, 0.65]
    }
  },
  "robustness_evidence": {
    "wf_pass": true,
    "leakage_score": 0.01,
    "regime_stability": 0.72
  },
  "notes": [
    "prefer short holding period for current intraday label horizon"
  ]
}
```

## 5. 与现有系统的关系

- 复用 `scripts/ai_trader/brain/brain.py` 的 `review_ml`
- 复用 `orchestrator.py` 的 `evening_learning()`
- 不替代现有 ML job 执行器

## 6. 关键约束

- 只能推荐注册表内可执行模块
- 不直接改生产参数
- 不直接生成自由文本策略后强行执行
- robustness evidence 必须结构化，不只写自然语言评价

## 7. MVP 任务

- 把 `review_ml` 结果转成结构化搜索空间 patch
- 把已有 walk-forward / leakage / drift 等结果转成结构化 robustness evidence
- 为股票和期权研究各给一份建议
- 限定在 intraday scalp 场景

## 8. 验收标准

- Stocks / Options agent 能消费其输出
- 输出不包含未注册特征或虚构字段
- Evaluator 能直接消费其 robustness evidence
