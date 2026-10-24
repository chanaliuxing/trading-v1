好，继续往前推一大块：
这一轮我们重点把 **特征空间 & 实验记录** 两个“长期痛点”补上。

> 目标一句话：
> 1）**把特征空间收紧成“有元数据、有边界”的安全子空间**，
> 2）**让每一次训练都有完整“病历卡”（数据质量+标签+特征+超参）可追踪**。

我会照旧给你**可以直接落地的代码骨架**，不需要你做选择题。

---

## 一、Feature Registry v2：特征有“身份证”和“使用说明书”

### 1.1 JSON 结构（升级版）

新建 / 升级一个文件，比如：`config/feature_registry.json`（比现在的 feature_programs 简洁、但更有元数据）。

```jsonc
{
  "_comment": "Feature Registry v2 – 控制 AI 大脑可用的特征空间",
  "_version": "2.0",

  "columns": {
    "close": {
      "table": "bars_1m",
      "dtype": "float",
      "role": "price",
      "granularity": "1m",
      "source": "raw",
      "depends_on": ["close"],
      "allowed_tasks": ["regression", "classification"],
      "allowed_labels": ["*"],          // 对所有 label_id 都合法
      "horizon_min": 1,
      "horizon_max": 120,               // 允许 1~120 bars 的 horizon 使用
      "notes": "主收盘价，已做复权/合并。"
    },

    "ret_1m": {
      "table": "bars_1m",
      "dtype": "float",
      "role": "return",
      "granularity": "1m",
      "source": "derived",
      "depends_on": ["close"],
      "formula": "log(close / lag(close, 1))",
      "allowed_tasks": ["regression", "classification"],
      "allowed_labels": ["*"],
      "horizon_min": 1,
      "horizon_max": 60,
      "notes": "1分钟对数收益率。"
    },

    "vwap_5m": {
      "table": "bars_1m",
      "dtype": "float",
      "role": "price",
      "granularity": "1m",
      "source": "derived",
      "depends_on": ["close", "volume"],
      "formula": "vwap(close, volume, window=5)",
      "allowed_tasks": ["regression", "classification"],
      "allowed_labels": ["*"],
      "horizon_min": 5,
      "horizon_max": 120,
      "notes": "5分钟滚动 VWAP。"
    }
  },

  "tags": {
    // 用于给 AI 大脑筛选 feature subset，例如 “microstructure”, “trend” 等
    "microstructure": ["ret_1m", "spread", "depth_imbalance"],
    "trend_short": ["ret_1m", "ma_5", "ma_10"],
    "trend_context": ["ma_20", "ma_60", "vwap_5m"]
  }
}
```

> 核心点：每个特征都有：
>
> * 表 / 粒度（granularity）
> * 来源（raw / derived）
> * 依赖列（防前视）
> * 允许用在哪些任务 / 哪些 label / 哪些 horizon

---

### 1.2 Python 侧：`FeatureSpec` + Registry Loader

新建模块：`features/registry.py`

```python
# features/registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import json


@dataclass
class FeatureSpec:
    name: str
    table: str
    dtype: str
    role: str
    granularity: str
    source: str                  # "raw" / "derived" / "sandbox"
    depends_on: List[str]
    formula: Optional[str]
    allowed_tasks: List[str]     # ["regression", "classification"]
    allowed_labels: List[str]    # ["*"] or label_id list
    horizon_min: Optional[int]
    horizon_max: Optional[int]
    notes: str = ""

    def supports_label(self, label_id: str) -> bool:
        return "*" in self.allowed_labels or label_id in self.allowed_labels

    def supports_horizon(self, horizon_bars: int) -> bool:
        if horizon_bars is None:
            return True
        if self.horizon_min is not None and horizon_bars < self.horizon_min:
            return False
        if self.horizon_max is not None and horizon_bars > self.horizon_max:
            return False
        return True

    def supports_task(self, task: str) -> bool:
        if not self.allowed_tasks:
            return True
        return task in self.allowed_tasks


class FeatureRegistry:
    """
    控制 AI & PI 可用的特征空间。
    所有特征必须在这里登记，才能被 PI / HPO 使用。
    """

    def __init__(self, spec: Dict[str, Any], *, path: Optional[Path] = None) -> None:
        self._raw_spec = spec
        self._path = path
        self._features: Dict[str, FeatureSpec] = {}
        self._tags: Dict[str, List[str]] = {}

        self._load()

    # ---------- Loader ----------

    @classmethod
    def from_file(cls, path: Path) -> "FeatureRegistry":
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return cls(raw, path=path)

    def _load(self) -> None:
        cols = self._raw_spec.get("columns", {})
        for name, cfg in cols.items():
            fs = FeatureSpec(
                name=name,
                table=cfg["table"],
                dtype=cfg.get("dtype", "float"),
                role=cfg.get("role", "generic"),
                granularity=cfg.get("granularity", "1m"),
                source=cfg.get("source", "raw"),
                depends_on=cfg.get("depends_on", []),
                formula=cfg.get("formula"),
                allowed_tasks=cfg.get("allowed_tasks", []),
                allowed_labels=cfg.get("allowed_labels", ["*"]),
                horizon_min=cfg.get("horizon_min"),
                horizon_max=cfg.get("horizon_max"),
                notes=cfg.get("notes", ""),
            )
            self._features[name] = fs

        self._tags = self._raw_spec.get("tags", {})

    # ---------- 查询 API ----------

    def has_feature(self, name: str) -> bool:
        return name in self._features

    def get(self, name: str) -> FeatureSpec:
        return self._features[name]

    def list_all(self) -> List[str]:
        return sorted(self._features.keys())

    def list_by_tag(self, tag: str) -> List[str]:
        return list(self._tags.get(tag, []))

    def filter_for_task_and_label(
        self,
        *,
        candidate_features: List[str],
        task: str,
        label_id: str,
        horizon_bars: Optional[int],
    ) -> Tuple[List[str], List[str]]:
        """
        返回 (allowed, rejected) – 根据特征自身的元数据做过滤。
        """
        allowed: List[str] = []
        rejected: List[str] = []

        for f in candidate_features:
            if f not in self._features:
                rejected.append(f"{f} (not in registry)")
                continue
            spec = self._features[f]
            if not spec.supports_task(task):
                rejected.append(f"{f} (not allowed for task={task})")
                continue
            if not spec.supports_label(label_id):
                rejected.append(f"{f} (not allowed for label={label_id})")
                continue
            if not spec.supports_horizon(horizon_bars or 0):
                rejected.append(f"{f} (not allowed for horizon={horizon_bars})")
                continue
            allowed.append(f)

        return allowed, rejected
```

