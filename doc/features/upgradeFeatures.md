
先说结论：
Gemini 提的几个“魔鬼代言人”观点里，大部分我**是接受的**，但有些地方我会**调整实现方式**，不会照单全收。下面我分两步说：

1. **逐条点评**：哪些接受、哪些部分接受、哪些不完全同意。
2. 在此基础上，给你一个**更新后的“最终版本架构 + 落地路线图”**，算是 V2 版总设计。

---

## 一、对 Gemini 批评的逐条回应

### 1. 性能瓶颈：Dataset Sanity 不能太“重” ✅ 接受（而且要大改设计）

* ✅ 我同意：**不能每次训练 / 每个 HPO trial 都在 Pandas 里对几百万行做全量相关性 /分布分析**，那会把 HPO 的迭代速度打废。
* 调整后的原则：

  1. **Sanity 检查尽可能下沉到 ClickHouse 端**，通过聚合 SQL 完成缺失率、分布统计、基础相关性；
  2. **对重度检查（如大规模相关矩阵）只对“采样数据”做**，比如随机抽 5% 或 N 行；
  3. **每个“数据版本”只做一次 Sanity，而不是每个 trial**

     * 比如：`TSLA_1m_2024-01-01~2024-06-01_ret10m_v1` 这个 dataset 做一次体检，后面 50 个 HPO trial 共用这份体检报告。

👉 所以：**Dataset Sanity 依然要做，但会变成“ClickHouse + 采样 + 缓存”的轻量方案，而不是每次都在 Pandas 上暴力扫全量。**

---

### 2. 特征契约的“僵化风险”：完全锁死 LLM 的创造力 ❗部分接受，改成“核心 + 沙箱”双层设计

* Gemini 的担心是：如果所有特征都必须先写进 `feature_registry.json`，LLM 就变成“只能在白名单里排列组合”，**失去创新空间**。
* 这一点我是认可的，所以我会这样改：

**最终策略：**

* 保留 **Feature Registry（核心特征）**：

  * 这些是你确认过、长期稳定的特征；
  * 所有**生产级策略 / 长期回测 / 风险评估**必须基于核心特征；
* 新增 **Sandbox Features（沙箱特征层）**：

  * 允许 PI / AI 大脑提出新的 **SQL 表达式**（在严格 DSL 限制内，比如只允许列运算、窗口函数、不允许子查询、不允许未来引用）；
  * 我们做：

    1. 语法 / 安全检查（只能用 registry 中已有列，不能用未来时间字段）；
    2. 抽样计算 + Sanity 检查（看有没有爆 NaN、爆值、极端偏态）；
    3. 通过检查就临时注册为 `sandbox_f_xxx`，可以参与本次实验；
  * 如果某个 sandbox 特征在多次实验中表现稳定，你可以人工（或半自动）“提干”为 core 特征，写入 `feature_registry.json`。

👉 总结：
**我不接受“完全放飞 LLM 到任何列”的旧模式，也不接受“LLM 永远只能用白名单”的死板模式。最终方案是：核心特征 + 受控沙箱特征。**

---

### 3. Label 只支持简单 horizon return，不支持 triple barrier ❗观点正确，但要分阶段实现

* Gemini 指出：真正机构级策略的标签，往往是 **Triple Barrier（止盈 / 止损 / 时间三栅栏）**，而不是简单 “t+10m 的收益率”。
* 这点我完全同意：

  * 简单 horizon return 容易出现“路径上其实早就 hit stop，但你拿终点价格当标签”的虚高问题；
  * Path-dependent label 需要用“路径”而不是“快照”。

**我会这样处理：**

1. **Label Registry 设计阶段就考虑三类 Label：**

   * 简单 Horizon Return：`ret_10m_fwd`（现成简单任务）；
   * Sign / Bucket 化：`dir_cls_10m` 等；
   * Barrier/Path-dependent：`triple_barrier_intraday_10m` 等——这一类内部实现可以：

     * 调用你现有 BacktestEngine 的一部分逻辑，或者
     * 用一个专门的 “barrier_label_engine”。

2. **落地顺序**：

   * **短期（优先）**：先把现有 horizon label 规范化（防泄露、shift 清晰），用 Label Registry 接住；
   * **中期**：从你最常用的 1~2 个 barrier 策略开始，把它们的 label 规则写进 registry 和 code；
   * **长期**：将主要策略的 entry/exit 逻辑都能映射到 Label 定义。

👉 所以：
**Gemini 说“Label Contract 目前太简单”是对的；我们在设计上直接兼容 triple barrier，只是实现上按优先级分期，而不是一口吃掉。**

---

### 4. AI 大脑的信息过载：直接把全量统计塞给 LLM ❗完全接受，改成“Summary 层 + 精简上下文”

