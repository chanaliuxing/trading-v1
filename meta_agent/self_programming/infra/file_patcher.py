from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Literal, Optional, Tuple
import re

from meta_agent.self_programming.schema import CodePatchSet, FileModification


MatchKind = Literal["exact", "normalized", "fuzzy", "already_applied", "ambiguous", "not_found"]


@dataclass
class MatchResult:
    kind: MatchKind
    start_idx: Optional[int] = None
    end_idx: Optional[int] = None
    similarity: Optional[float] = None
    candidates: Optional[List[Tuple[int, int, float]]] = None  # (start, end, sim)


@dataclass
class BlockApplyReport:
    file_path: str
    match_kind: MatchKind
    similarity: Optional[float]
    message: str
    applied: bool = False
    conflict: bool = False
    diff: Optional[str] = None


def normalize_whitespace(s: str) -> str:
    """
    Normalize whitespace for fuzzy matching: ignore indentation and multiple spaces.
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in s.split("\n")]
    lines = [re.sub(r"\s+", " ", line) for line in lines]
    return "\n".join(lines)


def _split_lines_with_offsets(text: str) -> List[Tuple[str, int, int]]:
    """
    Returns list of (line_text, start_idx, end_idx) for mapping line numbers to character ranges.
    """
    lines = []
    start = 0
    for line in text.splitlines(keepends=True):
        end = start + len(line)
        lines.append((line, start, end))
        start = end
    return lines


class FilePatcher:
    """
    Applies FileModification to actual files with support for:
    - Exact string matching
    - Fuzzy matching (normalized whitespace with similarity threshold)
    - Idempotency checking (avoid duplicate patches)
    - Ambiguity detection
    """

    def __init__(self, similarity_threshold: float = 0.95):
        self.similarity_threshold = similarity_threshold

    # ==== Public Interface ====

    def apply(
        self,
        patch_set: CodePatchSet,
        repo_root: str = ".",
        dry_run: bool = False,
    ) -> List[BlockApplyReport]:
        """
        Apply all modifications in a CodePatchSet, returning reports for each modification.
        """
        return self.apply_modifications(patch_set.files, repo_root, dry_run=dry_run)

    def apply_modifications(
        self,
        mods: List[FileModification],
        repo_root: str = ".",
        dry_run: bool = False,
    ) -> List[BlockApplyReport]:
        """
        Batch apply modifications, returning a report for each modification.
        """
        reports: List[BlockApplyReport] = []

        # Group by file_path to avoid repeated read/write
        mods_by_file: dict[str, List[FileModification]] = {}
        for m in mods:
            mods_by_file.setdefault(m.file_path, []).append(m)

        root = Path(repo_root)

        for rel_path, file_mods in mods_by_file.items():
            path = (root / rel_path).resolve()
            if not path.exists() and any(m.action == "modify" for m in file_mods):
                # Modifying a non-existent file - treat as empty
                text = ""
            elif path.exists():
                text = path.read_text(encoding="utf-8")
            else:
                text = ""

            new_text, file_reports = self._apply_to_text(text, file_mods, rel_path)
            file_diff: Optional[str] = None

            if new_text != text:
                file_diff = self._build_diff(text, new_text, rel_path)

            # Write back if changed
            if new_text != text and not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new_text, encoding="utf-8")
                if file_diff and file_reports:
                    file_reports[-1].diff = file_diff
            elif dry_run and file_diff:
                for r in file_reports:
                    r.diff = file_diff
                    if r.applied:
                        r.message = f"{r.message} (dry-run, not written)"

            reports.extend(file_reports)

        return reports

    # ==== Internal Implementation ====

    def _apply_to_text(
        self,
        original_text: str,
        mods: List[FileModification],
        file_path: str,
    ) -> tuple[str, List[BlockApplyReport]]:
        """
        Apply modifications to text content, returning (new_text, reports).
        """
        text = original_text
        reports: List[BlockApplyReport] = []
        applied_spans: List[tuple[int, int]] = []

        for mod in mods:
            prev_text = text
            if mod.action == "delete":
                # Simplified: file deletion handled at upper layer
                # Could extend to support partial deletion via original_snippet
                reports.append(
                    BlockApplyReport(
                        file_path=file_path,
                        match_kind="not_found",
                        similarity=None,
                        message="delete action not implemented at FilePatcher level",
                        conflict=True,
                    )
                )
                continue

            if mod.action == "create" and (not mod.original_snippet):
                # Full file creation - overwrite with new_content
                text = mod.new_content
                reports.append(
                    BlockApplyReport(
                        file_path=file_path,
                        match_kind="exact",
                        similarity=None,
                        message="created new file content",
                        applied=True,
                    )
                )
                continue

            # modify or create with original_snippet - use matching logic
            text, report, span = self._apply_single_block(text, mod, file_path)

            # Detect overlap/conflict with previously applied spans
            if span and any(not (span[1] <= s or span[0] >= e) for s, e in applied_spans):
                report.conflict = True
                report.message = f"{report.message} (conflict: overlapping patch)"
                # Roll back to original text to avoid partial apply
                text = prev_text
            elif span:
                applied_spans.append(span)

            reports.append(report)

        return text, reports

    def _apply_single_block(
        self,
        text: str,
        mod: FileModification,
        file_path: str,
    ) -> tuple[str, BlockApplyReport, Optional[tuple[int, int]]]:
        """
        Apply a single modification block to text.
        """
        orig = mod.original_snippet or ""
        if not orig:
            # No original_snippet - append new_content
            new_text = text + ("\n" if text and not text.endswith("\n") else "") + mod.new_content
            return new_text, BlockApplyReport(
                file_path=file_path,
                match_kind="exact",
                similarity=None,
                message="no original_snippet given, appended new_content",
                applied=True,
            ), (len(text), len(new_text))

        # 1) Try exact match
        idx = text.find(orig)
        if idx != -1:
            end = idx + len(orig)
            if self._already_applied(text, idx, end, mod.new_content):
                return text, BlockApplyReport(
                    file_path=file_path,
                    match_kind="already_applied",
                    similarity=1.0,
                    message="block already applied (exact slot)",
                    applied=False,
                ), None
            new_text = text[:idx] + mod.new_content + text[end:]
            return new_text, BlockApplyReport(
                file_path=file_path,
                match_kind="exact",
                similarity=1.0,
                message="applied with exact match",
                applied=True,
            ), (idx, idx + len(mod.new_content))

        # 2/3) Try normalized + fuzzy match
        match_res = self._fuzzy_match(text, orig)

        if match_res.kind in ("fuzzy", "normalized"):
            assert match_res.start_idx is not None and match_res.end_idx is not None
            idx, end = match_res.start_idx, match_res.end_idx
            if self._already_applied(text, idx, end, mod.new_content):
                return text, BlockApplyReport(
                    file_path=file_path,
                    match_kind="already_applied",
                    similarity=match_res.similarity,
                    message=f"block already applied (fuzzy slot, sim={match_res.similarity:.3f})",
                    applied=False,
                ), None
            new_text = text[:idx] + mod.new_content + text[end:]
            return new_text, BlockApplyReport(
                file_path=file_path,
                match_kind=match_res.kind,
                similarity=match_res.similarity,
                message=f"applied with {match_res.kind} match, sim={match_res.similarity:.3f}",
                applied=True,
            ), (idx, idx + len(mod.new_content))

        if match_res.kind == "ambiguous":
            return text, BlockApplyReport(
                file_path=file_path,
                match_kind="ambiguous",
                similarity=match_res.similarity,
                message=f"ambiguous fuzzy matches (n={len(match_res.candidates or [])}), no change applied",
                conflict=True,
            ), None

        return text, BlockApplyReport(
            file_path=file_path,
            match_kind="not_found",
            similarity=None,
            message="no suitable match found, block skipped",
            conflict=True,
        ), None

    def _fuzzy_match(self, text: str, original_snippet: str) -> MatchResult:
        """
        Fuzzy match using normalized whitespace and sliding window with similarity scoring.
        """
        orig_norm = normalize_whitespace(original_snippet)
        orig_lines = original_snippet.splitlines()
        n = max(1, len(orig_lines))

        lines_with_offsets = _split_lines_with_offsets(text)
        total_lines = len(lines_with_offsets)

        candidates: List[Tuple[int, int, float]] = []
        best_sim = 0.0

        for i in range(0, max(1, total_lines - n + 1)):
            window_lines = lines_with_offsets[i : i + n]
            window_text = "".join(l[0] for l in window_lines)
            window_norm = normalize_whitespace(window_text)
            sim = SequenceMatcher(None, orig_norm, window_norm).ratio()

            if sim >= self.similarity_threshold:
                start_idx = window_lines[0][1]
                end_idx = window_lines[-1][2]
                candidates.append((start_idx, end_idx, sim))
                if sim > best_sim:
                    best_sim = sim

        if not candidates:
            return MatchResult(kind="not_found")

        if len(candidates) == 1:
            s, e, sim = candidates[0]
            return MatchResult(
                kind="fuzzy", start_idx=s, end_idx=e, similarity=sim
            )

        # Multiple candidates: mark as ambiguous
        candidates.sort(key=lambda x: x[2], reverse=True)
        s, e, sim = candidates[0]
        return MatchResult(
            kind="ambiguous",
            start_idx=s,
            end_idx=e,
            similarity=sim,
            candidates=candidates,
        )

    def _already_applied(self, text: str, start: int, end: int, new_content: str) -> bool:
        """
        Check if the specified region already matches new_content (idempotency check).
        """
        current_segment = text[start:end]
        if current_segment == new_content:
            return True
        old_norm = normalize_whitespace(current_segment)
        new_norm = normalize_whitespace(new_content)
        sim = SequenceMatcher(None, old_norm, new_norm).ratio()
        return sim > 0.99

    def _build_diff(self, old_text: str, new_text: str, rel_path: str) -> str:
        """
        Build unified diff for reporting/dry-run output.
        """
        import difflib

        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
        return "\n".join(diff)


__all__ = ["FilePatcher", "BlockApplyReport", "MatchResult"]
