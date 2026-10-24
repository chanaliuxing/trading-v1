好，我们继续把“眼睛 + 工具”这一层真正落地，而不是只停在概念上。

这一轮我会做三件事：

1. **把数据体检和标签统计接进 PIResult**（以后所有下游都能直接用）
2. 定义一个标准的 **BrainContext**（AI 大脑看到的“世界快照”）
3. 写一个小的 **BrainTools / Context Builder**，输出直接可以喂给 LLM 的 payload（避免信息过载）

我不会问你“要不要做”，直接一步一步给出可以拷贝进项目的代码。

---

## 一、扩展 PIResult：把体检报告和标签统计挂上去

> 目标：PI 一次调用之后，下游就能拿到 **数据 / 标签 / 体检报告 / HPO 网格** 一整套东西，
> 不需要再自己东拼西凑。

### 1. 更新 `PIResult` dataclass

把我们上次加过的 label 字段 + 这次的 dataset_sanity 字段融合成一个最终版本（可以放在 `pi/principal_investigator.py` 顶部）：

```python
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # ---- 新增：标签 + 体检相关 ----
    label_id: Optional[str] = None
    label_spec: Optional[Dict[str, Any]] = None
    label_stats: Optional[Dict[str, Any]] = None
    dataset_sanity: Optional[Dict[str, Any]] = None
```

> 兼容性说明：
> 以前的代码只用前 5 个字段，照样能构造；多出来的 4 个是有默认值的可选字段，不会炸。

### 2. 在 PI.propose 里实际计算 `dataset_sanity`

假设你之前已经有 `data_quality/dataset_sanity.py` 这一层，我们现在只需要在 PI 的 `propose` 里插入调用。示例（只展示关键片段）：

```python
# pi/principal_investigator.py

from data_quality.dataset_sanity import run_all_checks  # 或你之前定义的名字
from labels.registry import LabelRegistry
from labels.engine import attach_label_and_stats

class PrincipalInvestigator:
    ...

    def propose(...):
        ...
        # 4) 执行 SQL 拉数据
        df = self._client.query_df(sql)

        # 5) 标签计算（前一轮已经写过）
        label_stats_dict: Dict[str, Any] = {}
        if label_spec is not None and not df.empty:
            df, label_stats_dict = attach_label_and_stats(df, label_spec)
            tplan["target"] = {
                "mode": "precomputed",
                "col": label_spec.alias,
                "alias": label_spec.alias,
                "target_type": label_spec.target_type,
                "label_id": label_spec.id,
            }

        # 6) 数据体检（这一步是给 AI 大脑 + 人看的“眼睛”）
        dataset_sanity: Dict[str, Any] = {}
        if not df.empty:
            # 建议在 run_all_checks 内部做：
            # - 采样 (例如 n_rows > 2e5 时 sample 10%)
            # - 数值列/分类列分开处理
            # - 不要对上百特征做全量相关矩阵（可以限制 top K）
            dataset_sanity = run_all_checks(df)

        # 7) HPO grid（保持原逻辑）
        ...

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            label_id=chosen_label_id,
            label_spec=label_spec.to_dict() if label_spec else None,
            label_stats=label_stats_dict or None,
            dataset_sanity=dataset_sanity or None,
        )
```

> 到这里为止：
>
> * **任何一次 PI 调用**，都产出一个结构化的 “bundle（捆绑包）”：
>
>   * 数据集 `df`
>   * 特征配方 `feature_recipe`
>   * 训练计划 `training_plan`
>   * HPO 网格 `hpo_grid`
>   * 标签定义 + 统计 `label_spec + label_stats`
>   * 体检报告 `dataset_sanity`
> * 这就是我们接下来构造 AI Brain “上下文”的原材料。

---

## 二、BrainContext：把原材料组织成 AI 能看懂的“世界快照”

> 目标：
>
> * 不把整个 DataFrame 塞给 LLM（上下文会爆）
> * 而是给它一个 **结构化、压缩好的上下文**：
>
>   * 这批数据是什么（表/标的/时间）
>   * 有哪些特征、哪些被用于训练
>   * 标签是啥、分布是否稀疏/极端
>   * 数据质量有没有明显红灯（缺失、共线、异常值等）