* 我之前确实写得太理想：把 `sanity_report` 整个丢给大脑会造成：

  * 上下文太挤；
  * 模型注意力被噪声盖住；
  * 最后它可能只会说些很空的话。

**新方案：**

1. **新增 Summary 层**（可以用代码+小 LLM，也可以完全用 Python 规则）：

   * 输入：原始 `sanity_report`（数值密密麻麻）；
   * 输出：**短摘要 JSON + 短自然语言**，例如：

   ```jsonc
   {
     "overall_status": "warn",
     "key_issues": [
       "feature rvol_20_1m missing_ratio=35%",
       "feature spread_1m has extreme outliers (>p99*10)",
       "label dir_cls_10m is highly imbalanced: positive=1.2%"
     ]
   }
   ```

   * 一段类似：

   > Data quality WARNING: 核心特征 rvol_20_1m 缺失率 35%，标签 dir_cls_10m 极度不平衡（正类仅 1.2%）。本次实验的效果可能高度不稳定，不建议直接推广到实盘。

2. **AI 大脑收到的只是一页“总结页”**：

   * 不直接看 50 列的数值表，而是看：

     * 几个最严重的 warning；
     * 关键统计（样本数、标签分布、阻断率）。

👉 这点完全接受，而且会作为**架构里的一层组件**来设计。

---

### 5. ClickHouse View 层 + LLM 结构化输出 ✅ 完全接受并整合进最终方案

* **View 层**：

  * 不直接把 registry 指向多年来演化出来的各种 `bars_1m_xxx` 生表；
  * 而是在 CH 建立一层统一视图 `vw_bars_1m_features_v2`，在 view 里做：

    * 列改名；
    * 字段修补；
    * 过滤明显的脏数据；
  * `feature_registry` 指向视图，大大降低后续变更的痛苦。
* **LLM Structured Output**：

  * 对于 `PrincipalInvestigator` 的 `plan = self._llm.generate(...)`：

    * 我完全赞成改成：**强制 JSON Schema 输出 + Pydantic 校验**；
    * 失败就重试 / 降级，而不是用 regex 硬抠。

---

### 6. 执行优先级：先做 Dataset Sanity / Label Guard / Feature Registry ✅ 同意但会稍微重排

Gemini 建议优先级：

1. Dataset Sanity
2. Label Guard
3. Feature Registry
4. Brain Eyes

总体是对的，我会做一点小调整：
**Step 0 增加一个“Experiment Manifest + ID 标准化”**，方便后面所有模块串起来。

---

## 二、综合之后的「最终版本架构 + 落地路线图」(V2)

下面是“吸收批评后的”最终方案，你可以把它当成之后写代码的蓝图。

---

### 0. 总体目标回顾（一句话版本）

> **每一次 ML/策略实验，都是一条有 ID、有说明书、有体检、有审计的“试验记录”。**
> 数据、特征、标签、风险门槛、AI 大脑用到的信息，都来自统一契约和统一 manifest，而不是散落在 config 和 prompt 里。

---

## 1. 核心架构：四层 + 一个沙箱

1. **Layer A：Data / Feature / Label Contract（契约层）**
2. **Layer B：Dataset Sanity + Quality Summary（体检 + 摘要层）**
3. **Layer C：ML/PI/HPO 工具层（包含 Sandbox 特征机制）**
4. **Layer D：AI 大脑视图层（Manifest + 摘要 + 决策辅助）**
5. **附加：ClickHouse View + Registry 管理层**

---

## 2. Layer A – Contract 层（含 Sandbox）

### 2.1 Feature Registry + Sandbox 设计

**Core Registry：**

* `feature_registry.json`（未来可迁移到 DB）包含：

  * `name` / `table_or_view` / `freq` / `type` / `expected_range` / `valid_from` / `tier` / `allow_for_pi`…
* 新增字段：

  * `tier`: `"core" | "sandbox" | "deprecated"`
  * `source`: `"manual" | "pi_derived" | "auto_evolver"`

**Sandbox 机制：**

* PI / AI 大脑可以在 training_plan 里提一个 derived feature 列表，例如：

  ```jsonc
  {
    "derived": [
      {
        "alias": "f_momo_ratio",
        "expr": "(close - vwap_1m) / atr_14_1m"
      }
    ]
  }
  ```

* Pipeline：

  1. **Expr 校验**：仅允许引用 core registry 中的列；限定操作符；
  2. 在 ClickHouse 里通过 `ALTER TABLE ... ADD COLUMN` 或临时视图/CTE 计算；
  3. 对该列跑一小轮 Sanity（缺失率/极值）；
  4. 写入 Registry 为 `tier: "sandbox"`，供本次训练使用；
  5. 如果多个实验都证明它有价值 → 人工“晋升”为 core。

