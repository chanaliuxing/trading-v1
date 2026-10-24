# brain/prompt_library.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict
import json


@dataclass
class PromptLibrary:
    raw: Dict[str, Any]
    prompts: Dict[str, str]   # prompt_id -> prompt text
    meta: Dict[str, Any]      # prompt_id -> meta dict（如果有）


    def get(self, prompt_id: str) -> str:
        """
        返回对应 prompt 文本；如果不存在，抛异常，避免静默 fallback。
        """
        try:
            return self.prompts[prompt_id]
        except KeyError:
            raise KeyError(f"Prompt id '{prompt_id}' not found in library")


    def get_meta(self, prompt_id: str) -> Dict[str, Any]:
        meta = self.meta.get(prompt_id)
        return meta or {}


def load_prompt_library(path: str | Path) -> PromptLibrary:
    """
    加载你贴的那种 prompt JSON:

    {
      "_comment": "...",
      "_usage": "...",
      "v3_stable": "....",
      "v3_fast_market_following": "....",
      ...
      "prompts": {
        "v3_fast_market_following_tactical_20251102": "....",
        ...
      },
      "meta": {
        "v3_fast_market_following_tactical_20251102": {...},
        ...
      }
    }
    """
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))

    prompts: Dict[str, str] = {}

    # 1) 顶层的 prompt（v3_stable / conservative / aggressive_scalper 等）
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k in ("prompts", "meta"):
            continue
        if isinstance(v, str):
            prompts[k] = v

    # 2) 嵌套在 "prompts" 下的演进版本
    nested = raw.get("prompts") or {}
    for k, v in nested.items():
        if isinstance(v, str):
            prompts[k] = v

    meta_raw = raw.get("meta") or {}

    return PromptLibrary(
        raw=raw,
        prompts=prompts,
        meta=meta_raw,
    )