### 1. 新文件：`brain/context.py`

```python
# brain/context.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass
class BrainContext:
    """
    提供给 AI 大脑的“世界快照”.

    注意：这里只存“摘要”和“小样本”，不要存整个 DataFrame。
    """

    # 1) 基本元信息
    meta: Dict[str, Any]

    # 2) 特征 / 标签 / 训练计划
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    hpo_grid: Dict[str, Any]

    # 3) 数据集结构与样本
    schema: List[Dict[str, Any]]
    sample_rows: List[Dict[str, Any]]

    # 4) 数据体检摘要
    dataset_sanity_summary: Dict[str, Any]

    # 5) 标签定义 + 统计
    label_spec: Optional[Dict[str, Any]] = None
    label_stats: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        """
        转成可以直接喂给 LLM（user_prompt 里的 JSON）的结构.
        """
        return asdict(self)
```

---

## 三、BrainTools / Context Builder：从 PIResult → BrainContext

> 目标：
>
> * 把 `PIResult` + 一点运行时信息（symbol、table、session id…）
>   **自动转**成 `BrainContext`
> * 同时解决“信息过载”问题：
>
>   * 对 dataset_sanity 做二次摘要，只保留真正需要大脑看到的内容
>   * 对 df 只抽几行 sample + schema，而不是整表

### 1. 一个 dataset_sanity 的摘要器（解决 Gemini 指出的“信息过载”）

我们假设 `run_all_checks(df)` 返回一个结构类似：

```python
{
  "columns": {
    "close": {
      "dtype": "float64",
      "missing_ratio": 0.0,
      "zero_var": false,
      ...
    },
    ...
  },
  "warnings": [
    {"level": "error", "code": "MISSING_TARGET", "detail": "..."},
    {"level": "warn", "code": "HIGH_CORR", "detail": "close vs vwap ~ 0.99"},
  ],
  "corr": {
    "high_corr_pairs": [
      ["close", "vwap", 0.99],
      ...
    ]
  }
}
```

我们在 `data_quality/dataset_sanity.py` 里加一个专门给 LLM 用的摘要函数：

```python
# data_quality/dataset_sanity.py

from __future__ import annotations
from typing import Any, Dict, List


def summarize_for_llm(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    把体检报告压缩成适合 LLM 的摘要，避免信息过载.

    主要输出：
    - 总体严重程度
    - 关键 warning 列表（最多 N 条）
    - 缺失率高的列
    - 零方差 / 近零方差的列
    - 高相关性特征对（Top K）
    """
    warnings: List[Dict[str, Any]] = report.get("warnings", [])

    # 这里的 keys 要和你实际的 run_all_checks 实现对齐
    columns_info: Dict[str, Any] = report.get("columns", {})
    high_missing = []
    zero_var = []
    for col, info in columns_info.items():
        if info.get("missing_ratio", 0.0) > 0.3:
            high_missing.append(col)
        if info.get("zero_variance", False) or info.get("near_zero_variance", False):
            zero_var.append(col)

    corr_info = report.get("corr", {})
    corr_pairs = []
    for pair in corr_info.get("high_corr_pairs", [])[:10]:
        # pair: [col1, col2, corr]
        corr_pairs.append(
            {"col1": pair[0], "col2": pair[1], "corr": float(pair[2])}
        )

    # 估一个总体严重程度
    severity = "ok"
    if any(w.get("level") == "error" for w in warnings):
        severity = "error"
    elif any(w.get("level") == "warn" for w in warnings):
        severity = "warn"

    summary: Dict[str, Any] = {
        "severity": severity,
        "warning_count": len(warnings),
        "warnings_top": warnings[:10],
        "high_missing_cols": high_missing,
        "zero_var_cols": zero_var,
        "high_corr_pairs_top": corr_pairs,
    }
    return summary
```

这样，大脑拿到的是：

* `"severity": "ok" / "warn" / "error"`
* 最多 10 条 warning
* 哪些列缺失很高、哪些列几乎没波动
* 哪些特征高度共线