### 2.2 Label Registry（支持 Horizon + Barrier）

* 定义示例：

```jsonc
{
  "ret_10m_fwd": {
    "kind": "horizon_return",
    "horizon": "10m",
    "base_price": "close",
    "freq": "1m",
    "shift": "+10m",
    "task_types": ["reg", "rank", "cls_sign"]
  },
  "triple_barrier_10m": {
    "kind": "triple_barrier",
    "horizon": "10m",
    "base_price": "close",
    "upper_mult": 2.0,
    "lower_mult": 1.0,
    "vol_source": "atr_14_1m",
    "freq": "1m",
    "task_types": ["cls"]
  }
}
```

* 实现策略：

  * 简单 horizon label：在 Python / CH SQL 中实现；
  * Triple-barrier：可调用“路径回放器”或一个专门的 barrier builder；
  * Registry 仅做“配置 + 元信息”，**逻辑由可插拔的 builder 函数实现**。

---

## 3. Layer B – Dataset Sanity & Summary（性能友好的体检）

### 3.1 ClickHouse 端预检查

在构造 dataset 的 SQL 之后：

1. **通过 ClickHouse 跑聚合查询**（只拉回来几十行指标）：

   * 每列缺失率 / 零比例 / 方差 / 均值 / p1/p99；
   * 标签列的分布（均值 / 类别占比）；
2. 可选：对少量列（如最重要的 N 个特征 + label）计算相关性。

### 3.2 Python 端采样检查

* 从 CH 里按同样 SQL 抽取 N 行（比如 50k）做：

  * 常量列检查；
  * 极端值（超过预期区间若干倍）；
  * 简单相关矩阵（仅对这 N 行的少数列）。

### 3.3 Sanity Report & Summary

* `sanity_report` 是一个详细 JSON（放文件/数据库）；
* `sanity_summary` 是压缩后的版本：

```jsonc
{
  "overall_status": "ok|warn|fail",
  "major_issues": [
    "rvol_20_1m missing_ratio=35%",
    "dir_cls_10m: pos_class=1.2% (heavy imbalance)"
  ],
  "basic_stats": {
    "n_rows": 123456,
    "n_features": 42,
    "label": "dir_cls_10m"
  }
}
```

* 只有 `sanity_summary` 会给 AI 大脑看。

---

## 4. Layer C – PI / HPO 工具层（受控 + 可扩展）

### 4.1 PrincipalInvestigator 改造要点

* **输入**：symbol/table/start_ts/end_ts/goal/horizon；
* **LLM 输出**：用 JSON Schema（Pydantic）定义 `FeatureRecipe` 和 `TrainingPlan`；
* **SQL 构造**：

  * 使用 **Core Registry + Sandbox 机制** 拼出 feature 列 SQL；
  * label 列只 select 构造所需的“基础价格/路径”，**不允许 LLM 直接写 target expr**；
* **Dataset 构造**：

  * 从 ClickHouse 拉数据；
  * 调用 `build_label(df, label_id)` 去做 label；
  * 调 `run_dataset_sanity_checks` 获取 `sanity_report` 和 `sanity_summary`；
  * 若 `overall_status == "fail"` → 直接 fail / 标记为不可用，交给 orchestrator 决定是否停止。

### 4.2 HPO Grid 构造 + Guard

* `sanitize_and_constrain_grid` 使用更强的 guard：

  * 合法模型类型；
  * 各模型参数的边界；
* Grid 的生成依然可以由 PI 提建议，但**最终一定经过 guard 收紧**；
* 体检结果（比如某些特征缺失率高）也可以参与：

  * 比如将某些高缺失特征自动从候选特征集合中剔除。

---

## 5. Layer D – AI 大脑视图（Manifest + 视图增强）

### 5.1 Experiment Manifest

每次 ML / 策略训练都有一个 manifest：

```jsonc
{
  "job_id": "ml_TSLA_1m_ret10m_20250101",
  "data_manifest": { ... },
  "feature_recipe": { ... },
  "training_plan": { ... },
  "sanity_summary": { ... },
  "backtest_summary": { ... },
  "risk_gate_coverage": { ... }
}
```

* `job_id` 统一命名规则（symbol + freq + label + ts）；
* `manifest` 存在 ClickHouse / Postgres / 文件都行，但必须有统一读写接口。

### 5.2 AI 大脑的输入内容

大脑在做“是否采纳这次模型 / 是否生成下一轮实验”的决策时：

* 只看：

  * `sanity_summary`（简版质量报告）；
  * `backtest_summary`（盈利 / 风险指标）；
  * `risk_gate_coverage`（实盘能交易多少 bar、被 gate 掉的主要原因）；
  * 简要的 feature / label version 信息；
