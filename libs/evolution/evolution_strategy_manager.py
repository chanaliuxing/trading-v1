#!/usr/bin/env python3
"""
Evolution Strategy Manager: 自演化策略管理器

实现蓝皮书 v3 Research Layer (L3) 的核心组件:
- 决定改哪个模块 (where to evolve)
- 决定怎么改 (how to evolve)
- 资源分配和优先级排序
- 基于历史收益选择最佳策略

Usage:
  from libs.evolution.evolution_strategy_manager import EvolutionStrategyManager

  manager = EvolutionStrategyManager()
  strategy = manager.select_next_target()
  print(f"Next target: {strategy['target']}, priority: {strategy['priority']}")
"""
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import numpy as np
import clickhouse_connect
import structlog

logger = structlog.get_logger()


class EvolutionStrategyManager:
    """
    自演化策略管理器

    负责:
    1. 从 mutable_targets 中选择下一个改进目标
    2. 基于历史收益和风险评估优先级
    3. 平衡 exploration (探索新模块) vs exploitation (优化已知模块)
    4. 考虑预算和资源约束
    """

    def __init__(self, ch_client=None):
        self.ch = ch_client or self._get_ch_client()
        self.logger = logger.bind(component="evolution_strategy_manager")
        self._experiments_table_exists_cache: Optional[bool] = None

        # 策略配置
        self.config = {
            "exploration_rate": 0.2,       # 20% 时间探索新模块
            "max_concurrent_experiments": 3,
            "min_samples_before_exploit": 5,
            "risk_tolerance": 0.5,         # 风险容忍度 (0-1)
        }

    def select_next_target(
        self,
        strategy: str = "ucb",             # 'ucb', 'epsilon_greedy', 'thompson'
        exclude_targets: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        选择下一个演化目标

        Args:
            strategy: 选择策略
                - 'ucb': Upper Confidence Bound (平衡探索和利用)
                - 'epsilon_greedy': ε-贪心算法
                - 'thompson': Thompson Sampling
            exclude_targets: 排除的目标列表 (如正在进行实验的目标)

        Returns:
            选中的目标及其优先级、预期收益等信息
        """
        self.logger.info("select_next_target_start", strategy=strategy)

        # 1. 加载所有可演化的目标
        mutable_targets = self._load_mutable_targets()

        if exclude_targets:
            mutable_targets = [t for t in mutable_targets if t["id"] not in exclude_targets]

        if not mutable_targets:
            return {"ok": False, "error": "No mutable targets available"}

        # 2. 加载历史实验数据
        history = self._load_experiment_history()

        # 3. 根据策略选择目标
        if strategy == "ucb":
            selected = self._select_ucb(mutable_targets, history)
        elif strategy == "epsilon_greedy":
            selected = self._select_epsilon_greedy(mutable_targets, history)
        elif strategy == "thompson":
            selected = self._select_thompson_sampling(mutable_targets, history)
        else:
            selected = self._select_random(mutable_targets)

        # 4. 增强选中目标的信息
        enriched = self._enrich_target(selected, history)

        self.logger.info("select_next_target_complete",
                        target_id=enriched["id"],
                        priority=enriched["priority"],
                        expected_benefit=enriched["expected_benefit"])

        return enriched

    def rank_all_targets(self) -> List[Dict[str, Any]]:
        """
        对所有目标排序，返回优先级列表

        Returns:
            排序后的目标列表 (按优先级从高到低)
        """
        mutable_targets = self._load_mutable_targets()
        history = self._load_experiment_history()

        ranked = []
        for target in mutable_targets:
            enriched = self._enrich_target(target, history)
            ranked.append(enriched)

        # 按优先级排序
        ranked.sort(key=lambda t: t["priority_score"], reverse=True)

        return ranked

    def allocate_budget(
        self,
        total_budget: float,                # 总预算 (如 Token 数、计算小时数)
        targets: Optional[List[str]] = None
    ) -> Dict[str, float]:
        """
        在多个目标之间分配预算

        Args:
            total_budget: 总预算
            targets: 目标列表 (如果为空，则考虑所有目标)

        Returns:
            {target_id: allocated_budget} 字典
        """
        self.logger.info("allocate_budget_start", total_budget=total_budget)

        ranked_targets = self.rank_all_targets()

        if targets:
            ranked_targets = [t for t in ranked_targets if t["id"] in targets]

        # 按优先级分配预算 (优先级越高，分配越多)
        priorities = np.array([t["priority_score"] for t in ranked_targets])
        weights = priorities / np.sum(priorities) if np.sum(priorities) > 0 else np.ones(len(priorities)) / len(priorities)

        allocation = {}
        for i, target in enumerate(ranked_targets):
            allocation[target["id"]] = total_budget * weights[i]

        self.logger.info("allocate_budget_complete",
                        targets_count=len(allocation),
                        top_target=ranked_targets[0]["id"] if ranked_targets else None)

        return allocation

    # ===== Selection Strategies =====

    def _select_ucb(
        self,
        targets: List[Dict],
        history: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """
        Upper Confidence Bound (UCB) 策略

        UCB = mean_reward + sqrt(2 * ln(total_trials) / trials_this_arm)
        """
        total_trials = sum(len(history.get(t["id"], [])) for t in targets)

        ucb_scores = []
        for target in targets:
            target_history = history.get(target["id"], [])
            trials = len(target_history)

            if trials == 0:
                # 未尝试过的目标给予无穷大的 UCB (优先探索)
                ucb_scores.append(float('inf'))
            else:
                rewards = [h["benefit_score"] for h in target_history if "benefit_score" in h]
                mean_reward = np.mean(rewards) if rewards else 0.0
                exploration_bonus = np.sqrt(2 * np.log(total_trials + 1) / trials)
                ucb = mean_reward + exploration_bonus
                ucb_scores.append(ucb)

        # 选择 UCB 最高的
        best_idx = np.argmax(ucb_scores)
        return targets[best_idx]

    def _select_epsilon_greedy(
        self,
        targets: List[Dict],
        history: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """
        ε-贪心策略

        以 ε 的概率随机探索，以 1-ε 的概率选择历史最佳
        """
        epsilon = self.config["exploration_rate"]

        if np.random.random() < epsilon:
            # 探索：随机选择
            return self._select_random(targets)
        else:
            # 利用：选择历史平均收益最高的
            return self._select_best_historical(targets, history)

    def _select_thompson_sampling(
        self,
        targets: List[Dict],
        history: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """
        Thompson Sampling (贝叶斯方法)

        假设每个目标的收益服从 Beta 分布
        """
        samples = []
        for target in targets:
            target_history = history.get(target["id"], [])

            if not target_history:
                # 未尝试过，使用先验 Beta(1, 1) = Uniform(0, 1)
                alpha, beta = 1, 1
            else:
                # 根据历史成功/失败次数更新 Beta 分布
                successes = sum(1 for h in target_history if h.get("benefit_score", 0) > 0.5)
                failures = len(target_history) - successes
                alpha = 1 + successes
                beta = 1 + failures

            # 从 Beta 分布采样
            sample = np.random.beta(alpha, beta)
            samples.append(sample)

        # 选择采样值最高的
        best_idx = np.argmax(samples)
        return targets[best_idx]

    def _select_random(self, targets: List[Dict]) -> Dict[str, Any]:
        """随机选择"""
        return targets[np.random.randint(len(targets))]

    def _select_best_historical(
        self,
        targets: List[Dict],
        history: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """选择历史平均收益最高的"""
        best_target = None
        best_reward = -float('inf')

        for target in targets:
            target_history = history.get(target["id"], [])
            if target_history:
                rewards = [h["benefit_score"] for h in target_history if "benefit_score" in h]
                mean_reward = np.mean(rewards) if rewards else 0.0
                if mean_reward > best_reward:
                    best_reward = mean_reward
                    best_target = target

        return best_target if best_target else self._select_random(targets)

    # ===== Helper Methods =====

    def _load_mutable_targets(self) -> List[Dict[str, Any]]:
        """从 snapshot 或数据库加载可演化目标"""
        from pathlib import Path
        import json

        targets = []
        snapshot_path = Path("runs/memory/mutable_targets.json")
        if snapshot_path.exists():
            try:
                data = json.loads(snapshot_path.read_text())
                for target_id, target_data in data.items():
                    targets.append({
                        "id": target_id,
                        "metrics": target_data.get("metrics", {}),
                        "immune_cases": target_data.get("immune_cases", [])
                    })
            except Exception as e:
                self.logger.error("load_mutable_targets_failed", error=str(e))

        if not targets:
            # Add a low-risk placeholder target if no mutable targets are available
            targets.append({
                "id": "tool:placeholder",
                "kind": "tool",
                "domain": "trading",
                "risk_level": "low",
                "mutable": True,
                "metrics": {"health_score": 1.0, "recommendation": "healthy"},
            })

        return targets

    def _load_experiment_history(self) -> Dict[str, List[Dict]]:
        """加载历史实验数据"""
        if not self._experiments_table_exists():
            self.logger.warning(
                "experiment_history_table_unavailable",
                table="ml.experiments",
                action="use_empty_history_and_fallback_selection",
            )
            return {}

        query = """
        SELECT
            JSON_VALUE(params, '$.target') AS target_id,
            status,
            JSON_VALUE(metrics, '$.benefit_score') AS benefit_score,
            JSON_VALUE(metrics, '$.risk_score') AS risk_score,
            timestamp
        FROM ml.experiments
        WHERE kind IN ('code_patch_experiment', 'tool_patch_experiment')
          AND timestamp >= now() - INTERVAL 90 DAY
        ORDER BY timestamp DESC
        """

        try:
            df = self.ch.query_df(query)
            history = {}
            for _, row in df.iterrows():
                target_id = row["target_id"]
                if target_id not in history:
                    history[target_id] = []

                history[target_id].append({
                    "status": row["status"],
                    "benefit_score": float(row["benefit_score"]) if row["benefit_score"] else 0.0,
                    "risk_score": float(row["risk_score"]) if row["risk_score"] else 0.0,
                    "timestamp": row["timestamp"]
                })

            return history
        except Exception as e:
            err = str(e)
            # ClickHouse code 60 = UNKNOWN_TABLE; keep this as non-blocking warning.
            if "Code: 60" in err or "UNKNOWN_TABLE" in err or "Unknown table" in err:
                self.logger.warning(
                    "experiment_history_table_unavailable",
                    table="ml.experiments",
                    error=err,
                    action="use_empty_history_and_fallback_selection",
                )
                self._experiments_table_exists_cache = False
            else:
                self.logger.error("load_experiment_history_failed", error=err)
            return {}

    def _experiments_table_exists(self) -> bool:
        if self._experiments_table_exists_cache is not None:
            return bool(self._experiments_table_exists_cache)
        try:
            df = self.ch.query_df(
                """
                SELECT count() AS cnt
                FROM system.tables
                WHERE database = 'ml' AND name = 'experiments'
                """
            )
            exists = False
            if not df.empty:
                exists = int(df.iloc[0]["cnt"]) > 0
            self._experiments_table_exists_cache = exists
            return exists
        except Exception as e:
            # Conservative fallback: if metadata query fails, keep previous behavior
            # and let normal history query handle errors.
            self.logger.warning("experiment_history_table_probe_failed", error=str(e))
            self._experiments_table_exists_cache = True
            return True

    def _enrich_target(
        self,
        target: Dict[str, Any],
        history: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """增强目标信息（添加优先级、预期收益等）"""
        target_id = target["id"]
        target_history = history.get(target_id, [])

        # 计算历史统计
        if target_history:
            benefits = [h["benefit_score"] for h in target_history]
            risks = [h["risk_score"] for h in target_history]
            avg_benefit = np.mean(benefits)
            avg_risk = np.mean(risks)
            trials = len(target_history)
        else:
            avg_benefit = 0.5  # 默认中等收益
            avg_risk = 0.5     # 默认中等风险
            trials = 0

        # 计算优先级评分
        health_score = target.get("metrics", {}).get("health_score", 1.0)
        error_rate = target.get("metrics", {}).get("error_rate", 0.0)

        # 优先级 = 预期收益 - 风险 + 健康度惩罚 + 错误率惩罚
        priority_score = (
            avg_benefit * 0.4 +
            (1 - avg_risk) * 0.2 +
            (1 - health_score) * 0.2 +  # 健康度低的优先修
            error_rate * 0.2             # 错误率高的优先修
        )

        return {
            **target,
            "priority_score": round(priority_score, 3),
            "expected_benefit": round(avg_benefit, 3),
            "expected_risk": round(avg_risk, 3),
            "trials_count": trials,
            "priority": "high" if priority_score >= 0.7 else "medium" if priority_score >= 0.4 else "low"
        }

    def _get_ch_client(self):
        """获取 ClickHouse 客户端"""
        try:
            from libs.config import config
            ccfg = config.clickhouse_config
            return clickhouse_connect.get_client(
                host=ccfg.get("host", "localhost"),
                port=int(ccfg.get("port", 8123)),
                database=ccfg.get("database", "trading"),
                username=ccfg.get("user", "default"),
                password=ccfg.get("password", ""),
            )
        except Exception:
            return clickhouse_connect.get_client(host="localhost", port=8123, database="trading")


# ===== 使用示例 =====
if __name__ == "__main__":
    import json

    manager = EvolutionStrategyManager()

    # 1. 选择下一个目标
    next_target = manager.select_next_target(strategy="ucb")
    print("Next target:")
    print(json.dumps(next_target, indent=2))

    # 2. 查看所有目标的优先级排序
    print("\nAll targets ranked:")
    ranked = manager.rank_all_targets()
    for i, target in enumerate(ranked[:5], 1):
        print(f"{i}. {target['id']}: priority={target['priority']}, score={target['priority_score']:.3f}")

    # 3. 分配预算
    budget_allocation = manager.allocate_budget(total_budget=1000.0)
    print("\nBudget allocation:")
    for target_id, budget in sorted(budget_allocation.items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"  {target_id}: {budget:.1f}")