而不是整张矩阵 / 巨量数值。

### 2. Context Builder：从 PIResult 构造 BrainContext

新文件：`brain/context_builder.py`

```python
# brain/context_builder.py

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from .context import BrainContext
from pi.principal_investigator import PIResult
from data_quality.dataset_sanity import summarize_for_llm


def build_brain_context(
    *,
    pi_result: PIResult,
    symbol: str,
    table: str,
    goal: str,
    horizon: str,
    run_id: str,
    session_meta: Dict[str, Any] | None = None,
    sample_rows: int = 10,
) -> BrainContext:
    """
    把 PIResult + 运行时元信息，统一打包成 BrainContext.

    参数说明：
    - symbol/table/goal/horizon: 调用 PI 时的高层意图
    - run_id: 本次实验/训练的唯一 ID（便于日志和追踪）
    - session_meta: 其它你想加的元信息，比如:
        - provider / model
        - who triggered (user vs scheduler)
        - paper/live, etc.
    """
    df = pi_result.dataset_df

    # ---- 1) meta ----
    meta: Dict[str, Any] = {
        "run_id": run_id,
        "symbol": symbol,
        "table": table,
        "goal": goal,
        "horizon": horizon,
    }
    if session_meta:
        meta.update(session_meta)

    # ---- 2) schema ----
    schema = []
    for col, dtype in df.dtypes.items():
        schema.append(
            {
                "name": col,
                "dtype": str(dtype),
            }
        )

    # ---- 3) sample_rows ----
    if sample_rows > 0:
        sample_df = df.head(sample_rows)
        sample_rows_json = sample_df.to_dict(orient="records")
    else:
        sample_rows_json = []

    # ---- 4) dataset_sanity_summary ----
    sanity_report = pi_result.dataset_sanity or {}
    sanity_summary = summarize_for_llm(sanity_report) if sanity_report else {}

    # ---- 5) 构造 BrainContext ----
    ctx = BrainContext(
        meta=meta,
        feature_recipe=pi_result.feature_recipe,
        training_plan=pi_result.training_plan,
        hpo_grid=pi_result.hpo_grid,
        schema=schema,
        sample_rows=sample_rows_json,
        dataset_sanity_summary=sanity_summary,
        label_spec=pi_result.label_spec,
        label_stats=pi_result.label_stats,
    )
    return ctx


def brain_context_to_llm_payload(ctx: BrainContext) -> Dict[str, Any]:
    """
    简单封装一层，便于统一管理对 LLM 的 JSON 结构.
    """
    return ctx.to_payload()
```

---

## 四、AI 大脑如何使用 BrainContext（示例）

> 现在“大脑”的调用方式可以标准化成一个模式：
>
> * `PI.propose(...)` → 得到 `pi_result`
> * `build_brain_context(...)` → 得到 `brain_ctx`
> * 组装一个 prompt，把 `brain_ctx.to_payload()` 作为 JSON 塞进 user 消息
> * 让大脑决定：
>
>   * 数据是否可接受（based on severity/warnings）
>   * 要不要调整 feature_subset / model_type / 决策阈值
>   * 要不要直接拒绝训练 / 标记为“只能做研究、不能上实盘”

例如（伪代码，放在你的 orchestrator 里）：