> 这一步的意义：
>
> * 不再是“AI 想起啥特征就用啥”，而是**先过 registry 这一关**
> * 你也能逐步把现在零散的 `feature_programs` 收拢进这个 registry

---

### 1.3 把 FeatureRegistry 接到 `PrincipalInvestigator`

在 `pi/core.py` 里，构造 PI 时引入 Registry（可以从 config 加，也可以硬编码路径）：

```python
# pi/core.py 片段

from features.registry import FeatureRegistry
from labels.engine import LabelDef  # 前面我们已经定义过

class PrincipalInvestigator:
    def __init__(self, ch_cfg, llm_cfg=None, *, feature_registry_path: Optional[str] = None):
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

        # 🔴 新增：特征注册表
        if feature_registry_path:
            self._feature_registry = FeatureRegistry.from_file(Path(feature_registry_path))
        else:
            # 也可以从默认路径加载，按你项目目录调整
            default_path = Path("config/feature_registry.json")
            self._feature_registry = FeatureRegistry.from_file(default_path)
```

然后在 `propose()` 里，原来我们有 `recipe_out["base_features"]` 这一部分；
现在增加一段 **基于 registry + label 的过滤**：

```python
        # 假设 label_attach.label_def 已经拿到
        label_def: LabelDef = label_attach.label_def
        task = tplan.get("task", "regression")
        label_id = label_def.id
        horizon_bars = label_def.horizon_bars

        # 计算原始候选特征：base + derived
        base_feats = recipe.get("base_features", [])
        derived_feats = [d.get("alias") for d in recipe.get("derived", [])]
        candidate_feats = list(dict.fromkeys(base_feats + derived_feats))  # 去重保序

        allowed_feats, rejected_feats = self._feature_registry.filter_for_task_and_label(
            candidate_features=candidate_feats,
            task=task,
            label_id=label_id,
            horizon_bars=horizon_bars,
        )

        # 把过滤结果写回 recipe_out
        recipe_out["base_features"] = [f for f in base_feats if f in allowed_feats]
        recipe_out["derived"] = [d for d in recipe.get("derived", []) if d.get("alias") in allowed_feats]
        recipe_out["rejected_by_registry"] = rejected_feats
```

> 效果：
>
> * 即使 LLM 在 recipe 里瞎写了一个 `magic_alpha_999`，也会被 registry 拦掉
> * 对于 horizon / label 不合适的特征，也会被自动剔除
> * 你可以在日志里看到 `rejected_by_registry`，方便后续“提拔/黑名单”

---

## 二、Model Run “病历卡”：每次实验都能复盘

现在我们已经有：

* DatasetSanityResult
* LabelAttachResult + LabelDef
* DatasetQualitySummary
* FeatureRegistry + 被剔除的特征
* HPO grid / 最终最佳模型 config

下一步是：**把这些拼在一起写成一张“模型训练病历卡”**，入库或者写 JSONL。

### 2.1 dataclass：`ModelRunContext`

新建：`runs/context.py`