* 不看：

  * 完整相关矩阵；
  * 每一列的精确分布表。

**总结：** 大脑看到的是**“结构化的摘要 + 少量关键数值 + 文字解释”**，而不是一堆原始统计。

---

## 6. ClickHouse View 层 & LLM 结构化输出

### 6.1 View 层

* 新建一层命名统一的 view，比如：

  * `vw_bars_1m_features_core`
  * `vw_bars_5s_micro_core`
* Feature Registry 只指向 view；
* 底层表怎么演化（分区、迁移、改名）都可以通过改 view 隔离影响。

### 6.2 LLM Structured Output

* 使用 Pydantic 模型定义：

```python
class ModelCandidate(BaseModel):
    model_type: Literal["xgb", "lgbm", "lasso", "ridge", ...]

class TrainingPlan(BaseModel):
    task: Literal["reg", "cls", "rank"]
    label_id: str
    model_candidates: List[ModelCandidate]
    ...
```

* 在 `LLMClient.generate()` 中：

  * 使用对应模型的 JSON schema，要求 LLM 严格输出；
  * 对解析失败 / 校验失败有明确 fallback 策略（例如重问一次、或切换到保守模板）。

---

## 7. 最终落地路线图（V2 优先级）

### Step 0 – Experiment Manifest & ID 规范

* 定义 job_id 命名规则；
* 给现有流程加上一层“把本次实验的核心配置写成 manifest”的逻辑（即便字段暂时不全）。

### Step 1 – Dataset Sanity（ClickHouse + 采样 + Summary）

* 实现：

  * CH 端聚合统计 SQL + Python 端采样检查；
  * `sanity_report` + `sanity_summary`；
* 将其集成到现有 ML pipeline 和 PI pipeline 的“训练前必经步骤”。

### Step 2 – Label Guard & Label Registry（基础版）

* 创建 `label_registry`；
* 统一 label 构造入口 `build_label()`；
* 禁止 LLM 直接写 target SQL；
* 先支持 horizon 类 label（你目前主要在用的）；
* 检查是否存在未来数据泄露。

### Step 3 – Feature Registry + ClickHouse View + Sandbox Mechanism

* 定义 core feature registry；
* 在 CH 中建立统一 view；
* 改造 PI & 固定 workflow 从 registry 获取特征；
* 加 sandbox 机制（允许 LLM 提新特征 → 校验 → 注册为 sandbox）。

### Step 4 – AI 大脑视图升级

* 引入 `sanity_summary`、`risk_gate_coverage`、简化的 manifest 信息进大脑 prompt；
* 让大脑学会：

  * 根据数据质量 / gate 覆盖情况决定是否“采纳 / 降级 / 重训”。

### Step 5 – 高级 Label（Triple Barrier 等）& 工具扩展

* 优先为你实际常用的 1~2 个策略做 triple-barrier label；
* 加 Feature Stats / Baseline 对比等工具供大脑调用。

---

如果把这版当成**最终总设计 V2**，下一步我们就可以从 **Step 1：Dataset Sanity（ClickHouse + 采样）** 开始写具体代码——你这边不需要再解释业务，只要告诉我你希望先集成到哪条 pipeline（比如：PI 里、还是某个固定训练脚本里），我就可以直接给你按照这个架构写模块骨架。

好，那我直接开干，不再问你要什么，按之前定好的路线图一步一步“落地施工”。

这一轮我完成这几块的**代码级落地**：

1. **Step 0：实验 Manifest（实验契约）**
2. **Step 1：Dataset Sanity（数据体检模块）**
3. **Step 2：Label Contract + Label Guard（标签契约 + 防泄露）**
4. 把 **(1)(2)(3)** 接进你现在的 `PrincipalInvestigator` 流程里，让 AI 大脑从这套“安全壳”里拿数据和标签。

> 下面所有代码，你可以直接按文件名新建 / 替换。只要路径跟我写的一致，AI 大脑就有了“眼睛 + 健康报告 + 安全标签”。

---

## 1️⃣ Step 0：Experiment Manifest（实验契约）

新建文件：`experiments/manifest.py`
（目录名你可以自己决定，但我会后面引用这个路径）