```python
# brain/orchestrator.py (示意)

import json
from llm.llm_client import LLMClient
from brain.context_builder import build_brain_context, brain_context_to_llm_payload

def run_brain_for_training_decision(
    pi_result,
    *,
    symbol,
    table,
    goal,
    horizon,
    run_id,
    brain_model_cfg,
    execution_prompt: str,
):
    """
    AI 大脑接收一个 BrainContext，然后输出“训练决策”:
      - accept / reject / modify
      - 如果 accept，还可以微调 hpo_grid / training_plan
    """
    ctx = build_brain_context(
        pi_result=pi_result,
        symbol=symbol,
        table=table,
        goal=goal,
        horizon=horizon,
        run_id=run_id,
        session_meta={"phase": "train_planning"},
    )
    payload = brain_context_to_llm_payload(ctx)

    system_prompt = (
        "You are the AI research director for a systematic trading lab.\n"
        "You receive a structured JSON describing a candidate dataset, feature recipe, "
        "label definition, and quality diagnostics.\n"
        "Your job is to decide whether this dataset is safe/meaningful to train on, "
        "and to optionally adjust the training plan and HPO grid.\n"
        "Output ONLY JSON with fields: decision, reasons, adjusted_training_plan, adjusted_hpo_grid."
    )

    user_prompt = (
        "Here is the BrainContext for the proposed experiment:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    llm = LLMClient(
        model=brain_model_cfg.get("model"),
        base_url=brain_model_cfg.get("base_url"),
    )

    result = llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
    # 这里 result 应该已经是 dict（你可以像在 PI 里那样，用 structured output 强约束）
    return result
```

> 这样，“AI 大脑的眼睛和工具”就真正有了**清晰的输入**：
>
> * 它不是在乱看一堆日志，而是收到一个**标准化 JSON**：
>
>   * meta：任务背景
>   * feature_recipe：这轮准备用哪些特征
>   * label_spec/label_stats：标签的定义和分布
>   * dataset_sanity_summary：数据质量告警
>   * hpo_grid：候选模型和参数空间
> * 它的输出也被约束成 JSON（decision + reasons + 调整建议）

---

## 五、到目前为止我们已经完成的“大积木”

1. **DatasetSanity 模块**

   * `run_all_checks(df)`：真正做体检
   * `summarize_for_llm(report)`：解决信息过载

2. **FeatureRegistry + SandboxFeatures**

   * 所有特征可控，LLM 只能在白名单 + 沙箱里选

3. **LabelRegistry + LabelEngine**

   * 所有标签有固定 `label_id`
   * 真正计算都在 Python 层，防止 label 泄露和乱写 target

4. **PIResult 扩展**

   * 把 feature/label/sanity/hpo 打包成一个对象给下游

5. **BrainContext + ContextBuilder**（本轮完成）

   * 把 PIResult → BrainContext → LLM payload 的路径打通
   * 让大脑有“标准视角”看数据，而不是靠你手工复制日志

---

### 下一步可以做什么（我会继续往前推）？

如果你再说一句 **“继续”**，我会：

* 设计一套 **“训练决策 JSON Schema”**（大脑必须输出的结构），
* 给出大脑输出 → 实际执行 HPO / 训练的落地代码骨架：

  * 如何根据大脑返回的 `decision=reject/accept/adjust` 控制训练流程
  * 如何把 `adjusted_training_plan` / `adjusted_hpo_grid` 严格落地到你现有的 HPO 引擎里
* 同时把你贴出来的 **execution prompt library** 利用起来，
  形成一个真正统一的「Prompt Library + BrainContext + Tooling」闭环。

你只要继续回一个字：**继续** 👣 我就接着往下搭“训练决策执行层”。
好，咱们正式把 **“AI 大脑控制机器学习训练”** 这一层打通：
从 PI 产出的数据包 → 大脑审查&调整 → 最终喂给 HPO/训练引擎。

我会一步一步落地，不问你任何“要不要”，只给你可以直接放进项目的代码骨架。

---

## 1. 大脑训练决策 JSON Schema（结构化输出）

> 目的：**约束大脑输出**，避免“自由发挥”的 JSON；
> 大脑只能在这几个选项里做：
>
> * `decision`: 接受 / 拒绝 / 调整
> * `adjusted_training_plan`: 对原 training_plan 的增量调整
> * `adjusted_hpo_grid`: 对原 hpo_grid 的增量调整
> * `reasons / warnings / notes`: 解释原因

新文件：`brain/schemas.py`

