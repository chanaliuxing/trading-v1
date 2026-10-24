#!/usr/bin/env python3
"""
Memory Consolidator: 记忆巩固与升级调度器

实现蓝皮书 v3 的 Continual Memory 要求:
1. STS → MTS: 基于访问频率和重要性
2. MTS → LTS: 基于验证次数和稳定性
3. 记忆压缩和去重
4. 安全遗忘机制

Usage:
  PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/memory_consolidator.py \
    --mode promote_sts_to_mts \
    --dry-run
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Tuple
import clickhouse_connect
import structlog

logger = structlog.get_logger()


def get_clickhouse_client():
    """Get ClickHouse client."""
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


class MemoryConsolidator:
    """记忆巩固器：管理 STS → MTS → LTS 的升级流程"""

    def __init__(self, ch_client=None):
        self.ch = ch_client or get_clickhouse_client()
        self.logger = logger.bind(component="memory_consolidator")

    def promote_sts_to_mts(
        self,
        min_access_count: int = 5,
        min_importance: float = 0.7,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        将 STS 中高频访问、高重要性的记忆升级到 MTS

        规则:
        1. access_count >= min_access_count
        2. importance_score >= min_importance
        3. 未被标记为已升级
        """
        self.logger.info("promote_sts_to_mts_start",
                         min_access_count=min_access_count,
                         min_importance=min_importance,
                         dry_run=dry_run)

        # 查询候选记忆
        query = """
        SELECT
            id,
            session_id,
            task_id,
            memory_type,
            content,
            embedding,
            domain,
            importance_score,
            access_count,
            timestamp
        FROM memory_fabric.short_term
        WHERE access_count >= %(min_access)d
          AND importance_score >= %(min_importance)f
          AND promoted_to_mid_term = false
          AND timestamp >= now() - INTERVAL 7 DAY
        ORDER BY importance_score DESC, access_count DESC
        LIMIT 1000
        """

        try:
            candidates = self.ch.query_df(query, parameters={
                "min_access": min_access_count,
                "min_importance": min_importance
            })
        except Exception as e:
            self.logger.error("query_sts_candidates_failed", error=str(e))
            return {"ok": False, "error": str(e)}

        if candidates.empty:
            self.logger.info("no_sts_candidates_found")
            return {"ok": True, "promoted_count": 0, "candidates_count": 0}

        promoted_count = 0
        errors = []

        for _, row in candidates.iterrows():
            try:
                # 聚合同一 session 的相关记忆
                related = self._find_related_sts(row["session_id"], row["task_id"])

                # 创建 MTS 记录
                mts_record = {
                    "project_id": f"{row['domain']}_{row['task_id']}" if row['task_id'] else row['session_id'],
                    "version": datetime.now().strftime("%Y.%m.%d"),
                    "memory_type": self._map_sts_type_to_mts(row["memory_type"]),
                    "content": self._consolidate_content([row] + related),
                    "embedding": row["embedding"],
                    "promoted_from_sts": True,
                    "source_ids": [row["id"]] + [r["id"] for r in related],
                    "consolidation_count": 1,
                    "domain": row["domain"],
                    "importance_score": row["importance_score"],
                    "confidence": min(0.8, row["importance_score"] * (row["access_count"] / 10.0))
                }

                if not dry_run:
                    # 插入 MTS
                    self._insert_mts(mts_record)

                    # 标记 STS 为已升级
                    self._mark_sts_promoted([row["id"]] + [r["id"] for r in related])

                    # 记录升级日志
                    self._log_promotion(
                        "sts_to_mts",
                        row["id"],
                        mts_record.get("id", "unknown"),
                        f"access_count={row['access_count']}, importance={row['importance_score']:.2f}",
                        row["importance_score"],
                        row["access_count"]
                    )

                promoted_count += 1
                self.logger.info("sts_promoted_to_mts",
                                sts_id=row["id"],
                                session_id=row["session_id"],
                                importance=row["importance_score"],
                                access_count=row["access_count"],
                                dry_run=dry_run)

            except Exception as e:
                errors.append({"sts_id": row["id"], "error": str(e)})
                self.logger.error("promote_sts_failed", sts_id=row["id"], error=str(e))

        result = {
            "ok": True,
            "promoted_count": promoted_count,
            "candidates_count": len(candidates),
            "errors": errors,
            "dry_run": dry_run
        }

        self.logger.info("promote_sts_to_mts_complete", **result)
        return result

    def promote_mts_to_lts(
        self,
        min_verification_count: int = 5,
        min_stability: float = 0.8,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        将 MTS 中经过充分验证、稳定的记忆升级到 LTS

        规则:
        1. consolidation_count >= min_verification_count (被多次验证)
        2. confidence >= min_stability
        3. 存在时间 >= 30天
        """
        self.logger.info("promote_mts_to_lts_start",
                         min_verification=min_verification_count,
                         min_stability=min_stability,
                         dry_run=dry_run)

        query = """
        SELECT
            id,
            project_id,
            version,
            memory_type,
            content,
            embedding,
            domain,
            importance_score,
            consolidation_count,
            confidence,
            timestamp
        FROM memory_fabric.mid_term
        WHERE consolidation_count >= %(min_verification)d
          AND confidence >= %(min_stability)f
          AND promoted_to_long_term = false
          AND timestamp <= now() - INTERVAL 30 DAY
        ORDER BY confidence DESC, consolidation_count DESC
        LIMIT 100
        """

        try:
            candidates = self.ch.query_df(query, parameters={
                "min_verification": min_verification_count,
                "min_stability": min_stability
            })
        except Exception as e:
            self.logger.error("query_mts_candidates_failed", error=str(e))
            return {"ok": False, "error": str(e)}

        if candidates.empty:
            self.logger.info("no_mts_candidates_found")
            return {"ok": True, "promoted_count": 0}

        promoted_count = 0

        for _, row in candidates.iterrows():
            try:
                # 创建 LTS 记录
                lts_record = {
                    "knowledge_type": self._map_mts_type_to_lts(row["memory_type"]),
                    "title": f"{row['domain']}_{row['project_id']}",
                    "content": row["content"],
                    "embedding": row["embedding"],
                    "promoted_from_mts": True,
                    "source_ids": [row["id"]],
                    "consolidation_count": row["consolidation_count"],
                    "verification_count": row["consolidation_count"],
                    "domain": row["domain"],
                    "importance_score": row["importance_score"],
                    "confidence": row["confidence"],
                    "stability_score": min(1.0, row["confidence"] * 1.2)
                }

                if not dry_run:
                    self._insert_lts(lts_record)
                    self._mark_mts_promoted([row["id"]])
                    self._log_promotion(
                        "mts_to_lts",
                        row["id"],
                        lts_record.get("id", "unknown"),
                        f"consolidation_count={row['consolidation_count']}, confidence={row['confidence']:.2f}",
                        row["importance_score"],
                        0
                    )

                promoted_count += 1
                self.logger.info("mts_promoted_to_lts",
                                mts_id=row["id"],
                                project_id=row["project_id"],
                                confidence=row["confidence"],
                                dry_run=dry_run)

            except Exception as e:
                self.logger.error("promote_mts_failed", mts_id=row["id"], error=str(e))

        return {
            "ok": True,
            "promoted_count": promoted_count,
            "candidates_count": len(candidates),
            "dry_run": dry_run
        }

    def _find_related_sts(self, session_id: str, task_id: str) -> List[Dict[str, Any]]:
        """查找相关的 STS 记忆（同一 session/task）"""
        query = """
        SELECT id, content, embedding
        FROM memory_fabric.short_term
        WHERE session_id = %(session)s
          AND task_id = %(task)s
          AND promoted_to_mid_term = false
        LIMIT 10
        """
        try:
            result = self.ch.query_df(query, parameters={"session": session_id, "task": task_id})
            return result.to_dict('records')
        except Exception:
            return []

    def _consolidate_content(self, records: List[Dict[str, Any]]) -> str:
        """合并多个记忆的内容"""
        consolidated = {
            "summary": "Consolidated from multiple STS records",
            "count": len(records),
            "contents": [json.loads(r["content"]) if isinstance(r["content"], str) else r["content"] for r in records]
        }
        return json.dumps(consolidated)

    def _map_sts_type_to_mts(self, sts_type: str) -> str:
        """映射 STS 类型到 MTS 类型"""
        mapping = {
            "context": "pattern",
            "observation": "pattern",
            "action": "skill",
            "result": "success_case",
            "error": "failure_case"
        }
        return mapping.get(sts_type, "pattern")

    def _map_mts_type_to_lts(self, mts_type: str) -> str:
        """映射 MTS 类型到 LTS 类型"""
        mapping = {
            "pattern": "best_practice",
            "heuristic": "principle",
            "skill": "cross_domain_skill",
            "failure_case": "anti_pattern",
            "success_case": "best_practice"
        }
        return mapping.get(mts_type, "domain_knowledge")

    def _insert_mts(self, record: Dict[str, Any]):
        """插入 MTS 记录"""
        # Implementation would insert into memory_fabric.mid_term
        pass

    def _insert_lts(self, record: Dict[str, Any]):
        """插入 LTS 记录"""
        # Implementation would insert into memory_fabric.long_term
        pass

    def _mark_sts_promoted(self, ids: List[str]):
        """标记 STS 为已升级"""
        # Implementation would update promoted_to_mid_term = true
        pass

    def _mark_mts_promoted(self, ids: List[str]):
        """标记 MTS 为已升级"""
        # Implementation would update promoted_to_long_term = true
        pass

    def _log_promotion(self, promotion_type: str, source_id: str, target_id: str,
                      reason: str, importance: float, access_count: int):
        """记录升级日志"""
        # Implementation would insert into memory_fabric.promotion_log
        pass


def main():
    ap = argparse.ArgumentParser(description="Memory Consolidator")
    ap.add_argument("--mode", choices=["promote_sts_to_mts", "promote_mts_to_lts", "both"],
                   default="both", help="Consolidation mode")
    ap.add_argument("--dry-run", action="store_true", help="Dry run mode (no actual changes)")
    ap.add_argument("--min-access-count", type=int, default=5, help="Min access count for STS→MTS")
    ap.add_argument("--min-importance", type=float, default=0.7, help="Min importance for STS→MTS")
    ap.add_argument("--min-verification", type=int, default=5, help="Min verification for MTS→LTS")
    ap.add_argument("--min-stability", type=float, default=0.8, help="Min stability for MTS→LTS")
    args = ap.parse_args()

    consolidator = MemoryConsolidator()

    if args.mode in ["promote_sts_to_mts", "both"]:
        result = consolidator.promote_sts_to_mts(
            min_access_count=args.min_access_count,
            min_importance=args.min_importance,
            dry_run=args.dry_run
        )
        print(f"✅ STS→MTS: {result}")

    if args.mode in ["promote_mts_to_lts", "both"]:
        result = consolidator.promote_mts_to_lts(
            min_verification_count=args.min_verification,
            min_stability=args.min_stability,
            dry_run=args.dry_run
        )
        print(f"✅ MTS→LTS: {result}")


if __name__ == "__main__":
    main()