```python
# runs/context.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from dataset_sanity.core import DatasetSanityResult
from brain.dataset_summary import DatasetQualitySummary
from labels.engine import LabelDef
from pi.core import PIResult  # 注意避免循环引用，必要时可以解耦


@dataclass
class ModelRunContext:
    run_id: str

    # 数据集信息
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    freq: str              # "1s"/"1m"/"5m" 等
    n_rows: int
    n_features: int

    # 标签信息
    label_def: LabelDef

    # 数据质量
    sanity: DatasetSanityResult
    dataset_quality: DatasetQualitySummary

    # 特征&训练计划
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    hpo_grid: Dict[str, Any]
    sandbox_rejected: List[Dict[str, Any]]
    registry_rejected: List[str]

    # HPO / 训练结果
    best_model_type: Optional[str] = None
    best_params: Optional[Dict[str, Any]] = None
    best_score: Optional[float] = None
    metric_name: Optional[str] = None
    backtest_summary: Optional[Dict[str, Any]] = None

    # 元信息
    created_at: str = datetime.utcnow().isoformat()
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        # 一些 dataclass 对象需要转换：
        out["label_def"] = self.label_def.to_dict()
        out["sanity"] = self.sanity.to_dict()
        out["dataset_quality"] = self.dataset_quality.to_dict()
        return out

    @classmethod
    def from_pi_result(
        cls,
        run_id: str,
        pi_result: PIResult,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        freq: str,
    ) -> "ModelRunContext":
        df = pi_result.dataset_df
        n_rows, n_cols = df.shape

        label_def = pi_result.sanity_result.label_def  # 如果你在 sanity_result 里放了 label_def
        # 如果没有，就从 label_attach_result 存在的地方拿

        registry_rejected = pi_result.feature_recipe.get("rejected_by_registry", [])

        return cls(
            run_id=run_id,
            table=table,
            symbol=symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            freq=freq,
            n_rows=int(n_rows),
            n_features=int(n_cols),
            label_def=label_def,
            sanity=pi_result.sanity_result,
            dataset_quality=pi_result.dataset_quality_summary,
            feature_recipe=pi_result.feature_recipe,
            training_plan=pi_result.training_plan,
            hpo_grid=pi_result.hpo_grid,
            sandbox_rejected=pi_result.sandbox_rejected,
            registry_rejected=registry_rejected,
        )
```

> 注意：上面用到 `label_def` 和 `sanity_result` 的存放位置，要跟你前面代码保持一致；
> 如果你是把 `label_def` 放在 `LabelAttachResult` 里，就可以换成 `pi_result.label_attach.label_def`。

---

### 2.2 HPO 结束后补全“病历卡”再落地

在 HPO 结束的地方（比如 `hpo/runner.py` 或 `wf_hpo.py`）：

```python
# hpo/runner.py 伪代码

from runs.context import ModelRunContext
import uuid
import json
from pathlib import Path

def run_full_experiment(cfg):
    # 1) PI → 数据 + 计划
    pi = PrincipalInvestigator(
        cfg["clickhouse"],
        cfg.get("llm"),
        feature_registry_path=cfg.get("feature_registry_path"),
    )
    pi_result = pi.propose(
        table=cfg["table"],
        symbol=cfg["symbol"],
        start_ts=cfg["start_ts"],
        end_ts=cfg["end_ts"],
        goal=cfg["goal"],
        horizon=cfg["horizon"],
        guard_cfg=cfg.get("grid_guard"),
        sanity_cfg=cfg.get("sanity"),
        default_label_id=cfg.get("default_label_id", "ret_10b_fwd_reg"),
    )

    run_id = cfg.get("run_id") or str(uuid.uuid4())

    # 2) 初始化 “病历卡”
    ctx = ModelRunContext.from_pi_result(
        run_id=run_id,
        pi_result=pi_result,
        table=cfg["table"],
        symbol=cfg["symbol"],
        start_ts=cfg["start_ts"],
        end_ts=cfg["end_ts"],
        freq=cfg.get("freq", "1m"),
    )

    # 3) 跑 HPO / 训练
    best_model, best_score, metric_name, best_params, backtest_summary = run_hpo(
        df=pi_result.dataset_df,
        hpo_grid=pi_result.hpo_grid,
        training_plan=pi_result.training_plan,
    )

    # 4) 补全 “病历卡”
    ctx.best_model_type = getattr(best_model, "model_type", None)
    ctx.best_params = best_params
    ctx.best_score = best_score
    ctx.metric_name = metric_name
    ctx.backtest_summary = backtest_summary

    # 5) 落地 – 先简单写 JSONL，后续你可以改成 ClickHouse / Fabric 表
    out_dir = Path(cfg.get("runs_out_dir", "runs_out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "model_runs.jsonl"

    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ctx.to_dict(), ensure_ascii=False))
        f.write("\n")

    return best_model, ctx
```

