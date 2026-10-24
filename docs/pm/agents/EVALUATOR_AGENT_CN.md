# Evaluator Agent

## 1. 角色定位

Evaluator Agent 是统一裁判，不发明策略，只做结果归一化、排序和晋级建议。

## 2. 职责

- 读取所有 `ExperimentResult`
- 过滤无效或低质量实验
- 统一计算排名分数
- 产出 `PromotionDecision`
- 产出 `PortfolioEvaluation`

## 3. 输入

- 股票实验结果
- 期权实验结果
- ML Brain 解释信息
- Data Readiness 警告

## 4. 输出

```json
{
  "agent_role": "evaluator",
  "status": "completed",
  "leaderboard": [
    {
      "experiment_id": "exp-options-spy-003",
      "score": 87.4
    }
  ],
  "promotion_decision": {
    "winners": ["exp-options-spy-003"]
  }
}
```

## 5. 评分原则

- 先过质量门槛，再比较收益。
- Trade count 太少的实验不能因为高收益直接获胜。
- Drawdown 与 coverage 要作为硬条件。
- complexity penalty 与 instability penalty 必须进入评分。
- 股票和期权必须做 cost normalization 后再比较。
- robustness 不通过的实验不得晋级。

## 6. 关键约束

- 不直接运行回测
- 不直接生成新实验
- 不因为单一指标最好就自动晋级

## 7. MVP 任务

- 定义统一 `score`
- 定义最小质量门槛
- 输出 leader board 和 retest queue
- 输出候选组合级别的 `PortfolioEvaluation`

## 8. 验收标准

- 股票和期权实验能进入同一份榜单
- 结果可以被 Director 直接用于下一轮任务拆分
- 结果不只是单实验排序，还能支持组合层决策