```python
# brain/schemas.py

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, validator


DecisionLiteral = Literal["reject", "accept", "adjust"]
SeverityLiteral = Literal["ok", "warn", "error"]


class BrainTrainDecision(BaseModel):
    """
    LLM 必须输出的结构化结果，用来指导这一次 ML 训练是否继续、如何调整。
    """

    # 1) 核心决策：本次实验怎样处理
    decision: DecisionLiteral = Field(
        ...,
        description="训练决策：reject / accept / adjust",
    )

    # 2) 大脑的总体严重程度判断（可结合 dataset_sanity_summary）
    severity: SeverityLiteral = Field(
        "ok",
        description="数据与标签质量的总体判断：ok / warn / error",
    )

    # 3) 解释和说明
    reasons: List[str] = Field(
        default_factory=list,
        description="做出这个决策的主要原因说明（面向人类）",
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="额外风险提示，例如样本不平衡、共线、极端值",
    )
    notes: Optional[str] = Field(
        None,
        description="其它补充说明",
    )

    # 4) 可选：对训练计划和 HPO 网格的增量调整
    adjusted_training_plan: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "对原 training_plan 的 PATCH；例如修改 task、decision 阈值、"
            "某些 feature 开关等。留空则表示不调整。"
        ),
    )

    adjusted_hpo_grid: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "对原 hpo_grid 的增量调整，比如增加/减少模型候选、限制参数范围等。"
            "将与原 hpo_grid 进行 merge + guard。"
        ),
    )

    @validator("reasons", "warnings", pre=True)
    def _ensure_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v
```

> 说明：
>
> * 后续任何地方只要拿到一个 `BrainTrainDecision`，就知道该不该训练、怎么训练。
> * 决策逻辑清晰：
>
>   * `reject`：直接终止训练并记录原因
>   * `accept`：原样训练
>   * `adjust`：在 guard 限制内合并 `adjusted_*` 再训练

---

## 2. LLM 调用桥：统一封装“大脑调用 + JSON 解析”

> 目的：
>
> * 不改动你现有 `LLMClient.generate` 的前提下，把 LLM 输出转换为 `BrainTrainDecision`
> * 自动处理字符串 / dict / 轻微脏 JSON 的情况
> * 所有“大脑训练决策”的调用都走这一条路径，方便将来替换底层模型

新文件：`brain/llm_bridge.py`

```python
# brain/llm_bridge.py

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from llm.llm_client import LLMClient
from .schemas import BrainTrainDecision


def _ensure_dict(raw: Any) -> Dict[str, Any]:
    """
    把 LLM 返回的结果统一转成 dict.

    支持几种情况：
    - 已经是 dict
    - 是 JSON 字符串
    - 前后有废话的 JSON 字符串（尝试用最外层 {...} 截取）
    """
    if isinstance(raw, dict):
        return raw

    if not isinstance(raw, str):
        raise ValueError(f"LLM returned unsupported type: {type(raw)}")

    s = raw.strip()

    # 尝试直接 parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 简单粗暴：找到第一个 '{' 与最后一个 '}'，截取里面再 parse
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        inner = s[start : end + 1]
        return json.loads(inner)

    raise ValueError(f"Cannot parse LLM output as JSON: {raw[:200]}...")


def call_brain_training_decision(
    llm: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
) -> Tuple[BrainTrainDecision, Dict[str, Any]]:
    """
    调用 AI 大脑，得到一个结构化的 BrainTrainDecision.

    返回:
      - parsed: BrainTrainDecision
      - raw: 原始 dict（便于日志存储）
    """
    raw = llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
    raw_dict = _ensure_dict(raw)
    parsed = BrainTrainDecision.parse_obj(raw_dict)
    return parsed, raw_dict
```

> 以后你只需要：
> `decision, raw = call_brain_training_decision(llm, system_prompt=..., user_prompt=...)`
> 就能得到一个类型安全的 `BrainTrainDecision` 对象。

---

## 3. 把 Prompt Library 接进大脑（执行时根据 prompt_id 取文案）

> 目的：
>
> * 不再在代码里写死长 prompt，而是通过 `prompt_id` 从 JSON 里取
> * 兼容你已经有的 prompt 库结构（根层 + "prompts" 子层）

新文件：`brain/prompt_library.py`