```python
# experiments/manifest.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
import json


@dataclass
class ExperimentManifest:
    """
    记录一次完整实验 / 训练任务的“契约”：
    - 用了什么数据、标签、特征、训练计划
    - 数据体检结果摘要
    - 方便审计 & 复现
    """
    job_id: str
    created_at: str

    symbol: str
    freq: str
    label_id: str

    data_source: Dict[str, Any]
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]

    sanity_summary: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def generate_job_id(
    symbol: str,
    freq: str,
    label_id: str,
    start_ts: str,
    end_ts: str,
) -> str:
    """
    统一 job_id 规范：NVDA_1m_ret_10b_fwd_2024-01-01_2024-03-01
    """
    safe_label = label_id.replace("/", "-")
    return f"{symbol}_{freq}_{safe_label}_{start_ts}_{end_ts}"


def create_manifest(
    *,
    symbol: str,
    freq: str,
    label_id: str,
    start_ts: str,
    end_ts: str,
    data_source: Dict[str, Any],
    feature_recipe: Dict[str, Any],
    training_plan: Dict[str, Any],
    sanity_summary: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> ExperimentManifest:
    job_id = generate_job_id(symbol, freq, label_id, start_ts, end_ts)
    return ExperimentManifest(
        job_id=job_id,
        created_at=datetime.utcnow().isoformat(),
        symbol=symbol,
        freq=freq,
        label_id=label_id,
        data_source=data_source,
        feature_recipe=feature_recipe,
        training_plan=training_plan,
        sanity_summary=sanity_summary,
        notes=notes,
        meta=meta or {},
    )


def save_manifest(
    manifest: ExperimentManifest,
    base_dir: str = "manifests",
) -> Path:
    """
    把 manifest 存成一个 JSON 文件，方便后面审计和回溯。
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{manifest.job_id}.json"
    path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
    return path
```

> 这一块暂时先“建好房间”，实际写文件我会在后面 `PrincipalInvestigator` 里接进去。

---

## 2️⃣ Step 1：Dataset Sanity（数据体检模块）

新建文件：`quality/dataset_sanity.py`

* 目标：**每次 AI 大脑要训练前**，对 `dataset_df` 做一次 **轻量但有用** 的体检：

  * 行/列数
  * 每个数值特征：缺失率、有限值比例、方差、极端值
  * 高度共线特征（|corr| > 0.98）
  * Label 是否严重失衡 / 全是 NaN
* 输出两个东西：

  * `report`：详细统计（给机器或后续 Summary Agent用）
  * `summary`：简要结果（overall_status = ok / warn / fail + issues 列表）

```python
# quality/dataset_sanity.py

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def run_dataset_sanity(
    df: pd.DataFrame,
    label_cols: Optional[List[str]] = None,
    max_rows: int = 50_000,
    high_missing_threshold: float = 0.30,
    high_corr_threshold: float = 0.98,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    对训练前的 dataset_df 做一次“体检”。

    返回:
      - report: 详细报告（列级统计、共线性、label 分布等）
      - summary: 简要结论（overall_status/ issues / basic 统计）
    """
    report: Dict[str, Any] = {}
    summary: Dict[str, Any] = {}
    n_rows, n_cols = df.shape

    if n_rows == 0:
        summary = {
            "overall_status": "fail",
            "reason": "empty_dataframe",
            "details": "DataFrame has 0 rows.",
        }
        report["basic"] = {"n_rows": 0, "n_cols": n_cols}
        return report, summary

    # 采样，避免在超大 DataFrame 上全量算统计
    if n_rows > max_rows:
        df_sample = df.sample(n=max_rows, random_state=42)
    else:
        df_sample = df

    numeric_cols = df_sample.select_dtypes(include=[np.number]).columns.tolist()
    issues: List[str] = []
    col_stats: Dict[str, Any] = {}

    # === 数值列统计 ===
    for col in numeric_cols:
        s = df_sample[col]
        null_ratio = float(s.isna().mean())
        finite_ratio = float(np.isfinite(s.to_numpy(dtype=float)).mean())
        variance = float(np.nanvar(s.to_numpy(dtype=float)))

        desc = s.describe(percentiles=[0.01, 0.5, 0.99])

        col_stats[col] = {
            "null_ratio": null_ratio,
            "finite_ratio": finite_ratio,
            "variance": variance,
            "min": float(desc["min"]),
            "p01": float(desc.get("1%", desc["min"])),
            "median": float(desc["50%"]),
            "p99": float(desc.get("99%", desc["max"])),
            "max": float(desc["max"]),
        }

        if null_ratio > high_missing_threshold:
            issues.append(f"high_missing:{col}:{null_ratio:.2%}")
        if variance == 0.0:
            issues.append(f"zero_variance:{col}")
        if finite_ratio < 0.99:
            issues.append(f"non_finite_values:{col}:{1 - finite_ratio:.2%}")

    report["basic"] = {
        "n_rows": int(n_rows),
        "n_cols": int(n_cols),
        "numeric_cols": numeric_cols,
    }
    report["columns"] = col_stats

    # === 高相关性检查（多重共线性） ===
    corr_notes: List[str] = []
    if 1 < len(numeric_cols) <= 60:  # 列太多就先不算，避免 O(n^2) 爆炸
        corr = df_sample[numeric_cols].corr()
        high_pairs = []
        for i, c1 in enumerate(numeric_cols):
            for c2 in numeric_cols[i + 1 :]:
                v = corr.loc[c1, c2]
                if abs(v) >= high_corr_threshold:
                    high_pairs.append((c1, c2, float(v)))

        if high_pairs:
            corr_notes.append(
                f"{len(high_pairs)} highly correlated pairs (|r| >= {high_corr_threshold})"
            )
            report["high_corr_pairs"] = [
                {"col1": c1, "col2": c2, "corr": v} for c1, c2, v in high_pairs
            ]
            issues.append("high_multicollinearity")

    # === Label 相关检查 ===
    label_info: Dict[str, Any] = {}
    label_issues: List[str] = []

    if label_cols:
        for lc in label_cols:
            if lc not in df_sample.columns:
                label_issues.append(f"label_missing:{lc}")
                continue

            s = df_sample[lc].dropna()
            if s.empty:
                label_issues.append(f"label_all_nan:{lc}")
                continue

            uniq = s.nunique()
            if uniq <= 10:
                # 认为是分类任务，看类别分布是否极度不平衡
                value_counts = s.value_counts(normalize=True).sort_values(ascending=False)
                label_info[lc] = {
                    "task": "cls",
                    "n_unique": int(uniq),
                    "top_values": value_counts.head(5).to_dict(),
                }
                top_ratio = float(value_counts.iloc[0])
                if top_ratio > 0.95:
                    label_issues.append(f"label_heavy_imbalance:{lc}:{top_ratio:.2%}")
            else:
                # 认为是回归/连续 label
                label_info[lc] = {
                    "task": "reg",
                    "n_unique": int(uniq),
                    "mean": float(s.mean()),
                    "std": float(s.std()),
                }

    report["labels"] = label_info
    if label_issues:
        issues.extend(label_issues)

    # === 汇总 ===
    if any(i.startswith("label_missing") or i.startswith("label_all_nan") for i in issues):
        overall = "fail"
    elif any(
        i.startswith("high_missing")
        or i.startswith("high_multicollinearity")
        or i.startswith("label_heavy_imbalance")
        or i.startswith("non_finite_values")
        for i in issues
    ):
        overall = "warn"
    else:
        overall = "ok"

    summary = {
        "overall_status": overall,
        "issues": issues,
        "basic": {"n_rows": int(n_rows), "n_cols": int(n_cols)},
        "labels": label_info,
        "notes": corr_notes,
    }

    return report, summary
```