> 从此以后，每一个试验都有一条 JSONL 记录，里面包括：
>
> * 数据：时间段、symbol、行数、特征数
> * 数据质量：score、severity、缺失、高相关
> * 标签：label_id、method、horizon、分布
> * 特征：用哪些、被 registry/sandbox 拒绝哪些
> * 模型：model_type, metric, backtest summary

以后你再回头看“这条模型为什么好/坏”的时候，就不会只看到一串神秘的 `Sharpe=3.1`，而是能看到它当时是建立在什么数据&标签基础上的。

---

## 总结一下这一轮你现在已经拥有的东西

1. ✅ **Feature Registry v2**

   * JSON 中给每个特征补“身份证 + 使用说明书”（来源表、粒度、允许任务、允许标签、horizon 范围）
   * Python `FeatureRegistry` 提供过滤 API，自动剔除不合法特征

2. ✅ **PI + Registry 集成**

   * LLM 提的 `base_features` / `derived` 会先走 Registry Gate
   * 不在 registry 或不适合当前 label/horizon 的特征会被自动剔除、记录在 `rejected_by_registry`

3. ✅ **Model Run 病历卡**

   * `ModelRunContext` 把数据体检、标签定义、特征方案、HPO 结果全部打包
   * 每次实验会写一行 JSONL（或未来写到 ClickHouse/Fabric 表），方便审计和复盘

---

### 下一步我会做什么？

继续按我们的大路线走，下一轮主要会做两件事（你不用回复“要不要”，我会直接往前推）：

1. **让 AI 大脑在“选 prompt / 选策略 / 选执行风格”时，也能看到这些 run history**

   * 比如：当它下次要做 `v3_fast_market_following` 训练时，可以检索过去相同 prompt 下的 run，看到：

     * 哪些数据质量条件下效果好
     * 哪种 label/horizon 组合比较稳
   * 这会变成一个“策略经验库”。

2. **把你现有的 prompt library（那一大坨 tactical prompts）和 Feature/Label/Run Registry 打通**

   * prompt meta 里可以记录：适用的 label_id / horizon / symbol bucket / regime
   * 大脑就不再是“凭感觉选 prompt”，而是“基于历史 run 和 meta 选最靠谱的一套”。

你再说一句「继续」，我就直接开始把 “prompt library ↔ model run ↔ feature/label registry” 这一层 glue 写出来，让 AI 大脑真正拥有“长期记忆”和“经验回放”。
好，继续把“眼睛 + 经验 + prompt 库”这一层真正连起来。
这一轮我做 3 件事：

1. 给你的 **prompt 库一个正式的 PromptRegistry**（专门读你这份 JSON）
2. 在 **ModelRun 病历卡里挂上 execution_prompt_id**，让每次实验都能追溯是哪一个 prompt 风格
3. 做一个 **RunHistoryIndex + DatasetQualityNarrator**：

   * 前者用“数据”评价每个 prompt（比如这个 prompt 最近平均 Sharpe / 回撤）
   * 后者把复杂的体检报告变成给 LLM 看的自然语言摘要，解决“信息过载”问题

我会像前几轮一样，直接给可以落地的代码骨架，你之后只需要把文件放进对应目录、按项目结构稍微改 import 即可。

---

## 一、PromptRegistry：把你那坨 JSON 变成“可检索的 prompt 仓库”

你的 prompt JSON 大概长这样（简化一下）：

```jsonc
{
  "_comment": "Prompt Library - Whitelist ...",
  "_usage": "...",
  "_version": "1.0",
  "v3_stable": "....",
  "v3_fast_market_following": "....",
  "v4_experimental": "....",
  "prompts": {
    "v3_fast_market_following_tactical_20251102": "....",
    "...": "...."
  },
  "meta": {
    "v3_fast_market_following_tactical_20251102": {
      "created_at": "...",
      "source": "auto_evolver",
      "base_id": "v3_fast_market_following",
      "reviewer": "",
      "signature": "",
      "version": 1
    },
    "...": {}
  }
}
```

### 1.1 Python 侧：`prompt/registry.py`