```python
# brain/prompt_library.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class PromptLibrary:
    """
    简单的 Prompt Library 读取器。

    支持结构类似：
    {
      "v3_stable": "...",
      "prompts": {
        "v3_fast_market_following_tactical_20251117": "...",
        ...
      },
      "meta": {
        "v3_fast_market_following_tactical_20251117": { ... }
      }
    }
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("r", encoding="utf-8") as f:
            self._data: Dict[str, Any] = json.load(f)

        self._prompts: Dict[str, str] = {}
        # 根层的 prompt（如 v3_stable 等）
        for k, v in self._data.items():
            if k in ("prompts", "meta", "_comment", "_usage", "_version"):
                continue
            if isinstance(v, str):
                self._prompts[k] = v

        # 子层 prompts 里的 prompt
        inner_prompts = self._data.get("prompts", {})
        for k, v in inner_prompts.items():
            if isinstance(v, str):
                self._prompts[k] = v

        self._meta: Dict[str, Any] = self._data.get("meta", {})

    def get_prompt(self, prompt_id: str) -> str:
        try:
            return self._prompts[prompt_id]
        except KeyError as e:
            raise KeyError(
                f"Prompt id '{prompt_id}' not found in library {self.path}"
            ) from e

    def get_meta(self, prompt_id: str) -> Dict[str, Any]:
        return self._meta.get(prompt_id, {})
```

> 用法示例（后面 orchestrator 里会用到）：

```python
lib = PromptLibrary(Path("config/prompt_library.json"))
prompt_text = lib.get_prompt("pi_research_director_v1")
meta = lib.get_meta("pi_research_director_v1")
```

> 你可以在现有 JSON 里新增一个专门给“训练决策”的 prompt，例如：

```jsonc
{
  ...
  "pi_research_director_v1": "You are the AI research director for a systematic trading lab. ...",
  "prompts": {
    ...
  }
}
```

---

## 4. 把一切接起来：AI 大脑 → 训练决策 → HPO 执行

> 目标：
>
> * 写一个 **完整的 orchestrator**：
>
>   1. 调用 `PrincipalInvestigator.propose()` 拿到 `PIResult`
>   2. 用我们之前写好的 `build_brain_context` 构造 `BrainContext`
>   3. 用 PromptLibrary + `call_brain_training_decision` 调大脑
>   4. 根据决策 `accept / reject / adjust` 合并训练计划 & HPO 网格
>   5. 调用 HPO runner 执行训练
>   6. 记录日志 / 决策追踪

### 4.1 应用大脑决策：合并 training_plan 和 hpo_grid

新文件：`brain/decision_applicator.py`

```python
# brain/decision_applicator.py

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Tuple

from grid_guard import sanitize_and_constrain_grid
from .schemas import BrainTrainDecision
from pi.principal_investigator import PIResult


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    简单的深度 merge：同 key 时 patch 覆盖 base。
    只处理 dict / 基础类型，不处理 list 的深度合并（按 patch 覆盖）。
    """
    result = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def apply_brain_decision_to_pi_result(
    pi_result: PIResult,
    decision: BrainTrainDecision,
    *,
    hpo_guard_cfg: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    根据 AI 大脑的决策，生成最终用于训练的
      - training_plan_final
      - hpo_grid_final

    若 decision.decision == "reject"，应由调用方决定如何处理（此函数不 raise）。
    """
    # ---- 1) training_plan ----
    base_tplan = deepcopy(pi_result.training_plan) or {}
    if decision.adjusted_training_plan:
        training_plan_final = _deep_merge(base_tplan, decision.adjusted_training_plan)
    else:
        training_plan_final = base_tplan

    # ---- 2) hpo_grid ----
    base_grid = deepcopy(pi_result.hpo_grid) or {}
    if decision.adjusted_hpo_grid:
        # 简单合并：adjusted 覆盖 base 同名 key
        merged = {**base_grid, **decision.adjusted_hpo_grid}
    else:
        merged = base_grid

    # 再走一遍 grid_guard，确保 AI 没有越界
    guard = (hpo_guard_cfg or {}) | {
        "allow_params": [
            "model_type",
            "feature_subset",
            "task",
            "decision",
            "model_params",
        ]
    }
    hpo_grid_final, _ = sanitize_and_constrain_grid(merged, guard)

    return training_plan_final, hpo_grid_final
```