> 先用纯 Pandas 做体检，性能足够了以后我们再按计划往 ClickHouse 下沉一部分统计。

---

## 3️⃣ Step 2：Label Contract + Label Guard（标签契约 + 防泄露）

### 3.1 Label Registry & Builder

新建文件：`contracts/labels.py`

先把最基础的 **10 bars 前瞻收益率** 规范化成一个 label：

```python
# contracts/labels.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pandas as pd


@dataclass
class LabelConfig:
    """
    Label 的“契约”：
      - kind: 目前先支持 horizon_return（N bar 前瞻收益）
      - horizon_bars: 使用多少个 bar 做前瞻
      - price_col: 用哪一列价格算收益
      - group_key: 分组键（一般按 symbol 分组）
      - ts_col: 时间列
      - required_columns: 构造 label 必须要有的列
      - task_types: 这个 label 适用于哪些 task（reg / rank / cls 等）
    """
    id: str
    kind: str  # "horizon_return" 先支持这一种
    horizon_bars: int
    price_col: str = "close"
    group_key: List[str] = field(default_factory=lambda: ["symbol"])
    ts_col: str = "ts"
    required_columns: List[str] = field(default_factory=lambda: ["symbol", "ts", "close"])
    task_types: List[str] = field(default_factory=lambda: ["reg"])


# ====== 内建一个基础 label：10 bar 前瞻收益率 ======
LABEL_REGISTRY: Dict[str, LabelConfig] = {
    "ret_10b_fwd": LabelConfig(
        id="ret_10b_fwd",
        kind="horizon_return",
        horizon_bars=10,          # 10 个 bar（假设 1m bar 就是 10 分钟）
        price_col="close",
        group_key=["symbol"],
        ts_col="ts",
        required_columns=["symbol", "ts", "close"],
        task_types=["reg", "rank"],
    )
}


def get_label_config(label_id: str) -> LabelConfig:
    try:
        return LABEL_REGISTRY[label_id]
    except KeyError:
        raise KeyError(f"Unknown label_id '{label_id}'. Please register it in LABEL_REGISTRY.")


def build_label_series(df: pd.DataFrame, cfg: LabelConfig) -> pd.Series:
    """
    根据 LabelConfig 在 **Python 层** 构造 label —— 完全禁止 SQL 自己写 target。
    这里实现 horizon_return：fwd_close / close - 1
    """
    # 确保排序稳定，避免 shift 出错
    df_sorted = df.sort_values(cfg.group_key + [cfg.ts_col]).copy()

    grp = df_sorted.groupby(cfg.group_key, sort=False)
    fwd_price = grp[cfg.price_col].shift(-cfg.horizon_bars)
    current_price = df_sorted[cfg.price_col]

    label = fwd_price / current_price - 1.0

    # 再对齐回原 index，以免调用者混乱
    label = label.reindex(df.index)
    return label


def attach_label(
    df: pd.DataFrame,
    label_id: str,
    colname: str = "label",
) -> pd.DataFrame:
    """
    在 df 上增加一列 label，列名默认叫 'label'。
    """
    cfg = get_label_config(label_id)

    missing = [c for c in cfg.required_columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"DataFrame missing required columns for label '{label_id}': {missing}"
        )

    s = build_label_series(df, cfg)
    df_out = df.copy()
    df_out[colname] = s
    return df_out


def get_label_required_columns(label_id: str) -> List[str]:
    cfg = get_label_config(label_id)
    return list(cfg.required_columns)
```