```python
# prompt/registry.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json


@dataclass
class PromptMeta:
    prompt_id: str
    base_id: Optional[str]
    created_at: Optional[datetime]
    source: str
    reviewer: str
    signature: str
    version: int

    @classmethod
    def from_raw(cls, prompt_id: str, raw: Dict[str, Any]) -> "PromptMeta":
        created_at_raw = raw.get("created_at")
        created_at = None
        if created_at_raw:
            try:
                created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except Exception:
                created_at = None

        return cls(
            prompt_id=prompt_id,
            base_id=raw.get("base_id"),
            created_at=created_at,
            source=raw.get("source", ""),
            reviewer=raw.get("reviewer", ""),
            signature=raw.get("signature", ""),
            version=int(raw.get("version", 1)),
        )


@dataclass
class PromptEntry:
    prompt_id: str
    text: str
    meta: Optional[PromptMeta] = None

    @property
    def base_id(self) -> Optional[str]:
        return self.meta.base_id if self.meta else None

    @property
    def created_at(self) -> Optional[datetime]:
        return self.meta.created_at if self.meta else None


class PromptRegistry:
    """
    统一管理 execution_prompt_id 白名单。

    - 顶层 key: "v3_stable" / "v3_fast_market_following" / "v4_experimental" 等
    - prompts.*: auto_evolver 生成的 tactical 变体
    """

    def __init__(self, spec: Dict[str, Any], *, path: Optional[Path] = None) -> None:
        self._spec = spec
        self._path = path

        self._entries: Dict[str, PromptEntry] = {}
        self._base_index: Dict[str, List[str]] = {}  # base_id -> [prompt_id]
        self._load()

    @classmethod
    def from_file(cls, path: Path) -> "PromptRegistry":
        with path.open("r", encoding="utf-8") as f:
            spec = json.load(f)
        return cls(spec, path=path)

    # ---------- load ----------

    def _load(self) -> None:
        meta_raw: Dict[str, Any] = self._spec.get("meta", {})
        prompts_group: Dict[str, str] = self._spec.get("prompts", {})

        # 1) 顶层 prompt（非 "prompts"/"_meta" 的 key）
        reserved = {"_comment", "_usage", "_version", "prompts", "meta"}
        for key, value in self._spec.items():
            if key in reserved:
                continue
            if not isinstance(value, str):
                continue
            m = meta_raw.get(key)
            meta = PromptMeta.from_raw(key, m) if m else None
            self._register_entry(prompt_id=key, text=value, meta=meta)

        # 2) prompts.* 子节点
        for pid, text in prompts_group.items():
            m = meta_raw.get(pid)
            meta = PromptMeta.from_raw(pid, m) if m else None
            self._register_entry(prompt_id=pid, text=text, meta=meta)

    def _register_entry(self, prompt_id: str, text: str, meta: Optional[PromptMeta]) -> None:
        entry = PromptEntry(prompt_id=prompt_id, text=text, meta=meta)
        self._entries[prompt_id] = entry

        base_id = meta.base_id if meta else None
        if base_id:
            self._base_index.setdefault(base_id, []).append(prompt_id)

    # ---------- 查询 API ----------

    def exists(self, prompt_id: str) -> bool:
        return prompt_id in self._entries

    def get(self, prompt_id: str) -> PromptEntry:
        return self._entries[prompt_id]

    def get_text(self, prompt_id: str) -> str:
        return self._entries[prompt_id].text

    def get_meta(self, prompt_id: str) -> Optional[PromptMeta]:
        entry = self._entries.get(prompt_id)
        return entry.meta if entry else None

    def list_all_ids(self) -> List[str]:
        return sorted(self._entries.keys())

    def list_by_base(self, base_id: str) -> List[str]:
        return sorted(self._base_index.get(base_id, []))

    def latest_by_base(self, base_id: str) -> Optional[PromptEntry]:
        """
        返回给定 base_id 下，按 created_at 降序的最新 prompt 版本。
        """
        candidates = [self._entries[pid] for pid in self._base_index.get(base_id, [])]
        if not candidates:
            return None

        def sort_key(e: PromptEntry):
            # created_at 为空时放得更早
            return (e.created_at or datetime.min, e.prompt_id)

        candidates.sort(key=sort_key, reverse=True)
        return candidates[0]
```

> 这样 AI 大脑在内部永远只能用 **prompt_id**，而不是随便拼 prompt 文本；
> 你也可以通过 **`base_id` 找最新一版 tactical 变体**，比如
> `latest_by_base("v3_fast_market_following")`。

---

## 二、在“病历卡”里挂上 execution_prompt_id

之前我们做的 `ModelRunContext` 还没有跟 prompt 直接挂钩。
现在加上两个字段：

* `execution_prompt_id`：本次训练/策略决策实际使用的 prompt
* `base_prompt_id`：其父 `base_id`（例如 `"v3_fast_market_following"`）

### 2.1 扩展 ModelRunContext

```python
# runs/context.py 中补充字段

@dataclass
class ModelRunContext:
    run_id: str

    # 新增：prompt 信息
    execution_prompt_id: Optional[str] = None
    base_prompt_id: Optional[str] = None

    # --- 以下是已有字段，略 ---
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    freq: str
    n_rows: int
    n_features: int

    label_def: LabelDef
    sanity: DatasetSanityResult
    dataset_quality: DatasetQualitySummary

    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    hpo_grid: Dict[str, Any]
    sandbox_rejected: List[Dict[str, Any]]
    registry_rejected: List[str]

    best_model_type: Optional[str] = None
    best_params: Optional[Dict[str, Any]] = None
    best_score: Optional[float] = None
    metric_name: Optional[str] = None
    backtest_summary: Optional[Dict[str, Any]] = None

    created_at: str = datetime.utcnow().isoformat()
    notes: str = ""
```

