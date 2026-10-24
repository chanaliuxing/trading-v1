# prompts/registry.py
"""
Prompt Registry: 统一管理 execution_prompt_id 白名单

功能:
- 加载 prompt_library.json
- 提供 prompt 查询和版本管理
- 支持 base_id -> tactical variants 查询
- 确保所有 prompt 可追溯
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import json


@dataclass
class PromptMeta:
    """Prompt 元数据"""
    prompt_id: str
    base_id: Optional[str]
    created_at: Optional[datetime]
    source: str
    reviewer: str
    signature: str
    version: int

    @classmethod
    def from_raw(cls, prompt_id: str, raw: Dict[str, Any]) -> "PromptMeta":
        """从 raw dict 构造 PromptMeta"""
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
    """完整的 Prompt 条目"""
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
    Prompt 注册表

    管理:
    - 顶层 base prompts (e.g., "v3_stable", "v3_fast_market_following")
    - Tactical variants (e.g., "v3_fast_market_following_tactical_20251117")

    确保:
    - 所有 prompt 有唯一 ID
    - 可追溯到 base_id
    - 可查询最新版本
    """

    def __init__(self, spec: Dict[str, Any], *, path: Optional[Path] = None) -> None:
        self._spec = spec
        self._path = path

        self._entries: Dict[str, PromptEntry] = {}
        self._base_index: Dict[str, List[str]] = {}  # base_id -> [prompt_id]
        self._load()

    @classmethod
    def from_file(cls, path: Path) -> "PromptRegistry":
        """从 JSON 文件加载"""
        with path.open("r", encoding="utf-8") as f:
            spec = json.load(f)
        return cls(spec, path=path)

    # ========== 加载 ==========

    def _load(self) -> None:
        """加载 prompt 条目"""
        meta_raw: Dict[str, Any] = self._spec.get("meta", {})
        prompts_group: Dict[str, str] = self._spec.get("prompts", {})

        # 1) 顶层 prompt (非保留 key)
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
        """注册 prompt 条目"""
        entry = PromptEntry(prompt_id=prompt_id, text=text, meta=meta)
        self._entries[prompt_id] = entry

        base_id = meta.base_id if meta else None
        if base_id:
            self._base_index.setdefault(base_id, []).append(prompt_id)

    # ========== 查询 API ==========

    def exists(self, prompt_id: str) -> bool:
        """检查 prompt 是否存在"""
        return prompt_id in self._entries

    def get(self, prompt_id: str) -> PromptEntry:
        """获取 prompt 条目"""
        if prompt_id not in self._entries:
            raise KeyError(f"Unknown prompt_id: {prompt_id}")
        return self._entries[prompt_id]

    def get_text(self, prompt_id: str) -> str:
        """获取 prompt 文本"""
        return self.get(prompt_id).text

    def get_meta(self, prompt_id: str) -> Optional[PromptMeta]:
        """获取 prompt 元数据"""
        entry = self._entries.get(prompt_id)
        return entry.meta if entry else None

    def list_all_ids(self) -> List[str]:
        """列出所有 prompt ID"""
        return sorted(self._entries.keys())

    def list_by_base(self, base_id: str) -> List[str]:
        """列出某个 base_id 下的所有 tactical variants"""
        return sorted(self._base_index.get(base_id, []))

    def latest_by_base(self, base_id: str) -> Optional[PromptEntry]:
        """
        返回某个 base_id 下按 created_at 降序的最新 prompt 版本

        如果没有 tactical variants,返回 None
        """
        candidates = [self._entries[pid] for pid in self._base_index.get(base_id, [])]
        if not candidates:
            return None

        def sort_key(e: PromptEntry):
            # created_at 为空时排在最前
            return (e.created_at or datetime.min, e.prompt_id)

        candidates.sort(key=sort_key, reverse=True)
        return candidates[0]

    def count_prompts(self) -> int:
        """总 prompt 数"""
        return len(self._entries)

    def count_base_prompts(self) -> int:
        """Base prompt 数"""
        return len(self._base_index)