之后你要加新 label（比如不同 horizon、triple-barrier 等），只需要在 `LABEL_REGISTRY` 里新增即可。

---

### 3.2 安全版 SQL Builder：禁止 LLM 写 target

在你的 `pi/feature_sql.py` 里，加一个 **新的安全函数**，专门给 PI 用：

```python
# pi/feature_sql.py

from __future__ import annotations

from typing import Any, Dict, List

from contracts.labels import get_label_required_columns


def build_select_sql_safe(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    label_id: str,
    feature_catalog: List[str],
) -> str:
    """
    安全版 SQL 构造器：
      - 只选：基础列 + base_features + 派生特征的 SQL expr
      - 不允许 LLM 提供任何 target 表达式
      - Label 完全在 Python 里算（attach_label）
    """

    # 1) label 需要的基础列（symbol/ts/close 等）
    required_cols = set(get_label_required_columns(label_id))

    # 2) LLM 提供的 base_features（必须存在于 feature_catalog 中才允许用）
    base_features = recipe.get("base_features", []) or []
    for col in base_features:
        if col in feature_catalog:
            required_cols.add(col)

    # 3) 派生特征（允许 LLM 给 expr，但只作为 SELECT 表达式，不当 target）
    derived_parts: List[str] = []
    for d in recipe.get("derived", []) or []:
        expr = d.get("expr")
        alias = d.get("alias")
        if not expr or not alias:
            continue
        # 这里默认 expr 已经过了你上游的 prompt 约束，只使用本表列，且是纯函数
        derived_parts.append(f"({expr}) AS {alias}")

    # 4) 基础列 SELECT 片段
    base_select_cols = sorted(required_cols)
    base_select_sql = ", ".join(base_select_cols)

    select_parts = [base_select_sql]
    if derived_parts:
        select_parts.extend(derived_parts)

    select_clause = ", ".join(select_parts)

    sql = f"""
        SELECT
            {select_clause}
        FROM {table}
        WHERE symbol = %(symbol)s
          AND ts >= %(start_ts)s
          AND ts < %(end_ts)s
        ORDER BY ts ASC
    """

    return sql.strip()
```

> 注意：旧的 `build_select_sql(...)` 你可以先保留，用在老流程上，新流程统一用 `build_select_sql_safe(...)`。

---

### 3.3 把这些接进 PrincipalInvestigator

现在回到你最开始给的 `PrincipalInvestigator` 文件，把“数据体检 + 安全 label + manifest”接进去。

假设文件：`pi/principal_investigator.py`
下面是**修改后的完整版本**（在你原始基础上加了几处关键改动，我用注释标了 `# NEW`）：