在 `from_pi_result()` 里多接一个 `execution_prompt_id` 参数：

```python
    @classmethod
    def from_pi_result(
        cls,
        run_id: str,
        pi_result: PIResult,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        freq: str,
        execution_prompt_id: Optional[str] = None,
        base_prompt_id: Optional[str] = None,
    ) -> "ModelRunContext":
        df = pi_result.dataset_df
        n_rows, n_cols = df.shape

        label_def = pi_result.sanity_result.label_def
        registry_rejected = pi_result.feature_recipe.get("rejected_by_registry", [])

        return cls(
            run_id=run_id,
            execution_prompt_id=execution_prompt_id,
            base_prompt_id=base_prompt_id,
            table=table,
            symbol=symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            freq=freq,
            n_rows=int(n_rows),
            n_features=int(n_cols),
            label_def=label_def,
            sanity=pi_result.sanity_result,
            dataset_quality=pi_result.dataset_quality_summary,
            feature_recipe=pi_result.feature_recipe,
            training_plan=pi_result.training_plan,
            hpo_grid=pi_result.hpo_grid,
            sandbox_rejected=pi_result.sandbox_rejected,
            registry_rejected=registry_rejected,
        )
```

### 2.2 在 HPO 启动时写入 prompt 信息

假设 orchestrator 那层已经决定：

* 这次训练使用 `execution_prompt_id = "v3_fast_market_following_tactical_20251117_v2"`
* 其 base 是 `"v3_fast_market_following"`

在 `run_full_experiment(cfg)` 中：

```python
from prompt.registry import PromptRegistry

def run_full_experiment(cfg):
    prompt_registry = PromptRegistry.from_file(Path(cfg["prompt_library_path"]))
    execution_prompt_id = cfg["execution_prompt_id"]  # orchestrator 传进来的
    if not prompt_registry.exists(execution_prompt_id):
        raise ValueError(f"Unknown execution_prompt_id={execution_prompt_id}")

    meta = prompt_registry.get_meta(execution_prompt_id)
    base_prompt_id = meta.base_id if meta else None

    # 1) PI → 生成数据和计划
    pi = PrincipalInvestigator(
        cfg["clickhouse"],
        cfg.get("llm"),
        feature_registry_path=cfg.get("feature_registry_path"),
    )

    pi_result = pi.propose(
        table=cfg["table"],
        symbol=cfg["symbol"],
        start_ts=cfg["start_ts"],
        end_ts=cfg["end_ts"],
        goal=cfg["goal"],
        horizon=cfg["horizon"],
        guard_cfg=cfg.get("grid_guard"),
        sanity_cfg=cfg.get("sanity"),
        default_label_id=cfg.get("default_label_id", "ret_10b_fwd_reg"),
    )

    run_id = cfg.get("run_id") or str(uuid.uuid4())

    ctx = ModelRunContext.from_pi_result(
        run_id=run_id,
        pi_result=pi_result,
        table=cfg["table"],
        symbol=cfg["symbol"],
        start_ts=cfg["start_ts"],
        end_ts=cfg["end_ts"],
        freq=cfg.get("freq", "1m"),
        execution_prompt_id=execution_prompt_id,
        base_prompt_id=base_prompt_id,
    )

    # ... 后面一样：HPO → 写 JSONL ...
```

> 这样，**每一条模型 run 记录里都明确写着：是哪一个 prompt 控制下训练出来的结果。**
> 后面我们做“经验回放”时，就能按 prompt 维度做统计。

---

## 三、RunHistoryIndex + DatasetQualityNarrator：给 AI 大脑看的“经验 +总结”

### 3.1 RunHistoryIndex：按 prompt/label/symbol 查历史表现

目标：让 AI 大脑不再凭感觉，而是说：

> “v3_fast_market_following_tactical 这一系列 prompt，
> 在 TSLA / label=ret_10b_fwd_reg / 最近 30 天的训练里，平均 Sharpe = 2.1，回撤 7%，
> 而 conservative prompt 平均 Sharpe 1.0，回撤 3%。
> 在当前市场 regime 下我更愿意选择哪个 prompt？”

先做一个简单的历史索引器：加载 `model_runs.jsonl`，按条件筛选，然后算一些统计指标。

