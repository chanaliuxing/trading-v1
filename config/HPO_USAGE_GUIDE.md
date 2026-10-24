# HPO 2.0: 贝叶斯优化框架使用指南

## 1. 概述

欢迎使用升级版 HPO (Hyperparameter Optimization) 框架。该框架基于 **Optuna** 构建, 旨在通过先进的搜索算法 (如贝叶斯优化) 高效地为您的交易策略寻找最优参数。

**核心特性:**
- **多种搜索算法**: 支持 **贝叶斯优化 (TPE, BoTorch)**, 随机搜索, 以及传统的网格搜索。
- **持久化与并行**: 使用 RDB 存储 (如 SQLite, PostgreSQL) 来保存研究进度, 并支持多进程并行 (`--n-jobs`)。
- **剪枝 (Pruning)**: 通过 `Median` 或 `ASHA` 剪枝器提前终止不佳的试验, 大幅提升优化效率。
- **灵活的评估逻辑**: 通过 `--runner` 参数可以指定任意函数作为评估器, 实现框架与业务逻辑的完全解耦。
- **交叉验证**: 内置 Walk-Forward (滚动窗口) 验证逻辑, 确保参数的鲁棒性。

---

## 2. 配置文件说明 (`search_space`)

新框架通过配置文件中的 `grid` 字段来定义参数的搜索空间。它非常灵活, 可以同时支持**范围搜索 (用于贝叶斯优化)** 和 **列表搜索 (用于网格搜索)**。

### 范围定义 (用于贝叶斯/随机搜索)
通过一个字典来定义参数的类型和范围。

- **`type`**: `float`, `int`
- **`low`**: 最小值
- **`high`**: 最大值
- **`log`**: (可选, bool) 是否使用对数尺度, 适用于数量级跨度大的参数。
- **`step`**: (可选, float) 步长。

**示例 (`config/hpo_tsla_v7_2.json`):**
```json
"grid": {
  "A1_thr_sigma": { "type": "float", "low": 0.006, "high": 0.012 },
  "A2_max_layers": { "type": "int", "low": 3, "high": 5 },
  "k_atr": { "type": "float", "low": 1.6, "high": 2.4, "step": 0.1 }
}
```

### 列表定义 (用于分类参数或网格搜索)
如果值为一个列表, Optuna 会将其视为一个分类分布 (`CategoricalDistribution`)。

**示例:**
```json
"grid": {
  "my_switch": { "type": "categorical", "choices": [true, false] },
  "ema_period": [10, 20, 50]
}
```
> **注意**: 当所有参数都定义为列表时, 配合 `GridSampler`, 即可实现传统的网格搜索。

---

## 3. 使用步骤: `hpo_cli_upgraded.py`

新的 HPO 功能通过 `hpo_cli_upgraded.py` 脚本启动。

### 命令行选项说明

```
python hpo_cli_upgraded.py [选项]

必需参数:
  --config PATH          HPO 配置文件路径。
  --study-name NAME      研究的名称, 用于持久化存储。
  --runner RUNNER        评估函数路径, 格式为 "module:function"。
                         例如: "wf_hpo:evaluate_trial"

核心可选参数:
  --n-trials INT         优化的总试验次数 (默认: 80)。
  --n-jobs INT           并行执行的作业数 (默认: 1)。
  --backend NAME         采样器后端: 'optuna-tpe' (默认), 'optuna-botorch'。
  --pruner NAME          剪枝器: 'median' (默认), 'asha', 'none'。
  --storage URL          Optuna 存储 URL, 用于持久化和并行。
                         例如: 'sqlite:///hpo_study.db'

其他可选参数:
  --aggregate HOW        多 Folds 结果的聚合方式: 'mean' (默认) 或 'median'。
  --seed INT             随机种子 (默认: 2025)。
  --log-ch URL           (可选) ClickHouse HTTP URL 用于日志记录。
```

### 运行示例

#### 示例 1: 基础贝叶斯优化 (TPE)

这是一个标准的、高效的优化命令。
```bash
python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2.json \
  --study-name "tsla_v7_tpe_study" \
  --runner "wf_hpo:evaluate_trial" \
  --n-trials 200 \
  --n-jobs 4 \
  --storage "sqlite:///hpo_studies.db"
```
- **作用**: 对 `hpo_tsla_v7_2.json` 配置进行 200 次试验, 使用 4 个并行进程, 采用 TPE 采样器, 并将研究进度保存在 `hpo_studies.db` 文件中。

#### 示例 2: 使用高斯过程 (BoTorch) 和 ASHA 剪枝

```bash
python hpo_cli_upgraded.py \
  --config config/hpo_reve_intraday.json \
  --study-name "reve_botorch_asha_study" \
  --runner "wf_hpo:evaluate_trial" \
  --n-trials 150 \
  --n-jobs 4 \
  --storage "sqlite:///hpo_studies.db" \
  --backend "optuna-botorch" \
  --pruner "asha"
```
- **作用**: 使用更先进的 BoTorch (高斯过程) 采样器和 ASHA 剪枝器进行优化。

---

## 4. 结果解读

优化完成后, 脚本会:
1.  在控制台打印**最佳试验**的编号、最终得分和参数。
2.  在当前目录下创建一个名为 **`<study-name>_best_params.json`** 的文件, 其中包含了最优的参数组合。

**示例输出文件 (`tsla_v7_tpe_study_best_params.json`):**
```json
{
  "A1_thr_sigma": 0.0085,
  "A2_max_layers": 4,
  "k_atr": 2.1
}
```

您可以直接将这些参数用于您的策略配置中。

---

## 5. 进阶用法

### 持久化与恢复
只要指定了相同的 `--study-name` 和 `--storage`, 多次运行 `hpo_cli_upgraded.py` 会在同一个 "研究" 上继续进行, 而不是从头开始。这对于长时间、分阶段的优化非常有用。

### 并行优化
在单台机器上, 设置 `--n-jobs` 大于 1 即可利用多核进行并行优化。对于分布式优化 (多台机器), 只需将 `--storage` 指向一个共享的数据库 (如 PostgreSQL), 并在多台机器上用相同的 `--study-name` 启动脚本即可。

### 自定义评估函数
您可以编写自己的评估函数, 只需确保其函数签名与 `wf_hpo.evaluate_trial` 一致:
`my_eval_func(config: dict, params: dict, fold: dict | None) -> dict`
然后通过 `--runner "my_module:my_eval_func"` 来调用它。

---

**最后更新:** 2025-10-23
**版本:** 2.0