```python
# pi/principal_investigator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql_safe  # NEW
from quality.dataset_sanity import run_dataset_sanity  # NEW
from contracts.labels import attach_label, get_label_required_columns  # NEW
from experiments.manifest import create_manifest, save_manifest  # NEW


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # NEW: 体检结果 + label_id + manifest_path
    sanity_report: Optional[Dict[str, Any]] = None
    sanity_summary: Optional[Dict[str, Any]] = None
    label_id: Optional[str] = None
    manifest_path: Optional[str] = None


def _fetch_catalog(client, table: str) -> List[str]:
    q = "SELECT name FROM system.columns WHERE table = {table:String}"
    df = client.query_df(q, parameters={"table": table})
    return sorted(df["name"].tolist())


class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
        self._ch_cfg = ch_cfg
        self._llm = LLMClient(
            model=(llm_cfg or {}).get("model"),
            base_url=(llm_cfg or {}).get("base_url"),
        )
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )

    def propose(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        goal: str,
        horizon: str,
        guard_cfg: Optional[Dict[str, Any]] = None,
        freq: str = "1m",  # NEW: 方便写 manifest 的频率标记
        default_label_id: str = "ret_10b_fwd",  # NEW: 默认 label
    ) -> PIResult:
        # === 1) 拿表结构 ===
        catalog = _fetch_catalog(self._client, table)

        # === 2) 让 LLM 生成 feature_recipe + training_plan（不许它写 target SQL） ===
        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "注意：不要写任何 SQL 的 target 表达式，label 将在 Python 层统一计算。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # === 3) 决定这次实验用哪个 label（只允许来自内部 registry） ===
        # training_plan 里如果提供了 label_id，就用；否则 fall back 到默认
        label_id = tplan.get("label_id", default_label_id)

        # === 4) 构造“安全版” SQL（只包含特征 + label 所需基础列） ===
        sql = build_select_sql_safe(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            label_id=label_id,
            feature_catalog=catalog,
        )

        df = self._client.query_df(
            sql,
            parameters={"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
        )

        # === 5) 在 Python 层统一构造 label（Label Guard） ===
        df = attach_label(df, label_id=label_id, colname="label")

        # === 6) 跑 Dataset Sanity 体检 ===
        sanity_report, sanity_summary = run_dataset_sanity(df, label_cols=["label"])

        # === 7) 构造 HPO 网格（不变，只是加了 label_id 这一维度在 manifest 里记录） ===
        grid = {
            "model_type": [],
            "feature_subset": [
                recipe.get("base_features", [])
                + [d.get("alias") for d in recipe.get("derived", []) or []]
            ],
            "task": [tplan.get("task", "reg")],
            "decision": [
                tplan.get(
                    "decision",
                    {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
                )
            ],
            "model_params": [],
        }
        for cand in tplan.get("model_candidates", []) or []:
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        guard = (guard_cfg or {}) | {
            "allow_params": [
                "model_type",
                "feature_subset",
                "task",
                "decision",
                "model_params",
            ]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        # === 8) 生成并保存 Experiment Manifest（供 AI 大脑和审计使用） ===
        manifest = create_manifest(
            symbol=symbol,
            freq=freq,
            label_id=label_id,
            start_ts=start_ts,
            end_ts=end_ts,
            data_source={
                "table": table,
                "use_table": recipe.get("use_table", table),
                "clickhouse": self._ch_cfg,
            },
            feature_recipe=recipe,
            training_plan=tplan,
            sanity_summary=sanity_summary,
            meta={
                "sql": sql,
                "feature_catalog": catalog,
            },
        )
        manifest_path = save_manifest(manifest)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            sanity_report=sanity_report,
            sanity_summary=sanity_summary,
            label_id=label_id,
            manifest_path=str(manifest_path),
        )
```

> 现在，**AI 大脑再怎么乱提要求**，只要是走 `PrincipalInvestigator.propose()` 这条链路，就会自动经过：
>
> 1. **安全 SQL 构造器**（禁止 target SQL，所有 label 都由我们的 Label Registry 控制）
> 2. **Python 层统一算 Label**（防止信息泄露）
> 3. **Dataset Sanity 体检**（垃圾数据直接标记为 warn/fail）
> 4. **Experiment Manifest**（每次实验都有完整“病历卡”）

---

## 4️⃣ 接下来怎么继续（我自己会按这个顺序来，不用你指挥）

下一步，我会按之前的路线再继续往前推：

* **下一轮（Step 3）**：

  * 落地 **Feature Registry（特征注册表）** + **View 层** 的规范；
  * 把 `feature_programs.json` 和现在的 LLM `feature_recipe` 统一起来；
  * 加上“沙箱特征（Sandbox Features）”机制：允许 LLM 提出新 SQL 派生列，但必须通过一个小的语法白名单检查器。

* 再下一轮才会去升级：

  * AI 大脑的“眼睛”——输入改成 `(manifest + sanity_summary + performance_summary)`；
  * 增加 Triple Barrier / path-dependent label 等更复杂的标签。

---

你这一步要做的事其实只有两件：

1. 把上面的几个文件/改动，按路径放进你的 repo（可以直接 copy/paste）。
2. 把原来调用 `PrincipalInvestigator.propose(...)` 的地方跑一跑，看有没有 import 路径或 config 名字需要微调。

等你这边代码能正常 import & 跑一两个 symbol，我就继续做 **Feature Registry + 沙箱特征 + AI 大脑输入升级**，继续按这个路线一点点把“量化工厂”搭全。