```python
# runs/history_index.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import json
from statistics import mean


@dataclass
class RunRecord:
    run_id: str
    created_at: datetime
    symbol: str
    execution_prompt_id: Optional[str]
    base_prompt_id: Optional[str]
    label_id: str
    best_score: Optional[float]
    metric_name: Optional[str]
    backtest_summary: Dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "RunRecord":
        created_at_raw = raw.get("created_at")
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))

        label_def = raw.get("label_def", {})
        label_id = label_def.get("id", "")

        return cls(
            run_id=raw["run_id"],
            created_at=created_at,
            symbol=raw["symbol"],
            execution_prompt_id=raw.get("execution_prompt_id"),
            base_prompt_id=raw.get("base_prompt_id"),
            label_id=label_id,
            best_score=raw.get("best_score"),
            metric_name=raw.get("metric_name"),
            backtest_summary=raw.get("backtest_summary") or {},
        )


@dataclass
class PromptPerformanceSummary:
    prompt_id: str
    base_prompt_id: Optional[str]
    n_runs: int
    metric_name: Optional[str]
    metric_mean: Optional[float]
    metric_median: Optional[float]
    metric_min: Optional[float]
    metric_max: Optional[float]


class RunHistoryIndex:
    """
    按 prompt / symbol / label 检索历史 run，并做简单统计，
    为 prompt 选择 & 策略配置提供数据支撑。
    """

    def __init__(self, runs: List[RunRecord]) -> None:
        self._runs = runs

    @classmethod
    def from_jsonl(cls, path: Path) -> "RunHistoryIndex":
        runs: List[RunRecord] = []
        if not path.exists():
            return cls(runs)

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                try:
                    runs.append(RunRecord.from_raw(raw))
                except Exception:
                    # TODO: logging
                    continue
        return cls(runs)

    # --------- 过滤工具 ---------

    def filter(
        self,
        *,
        symbol: Optional[str] = None,
        label_id: Optional[str] = None,
        execution_prompt_id: Optional[str] = None,
        base_prompt_id: Optional[str] = None,
        since_days: Optional[int] = None,
    ) -> List[RunRecord]:
        now = datetime.utcnow()
        since_time = now - timedelta(days=since_days) if since_days else None

        result: List[RunRecord] = []
        for r in self._runs:
            if symbol and r.symbol != symbol:
                continue
            if label_id and r.label_id != label_id:
                continue
            if execution_prompt_id and r.execution_prompt_id != execution_prompt_id:
                continue
            if base_prompt_id and r.base_prompt_id != base_prompt_id:
                continue
            if since_time and r.created_at < since_time:
                continue
            result.append(r)
        return result

    # --------- 指标统计 ---------

    @staticmethod
    def _summarize_metric(runs: List[RunRecord]) -> Dict[str, Any]:
        scores = [r.best_score for r in runs if r.best_score is not None]
        if not scores:
            return {
                "metric_mean": None,
                "metric_median": None,
                "metric_min": None,
                "metric_max": None,
            }

        scores_sorted = sorted(scores)
        n = len(scores_sorted)
        if n % 2 == 1:
            median = scores_sorted[n // 2]
        else:
            median = 0.5 * (scores_sorted[n // 2 - 1] + scores_sorted[n // 2])

        return {
            "metric_mean": float(mean(scores_sorted)),
            "metric_median": float(median),
            "metric_min": float(scores_sorted[0]),
            "metric_max": float(scores_sorted[-1]),
        }

    def summarize_by_prompt(
        self,
        *,
        symbol: str,
        label_id: str,
        base_prompt_id: Optional[str] = None,
        since_days: int = 30,
    ) -> List[PromptPerformanceSummary]:
        """
        对某个 symbol + label，在最近 since_days 天内，
        统计每个 execution_prompt_id 的表现。
        """
        runs = self.filter(symbol=symbol, label_id=label_id, since_days=since_days)
        if base_prompt_id:
            runs = [r for r in runs if r.base_prompt_id == base_prompt_id]

        by_prompt: Dict[str, List[RunRecord]] = {}
        for r in runs:
            if not r.execution_prompt_id:
                continue
            by_prompt.setdefault(r.execution_prompt_id, []).append(r)

        summaries: List[PromptPerformanceSummary] = []
        for pid, rs in by_prompt.items():
            if not rs:
                continue
            metric_name = rs[0].metric_name
            stats = self._summarize_metric(rs)
            base_id = rs[0].base_prompt_id

            summaries.append(
                PromptPerformanceSummary(
                    prompt_id=pid,
                    base_prompt_id=base_id,
                    n_runs=len(rs),
                    metric_name=metric_name,
                    metric_mean=stats["metric_mean"],
                    metric_median=stats["metric_median"],
                    metric_min=stats["metric_min"],
                    metric_max=stats["metric_max"],
                )
            )

        # 按 metric_mean 降序排序（比如 Sharpe 越高越好）
        summaries.sort(key=lambda s: (s.metric_mean or float("-inf"), s.n_runs), reverse=True)
        return summaries
```

> 现在，**prompt 选择**就可以变成一个数据驱动逻辑，例如：

```python
history = RunHistoryIndex.from_jsonl(Path("runs_out/model_runs.jsonl"))

summaries = history.summarize_by_prompt(
    symbol="TSLA",
    label_id="ret_10b_fwd_reg",
    base_prompt_id="v3_fast_market_following",
    since_days=30,
)

best_prompt = summaries[0].prompt_id if summaries else "v3_fast_market_following"
```