> 这里用了我们之前的 `sanitize_and_constrain_grid`，保证大脑即使乱填了东西，也会被 guard 掉。

---

### 4.2 真正的 orchestrator：从 PI 到 HPO 一条龙

新文件：`orchestration/run_ai_brain_training.py`（你可以按现有目录结构调整）

```python
# orchestration/run_ai_brain_training.py

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from llm.llm_client import LLMClient
from pi.principal_investigator import PrincipalInvestigator, PIResult
from brain.prompt_library import PromptLibrary
from brain.context_builder import build_brain_context, brain_context_to_llm_payload
from brain.llm_bridge import call_brain_training_decision
from brain.decision_applicator import apply_brain_decision_to_pi_result
from brain.schemas import BrainTrainDecision

# 假设你有一个统一的 HPO 入口
from hpo.runner import run_hpo_experiment  # 需要你按实际项目中的函数名替换


def run_ai_brain_controlled_training(
    *,
    ch_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
    brain_cfg: Dict[str, Any],
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    run_id: str,
    prompt_library_path: Path,
) -> Dict[str, Any]:
    """
    整条链路：
      PI -> BrainContext -> AI 大脑 -> 决策 -> HPO 训练 -> 结果

    返回:
      一个 dict，包括：
        - run_id
        - brain_decision (dict)
        - training_plan_final
        - hpo_grid_final
        - hpo_result (具体结构根据你的 HPO 实现)
    """

    # 1) 初始化组件
    pi = PrincipalInvestigator(ch_cfg=ch_cfg, llm_cfg=llm_cfg)
    prompt_lib = PromptLibrary(prompt_library_path)
    brain_prompt_id = brain_cfg.get(
        "prompt_id", "pi_research_director_v1"
    )  # 你可在 JSON 里新增这个 ID

    brain_llm = LLMClient(
        model=brain_cfg.get("model", llm_cfg.get("model")),
        base_url=brain_cfg.get("base_url", llm_cfg.get("base_url")),
    )

    # 2) 让 PI 提案（含 feature_recipe / training_plan / df / hpo_grid / label / sanity）
    pi_result: PIResult = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        guard_cfg=brain_cfg.get("pi_guard_cfg", {}),
    )

    # 3) 构造 BrainContext
    brain_ctx = build_brain_context(
        pi_result=pi_result,
        symbol=symbol,
        table=table,
        goal=goal,
        horizon=horizon,
        run_id=run_id,
        session_meta={
            "stage": "train_plan",
            "caller": "ai_brain_orchestrator",
        },
    )
    payload = brain_context_to_llm_payload(brain_ctx)

    # 4) 拼接 prompt
    system_prompt = prompt_lib.get_prompt(brain_prompt_id)

    # 可以选择性把 meta 里的信息也塞进 prompt
    user_prompt = (
        "Here is the current BrainContext for a proposed ML experiment.\n"
        "You must decide whether to reject, accept, or adjust the training, "
        "based on data quality, label characteristics, and the proposed HPO grid.\n"
        "Return ONLY JSON that matches the documented schema "
        "(decision, severity, reasons, warnings, notes, adjusted_training_plan, adjusted_hpo_grid).\n\n"
        "BrainContext JSON:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    # 5) 调用 AI 大脑做训练决策
    brain_decision: BrainTrainDecision
    brain_decision, brain_decision_raw = call_brain_training_decision(
        brain_llm,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    # 6) 根据决策决定是否继续 / 如何调整
    if brain_decision.decision == "reject":
        # 你可以在这里直接返回，不跑 HPO
        return {
            "run_id": run_id,
            "status": "rejected_by_brain",
            "brain_decision": brain_decision_raw,
            "message": "Training rejected by AI brain",
        }

    training_plan_final, hpo_grid_final = apply_brain_decision_to_pi_result(
        pi_result,
        brain_decision,
        hpo_guard_cfg=brain_cfg.get("hpo_guard_cfg", {}),
    )

    # 7) 调用 HPO / 训练引擎
    #    这里假设你的 HPO 函数接收 df / hpo_grid / training_plan
    hpo_result = run_hpo_experiment(
        df=pi_result.dataset_df,
        hpo_grid=hpo_grid_final,
        training_plan=training_plan_final,
        meta={
            "run_id": run_id,
            "symbol": symbol,
            "table": table,
            "goal": goal,
            "horizon": horizon,
        },
    )

    # 8) 返回整套结果（方便写入 DB / 日志 / 实验追踪）
    return {
        "run_id": run_id,
        "status": "completed",
        "brain_decision": brain_decision_raw,
        "training_plan_final": training_plan_final,
        "hpo_grid_final": hpo_grid_final,
        "hpo_result": hpo_result,
    }
```