这样做完全符合你之前的要求：

> **“所有参数和策略选择都必须数据驱动，而不是靠感觉。”**

---

### 3.2 DatasetQualityNarrator：把体检结果翻译成 LLM 能消化的摘要

解决 Gemini 提到的“信息过载”问题：不要给 LLM 一大坨数值表，而是给一个简短的“体检总结”。

```python
# brain/narrator.py

from __future__ import annotations

from typing import List

from dataset_sanity.core import DatasetSanityResult
from brain.dataset_summary import DatasetQualitySummary


def narrate_dataset_quality(
    *,
    sanity: DatasetSanityResult,
    quality: DatasetQualitySummary,
    max_items: int = 5,
) -> str:
    """
    把体检结果 → 简明自然语言摘要，给 AI 大脑看。
    """

    lines: List[str] = []

    # 1) 总体评分
    lines.append(
        f"Dataset quality overall score: {quality.score:.3f} (severity={quality.severity.name})."
    )

    # 2) 样本数 & 特征数
    lines.append(
        f"Rows={quality.n_rows}, features={quality.n_features}, "
        f"label='{quality.label_def.name}' (id={quality.label_def.id}, horizon={quality.label_def.horizon_bars} bars)."
    )

    # 3) 主要风险点
    if sanity.has_critical_issues():
        lines.append("Critical issues detected:")
        for issue in sanity.list_critical_issues()[:max_items]:
            lines.append(f"- [CRITICAL] {issue}")
    elif sanity.has_warning_issues():
        lines.append("Warnings detected:")
        for issue in sanity.list_warning_issues()[:max_items]:
            lines.append(f"- [WARN] {issue}")
    else:
        lines.append("No major data quality issues detected.")

    # 4) 缺失值 & 高相关特征
    if quality.missing_report:
        missing_top = sorted(
            quality.missing_report.items(), key=lambda kv: kv[1], reverse=True
        )[:max_items]
        if missing_top:
            lines.append("Top features by missing ratio:")
            for col, ratio in missing_top:
                lines.append(f"- {col}: missing_ratio={ratio:.3f}")

    if quality.correlated_pairs:
        pairs_top = quality.correlated_pairs[:max_items]
        lines.append("Highly correlated feature pairs (|corr|≥threshold):")
        for p in pairs_top:
            lines.append(f"- {p.feature_a} vs {p.feature_b}: corr={p.corr:.3f}")

    return "\n".join(lines)
```

在 orchestrator 里使用：

```python
summary_text = narrate_dataset_quality(
    sanity=pi_result.sanity_result,
    quality=pi_result.dataset_quality_summary,
)

# 然后把 summary_text 放进 LLM 的 system_prompt 或 context 里，而不是塞一大堆表格。
```

---

## 这一轮的成果小结

到目前为止，你已经有：

1. ✅ **PromptRegistry**

   * 统一加载你这份 prompt JSON（顶层 + prompts.* 子树）
   * 支持 `latest_by_base`、`get_text`、`get_meta`

2. ✅ **ModelRunContext + prompt 绑定**

   * 每个 run 都记录 `execution_prompt_id + base_prompt_id`
   * 以后可以按 prompt/label/symbol 做全链路追踪

3. ✅ **RunHistoryIndex**

   * 从 `model_runs.jsonl` 中构建历史索引
   * 按 symbol+label+base_prompt → 统计各个 prompt 变体的平均表现
   * 允许你“数据驱动地选 prompt”

4. ✅ **DatasetQualityNarrator**

   * 把复杂的体检数据 → 几行简洁的自然语言摘要
   * 专门给 LLM 大脑看，避免上下文爆炸和“看不懂重点”

---

### 下一步我会做什么？

**下一轮我会直接做两件事：**

1. 写一个 **Brain-side Prompt Selector**：

   * 输入：symbol, label_id, 任务类型（如“fast_market_following”）、历史窗口（30d 等）
   * 内部：用 RunHistoryIndex + PromptRegistry → 选出一个 `execution_prompt_id`
   * 输出：一个结构化的 `BrainExecutionPlan`，里面包含：

     * 使用哪个 prompt
     * 允许的 label / feature / model_type 范围
     * 是否允许 sandbox 特征

2. 写一个 **AI 大脑 orchestrator 调用链示意**：

   * `BrainExecutionPlan` → 调 `run_full_experiment`，自动把 prompt_id 写入 run
   * 完成一次端到端：
     “AI 大脑决定训练 + prompt 选择 → 数据体检 → Label 安全 → 特征 Registry → HPO → 结果入库。”

你再说一次「继续」，我就把这两个 glue 层的代码骨架写出来，让 “AI 大脑控制机器学习” 真正闭环起来。