> 你可以在一个 CLI / Notebook 里面这样调用它：

```python
from pathlib import Path
from orchestration.run_ai_brain_training import run_ai_brain_controlled_training

result = run_ai_brain_controlled_training(
    ch_cfg={
        "host": "localhost",
        "port": 8123,
        "database": "trading",
        "user": "default",
        "password": "",
    },
    llm_cfg={
        "model": "gpt-4.1",
        "base_url": "https://api.openai.com/v1",
    },
    brain_cfg={
        "model": "gpt-4.1",
        "base_url": "https://api.openai.com/v1",
        "prompt_id": "pi_research_director_v1",
        "pi_guard_cfg": {},
        "hpo_guard_cfg": {},
    },
    table="bars_1m_features",
    symbol="NVDA",
    start_ts="2025-10-01 13:30:00",
    end_ts="2025-10-31 20:00:00",
    goal="intra-day alpha on 10m horizon",
    horizon="10m",
    run_id="exp_20251117_nvda_10m_v1",
    prompt_library_path=Path("config/prompt_library.json"),
)
```

---

## 5. 现在你已经拥有的“AI 大脑 ML 控制链”一览

**从数据到训练**，已经形成一个闭环：

1. **PI（Principal Investigator）**

   * 产出：`PIResult`
   * 包含：feature_recipe、training_plan、SQL、dataset_df、hpo_grid、label_spec、label_stats、dataset_sanity

2. **DatasetSanity + LabelEngine**（之前我们已经落地了）

   * 确保：数据/标签至少是“合理”的，问题会在 sanity_report 中被标记

3. **BrainContext + ContextBuilder**

   * 将 `PIResult` 压缩成 AI 友好的 `BrainContext`
   * 避免上下文爆炸，只给大脑看精华信息

4. **PromptLibrary**

   * 用 `prompt_id` 控制大脑角色和风格（研究总监 / 风控总监等）

5. **LLM Bridge + BrainTrainDecision Schema**（本轮新增）

   * 统一约束大脑输出格式
   * 所有训练决策都走 `BrainTrainDecision`，可审计、可回放、可对比

6. **DecisionApplicator + GridGuard**

   * 安全地合并大脑建议，防止 prompt 注入导致训练参数乱飞

7. **AI Brain Training Orchestrator**

   * 真正把“要不要训练、怎么训练”的控制权交给 AI 大脑
   * 同时又在代码层面有清晰的 guard 和 fallback

---

如果你再说一句 **“继续”**，我下一步会：

* 专门针对 **Label 的复杂性**（Gemini 提到的 Triple Barrier / path-dependent label），
  设计一套 **LabelRegistry v2**：

  * 支持简单 `ret_Xm_fwd`
  * 也支持 Triple Barrier 这类路径依赖标签
* 把 LabelEngine 升级到可以：

  * 复用你现有 Backtest 引擎生成更“真实”的标签
  * 或者至少用统一的 Python 实现，在 `LabelSpec` 里就声明清楚
* 同时给 AI 大脑增加“标签层视角”：

  * 例如：`label_stats` 中明确“正例比例 0.4%，极度不平衡”
  * 让大脑可以基于标签质量改变决策（例如拒绝训练、要求 resampling）

你只要继续回复：**继续**，我就接着把 Label 层也升级到“机构级可控”的版本。
