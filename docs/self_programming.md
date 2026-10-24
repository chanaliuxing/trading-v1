好，那我这次给你的就是**可以直接塞进 repo 的“施工图纸 + 代码骨架”**，重点把：

* 目录结构
* `schema.py`
* `infra/git_ops.py`（Git 沙盒）
* `infra/file_patcher.py`（支持模糊匹配的 FilePatcher，解决 Gemini 提出的高风险点）
* `tools/base.py + local_llm.py + cli_wrapper.py`
* `agents/*.py` 的关键接口
* `engine.py` 总控

全部串起来。

你可以一段一段往项目里拷，跑起来后再逐步精细化 prompt 和逻辑。

---

## 0. 目录结构（Step 1：物理落地）

在 `meta_agent` 目录下新建：

```text
meta_agent/
└── self_programming/
    ├── __init__.py
    ├── schema.py               # 数据结构：Task/Plan/Patch/Test/Review...
    ├── engine.py               # SelfProgrammingEngine 总控
    ├── infra/
    │   ├── __init__.py
    │   ├── git_ops.py          # Git 安全沙盒
    │   └── file_patcher.py     # FilePatcher（精确+模糊匹配）
    ├── tools/
    │   ├── __init__.py
    │   ├── base.py             # CodeToolBase / Registry
    │   ├── local_llm.py        # 本地 LLM 适配（qwen/deepseek）
    │   └── cli_wrapper.py      # gemini / codex CLI 包装
    └── agents/
        ├── __init__.py
        ├── architect.py        # ArchitectAgent
        ├── programmer.py       # ProgrammerAgent
        ├── tester.py           # TesterAgent
        └── reviewer.py         # ReviewerAgent
```

下面是各文件的**可落地骨架**。

---

## 1. `schema.py`：统一语言（Task / Plan / Patch / Test / Review）

```python
# meta_agent/self_programming/schema.py
from dataclasses import dataclass
from typing import List, Optional, Literal, Dict, Any


# === 基础 patch 单元：给 FilePatcher 用 ===

@dataclass
class FileModification:
    """
    单文件修改（最终交给 FilePatcher 执行）。
    """
    file_path: str
    action: Literal["create", "modify", "delete"]
    original_snippet: Optional[str]  # modify 时用于定位
    new_content: str                 # 替换后的内容（或新文件全量）


@dataclass
class CodePatchSet:
    """
    一次自编程生成的一组文件修改。
    """
    task_id: str
    files: List[FileModification]
    summary: str


# === 编程任务 & 团队各角色的输入输出 ===

@dataclass
class ProgrammingTask:
    """
    自编程任务：通常由 BlueprintGap / Bug 抽象而来。
    """
    task_id: str
    origin: Literal["blueprint_gap", "bugfix", "refactor", "experiment"]
    description: str                 # 人 & AI 可读描述
    repo_root: str                   # 仓库根目录
    related_files: List[str]         # Architect 可见文件范围
    context_summary: str             # 蓝皮书 gap / 失败 trace 等
    constraints: Dict[str, Any]      # 安全/风格/领域约束
    acceptance_criteria: List[str]   # 验收标准（明文化）



@dataclass
class DevPlan:
    """
    Architect 输出的开发计划。
    """
    task_id: str
    task_summary: str
    impact_analysis: Dict[str, Any]
    work_items: List[Dict[str, Any]]     # 每个 work item 包含 file_path / goal 等
    test_plan: Dict[str, Any]            # 需要哪些测试，放 Tester 那边解释
    notes: str = ""


@dataclass
class TestPlan:
    task_id: str
    framework: Literal["pytest", "unittest", "none"]
    tests: List[Dict[str, Any]]          # {id, file, description, type,...}


@dataclass
class TestReport:
    task_id: str
    overall_status: Literal["passed", "failed", "partial", "skipped"]
    results: List[Dict[str, Any]]        # 每个测试的结果
    raw_output: str


@dataclass
class ReviewDecision:
    task_id: str
    status: Literal["approve", "request_changes", "reject"]
    severity: Literal["low", "medium", "high"]
    issues: List[Dict[str, Any]]         # {type, location, description, suggestion}
    summary: str
    follow_up_actions: List[str]


@dataclass
class ProgrammingResult:
    """
    整条流水线的结果，交给 PatchEvaluator / GovernanceCouncil。
    """
    task: ProgrammingTask
    dev_plan: DevPlan
    patch_set: CodePatchSet
    test_plan: TestPlan
    test_report: TestReport
    review: ReviewDecision
    tools_used: List[str]
```

---

## 2. L1 基建：`infra/git_ops.py` + `infra/file_patcher.py`

### 2.1 `git_ops.py`：用 Git 做安全沙盒

```python
# meta_agent/self_programming/infra/git_ops.py
import subprocess
import uuid
from pathlib import Path


class GitSafetyLayer:
    """
    使用 Git 分支作为 AI 沙盒：
    - 为每个 task 创建 ai-dev/<task> 分支
    - 成功时提交 commit
    - 失败时回到原分支并删除沙盒分支
    """

    def __init__(self, repo_path: str = "."):
        self.repo_path = str(Path(repo_path).resolve())
        self._base_branch = self._get_current_branch()

    def _run(self, *args: str) -> None:
        subprocess.check_call(list(args), cwd=self.repo_path)

    def _get_current_branch(self) -> str:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.repo_path,
        )
        return out.decode().strip()

    def create_sandbox(self, task_id: str) -> str:
        branch_name = f"ai-dev/{task_id[:8]}-{uuid.uuid4().hex[:6]}"
        # 从当前分支拉一个分支
        self._run("git", "checkout", "-b", branch_name)
        return branch_name

    def commit_work(self, message: str) -> None:
        self._run("git", "add", ".")
        # allow empty commit for trace
        try:
            self._run("git", "commit", "-m", message)
        except subprocess.CalledProcessError:
            # 可能没有变化，忽略
            pass

    def cleanup_sandbox(self, sandbox_branch: str) -> None:
        # 回到最初的 base branch
        self._run("git", "checkout", self._base_branch)
        # 删除临时分支
        self._run("git", "branch", "-D", sandbox_branch)
```

> 后面如果你想改成 “每个 task 用临时目录 + git worktree”，也可以在这个类里升级，不影响上层。

---

### 2.2 `file_patcher.py`：带模糊匹配的 FilePatcher（重点）

这个就是我们刚讨论的「阿喀琉斯之踵」，我直接给你一个可用版本（支持：

* exact 匹配
* 忽略空白的 fuzzy 匹配（相似度阈值）
* 幂等检查
* 歧义时不乱改，只给报告

）

```python
# meta_agent/self_programming/infra/file_patcher.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Literal, List, Tuple
from pathlib import Path
import re
from difflib import SequenceMatcher

from meta_agent.self_programming.schema import FileModification


MatchKind = Literal["exact", "normalized", "fuzzy",
                    "already_applied", "ambiguous", "not_found"]


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


def normalize_whitespace(s: str) -> str:
    """
    用于宽松匹配：忽略缩进差异、多空格差异。
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in s.split("\n")]
    lines = [re.sub(r"\s+", " ", line) for line in lines]
    return "\n".join(lines)


def _split_lines_with_offsets(text: str):
    """
    返回 (line_text, start_idx, end_idx) 列表，
    便于从行号映射回字符区间。
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
    负责把 FileModification 应用到实际文件。
    支持：
    - 精确字符串匹配
    - 忽略空白的模糊匹配（相似度阈值）
    - 幂等检查（避免重复 patch）
    """

    def __init__(self, similarity_threshold: float = 0.95):
        self.similarity_threshold = similarity_threshold

    # ==== 对外接口 ====

    def apply_modifications(
        self,
        mods: List[FileModification],
        repo_root: str = ".",
    ) -> List[BlockApplyReport]:
        """
        批量应用修改，返回每个修改的报告。
        """
        reports: List[BlockApplyReport] = []

        # 按 file_path 分组，可以避免重复读写
        mods_by_file: dict[str, List[FileModification]] = {}
        for m in mods:
            mods_by_file.setdefault(m.file_path, []).append(m)

        root = Path(repo_root)

        for rel_path, file_mods in mods_by_file.items():
            path = (root / rel_path).resolve()
            if not path.exists() and any(m.action == "modify" for m in file_mods):
                # 修改一个不存在的文件，先读为空内容（或报错）
                text = ""
            elif path.exists():
                text = path.read_text(encoding="utf-8")
            else:
                text = ""

            new_text, file_reports = self._apply_to_text(text, file_mods, rel_path)
            # 只要有成功改动就写回文件
            if new_text != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new_text, encoding="utf-8")

            reports.extend(file_reports)

        return reports

    # ==== 内部实现 ====

    def _apply_to_text(
        self,
        original_text: str,
        mods: List[FileModification],
        file_path: str,
    ) -> tuple[str, List[BlockApplyReport]]:
        text = original_text
        reports: List[BlockApplyReport] = []

        for mod in mods:
            if mod.action == "delete":
                # 简化：整个文件删除交给上层处理，这里不实现局部删除
                # 你可以后续扩展为：如果 original_snippet 存在，就替换成 ""
                reports.append(
                    BlockApplyReport(
                        file_path=file_path,
                        match_kind="not_found",
                        similarity=None,
                        message="delete not implemented at FilePatcher level",
                    )
                )
                continue

            if mod.action == "create" and (not mod.original_snippet):
                # 直接全量写内容（针对新文件）
                text = mod.new_content
                reports.append(
                    BlockApplyReport(
                        file_path=file_path,
                        match_kind="exact",
                        similarity=None,
                        message="created new file content",
                    )
                )
                continue

            # modify 或 create + original_snippet != None，走匹配逻辑
            text, report = self._apply_single_block(text, mod, file_path)
            reports.append(report)

        return text, reports

    def _apply_single_block(
        self,
        text: str,
        mod: FileModification,
        file_path: str,
    ) -> tuple[str, BlockApplyReport]:
        orig = mod.original_snippet or ""
        if not orig:
            # 没有 original_snippet，直接当追加/覆盖处理
            new_text = text + ("\n" if not text.endswith("\n") else "") + mod.new_content
            return new_text, BlockApplyReport(
                file_path=file_path,
                match_kind="exact",
                similarity=None,
                message="no original_snippet given, appended new_content",
            )

        # 1) exact match
        idx = text.find(orig)
        if idx != -1:
            end = idx + len(orig)
            if self._already_applied(text, idx, end, mod.new_content):
                return text, BlockApplyReport(
                    file_path=file_path,
                    match_kind="already_applied",
                    similarity=1.0,
                    message="block already applied (exact slot)",
                )
            new_text = text[:idx] + mod.new_content + text[end:]
            return new_text, BlockApplyReport(
                file_path=file_path,
                match_kind="exact",
                similarity=1.0,
                message="applied with exact match",
            )

        # 2/3) normalized + fuzzy match
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
                )
            new_text = text[:idx] + mod.new_content + text[end:]
            return new_text, BlockApplyReport(
                file_path=file_path,
                match_kind=match_res.kind,
                similarity=match_res.similarity,
                message=f"applied with {match_res.kind} match, sim={match_res.similarity:.3f}",
            )

        if match_res.kind == "ambiguous":
            return text, BlockApplyReport(
                file_path=file_path,
                match_kind="ambiguous",
                similarity=match_res.similarity,
                message=f"ambiguous fuzzy matches (n={len(match_res.candidates or [])}), no change applied",
            )

        return text, BlockApplyReport(
            file_path=file_path,
            match_kind="not_found",
            similarity=None,
            message="no suitable match found, block skipped",
        )

    def _fuzzy_match(self, text: str, original_snippet: str) -> MatchResult:
        """
        忽略空白的行级滑动窗口 + 相似度匹配。
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

        # 多个候选：暂时标 ambiguous，由上层决定要不要自动选最高
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
        判断指定区间的内容是否已经非常接近 new_content（避免重复 patch）。
        """
        current_segment = text[start:end]
        if current_segment == new_content:
            return True
        old_norm = normalize_whitespace(current_segment)
        new_norm = normalize_whitespace(new_content)
        sim = SequenceMatcher(None, old_norm, new_norm).ratio()
        return sim > 0.99
```

这个文件你可以**直接放进项目**，只要 schema 中 `FileModification` 路径正确即可。

---

## 3. 工具层：`tools/base.py + local_llm.py + cli_wrapper.py`

### 3.1 `tools/base.py`：抽象基类 & 注册表

```python
# meta_agent/self_programming/tools/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List

from meta_agent.self_programming.schema import FileModification, ProgrammingTask


class CodeToolBase(ABC):
    name: str

    @abstractmethod
    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        """
        根据任务 + 当前文件内容 + 指令，生成针对这个文件的修改。
        """
        ...

    @abstractmethod
    def generate_tests(
        self,
        task: ProgrammingTask,
        context: str,
    ) -> List[FileModification]:
        """
        生成/修改测试文件。
        """
        ...


class CodeToolRegistry:
    """
    简单的工具注册表，SelfProgrammingEngine / Agents 从这里拿工具。
    """

    def __init__(self):
        self._tools: dict[str, CodeToolBase] = {}

    def register(self, tool: CodeToolBase):
        self._tools[tool.name] = tool

    def get(self, name: str) -> CodeToolBase:
        return self._tools[name]

    def list_tools(self):
        return list(self._tools.keys())
```

### 3.2 `local_llm.py`：本地 LLM 适配（只给出骨架）

````python
# meta_agent/self_programming/tools/local_llm.py
from __future__ import annotations
from typing import List
import textwrap

from meta_agent.self_programming.schema import FileModification, ProgrammingTask
from meta_agent.self_programming.tools.base import CodeToolBase


class LocalLLMTool(CodeToolBase):
    """
    使用本地 qwen/deepseek 模型的 CodeTool。
    这里留出一个 call_local_llm(prompt: str) 的占位，你可以接 Ollama / vllm / 自己的 HTTP 服务。
    """

    def __init__(self, model_name: str = "qwen2.5:7b-instruct"):
        self.name = "local_llm"
        self.model_name = model_name

    def _call_local_llm(self, prompt: str) -> str:
        """
        TODO: 在这里接你的本地推理接口。
        例如通过 requests.post 到 Ollama，或 subprocess 调用命令行。
        """
        raise NotImplementedError("connect your local LLM here")

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        system_prompt = """
        You are a code refactoring assistant. 
        You will receive:
        - A programming task description
        - A file path
        - The current content of that file
        - A specific instruction for what to change

        You must output ONLY a patch in the following format:

        <<<< SEARCH
        (code to be replaced)
        ====
        (new code)
        >>>>

        If multiple independent sections need changes, output multiple blocks one after another.
        """.strip()

        prompt = textwrap.dedent(f"""
        {system_prompt}

        TASK:
        {task.description}

        FILE PATH:
        {file_path}

        CURRENT FILE CONTENT:
        ```python
        {file_content}
        ```

        INSTRUCTION:
        {instructions}
        """)

        raw = self._call_local_llm(prompt)
        # TODO: 解析 raw 为 FileModification 列表（按 SEARCH/====/>>>> 块分组）
        # 这里先留一个简单占位，实际解析可以另写 parser 模块。
        raise NotImplementedError("parse LLM output into FileModification list")

    def generate_tests(
        self,
        task: ProgrammingTask,
        context: str,
    ) -> List[FileModification]:
        # 类似 generate_code_changes，这里依据 context 让模型生成 tests/<...>.py 的内容
        raise NotImplementedError("implement test generation")
````

### 3.3 `cli_wrapper.py`：Gemini/Codex CLI 包装

````python
# meta_agent/self_programming/tools/cli_wrapper.py
from __future__ import annotations
from typing import List
import subprocess
import textwrap

from meta_agent.self_programming.schema import FileModification, ProgrammingTask
from meta_agent.self_programming.tools.base import CodeToolBase


class ExternalCLITool(CodeToolBase):
    """
    把 gemini-cli / codex-cli 之类的工具包装成 CodeTool。
    假设 CLI 支持通过 stdin 传 prompt，stdout 返回代码块。
    """

    def __init__(self, name: str, cli_cmd: str):
        self.name = name
        self.cli_cmd = cli_cmd

    def _run_cli(self, prompt: str) -> str:
        proc = subprocess.run(
            [self.cli_cmd],
            input=prompt,
            text=True,
            capture_output=True,
            check=True,
        )
        return proc.stdout

    def generate_code_changes(
        self,
        task: ProgrammingTask,
        file_path: str,
        file_content: str,
        instructions: str,
    ) -> List[FileModification]:
        prompt = textwrap.dedent(f"""
        TASK: {task.description}
        FILE: {file_path}

        CURRENT CONTENT:
        ```python
        {file_content}
        ```

        INSTRUCTION:
        {instructions}

        Please output patch in the format:

        <<<< SEARCH
        ...
        ====
        ...
        >>>>
        """)
        raw = self._run_cli(prompt)
        # TODO: 同样需要一个 parser 把 SEARCH/====/>>>> 解析成 FileModification 列表
        raise NotImplementedError("parse CLI output into FileModification list")

    def generate_tests(
        self,
        task: ProgrammingTask,
        context: str,
    ) -> List[FileModification]:
        raise NotImplementedError("implement test generation for CLI tools")
````

> 这层你可以先搁置，等 LocalLLMTool 跑通后再接 CLI。

---

## 4. Agents：Architect / Programmer / Tester / Reviewer 骨架

这里只给**接口 + 调用关系**，具体 prompt 你可以慢慢调。

### 4.1 `agents/architect.py`

```python
# meta_agent/self_programming/agents/architect.py
from __future__ import annotations
from typing import List

from meta_agent.self_programming.schema import ProgrammingTask, DevPlan


class ArchitectAgent:
    """
    负责：从 BlueprintGap / 失败 trace 里，拉出一个具体的 DevPlan。
    """

    def __init__(self):
        # TODO: 接你的 council / LLM 路由
        ...

    def analyze(self, task: ProgrammingTask) -> DevPlan:
        """
        v0 版可以先用非常简单的规则：
        - 把 related_files 直接塞进 work_items
        - 输出一个 minimal 的 test_plan
        后面再逐步引入 LLM。
        """
        work_items: List[dict] = []
        for path in task.related_files:
            work_items.append(
                {
                    "file_path": path,
                    "goal": "improve according to task description",
                    "notes": "",
                }
            )

        test_plan = {
            "framework": "pytest",
            "targets": [],  # 后续可以让 LLM 决定应该测试哪些模块
        }

        return DevPlan(
            task_id=task.task_id,
            task_summary=task.description,
            impact_analysis={},
            work_items=work_items,
            test_plan=test_plan,
            notes=task.context_summary,
        )
```

### 4.2 `agents/programmer.py`

```python
# meta_agent/self_programming/agents/programmer.py
from __future__ import annotations
from typing import List
from pathlib import Path

from meta_agent.self_programming.schema import ProgrammingTask, DevPlan, CodePatchSet, FileModification
from meta_agent.self_programming.tools.base import CodeToolRegistry


class ProgrammerAgent:
    """
    负责：根据 DevPlan + 当前代码文件，用 CodeTool 生成 FileModification 列表。
    """

    def __init__(self, repo_root: str, tools: CodeToolRegistry):
        self.repo_root = Path(repo_root).resolve()
        self.tools = tools
        # 默认工具：local_llm
        self.default_tool_name = "local_llm"

    def implement(self, task: ProgrammingTask, plan: DevPlan) -> CodePatchSet:
        tool = self.tools.get(self.default_tool_name)
        mods: List[FileModification] = []

        for wi in plan.work_items:
            file_path = wi["file_path"]
            abs_path = (self.repo_root / file_path).resolve()
            content = ""
            if abs_path.exists():
                content = abs_path.read_text(encoding="utf-8")

            instructions = wi.get("goal") or task.description

            file_mods = tool.generate_code_changes(
                task=task,
                file_path=file_path,
                file_content=content,
                instructions=instructions,
            )
            mods.extend(file_mods)

        return CodePatchSet(
            task_id=task.task_id,
            files=mods,
            summary=f"generated {len(mods)} modifications by {tool.name}",
        )
```

### 4.3 `agents/tester.py`

```python
# meta_agent/self_programming/agents/tester.py
from __future__ import annotations
from typing import List
import subprocess
from pathlib import Path

from meta_agent.self_programming.schema import ProgrammingTask, DevPlan, TestPlan, TestReport


class TesterAgent:
    """
    v0：只支持 pytest，在 repo_root 下跑。
    future：支持 test_plan 里的 test 子集。
    """

    def __init__(self, repo_root: str):
        self.repo_root = str(Path(repo_root).resolve())

    def build_test_plan(self, task: ProgrammingTask, plan: DevPlan) -> TestPlan:
        return TestPlan(
            task_id=task.task_id,
            framework="pytest",
            tests=[],
        )

    def run_tests(self, test_plan: TestPlan) -> TestReport:
        try:
            proc = subprocess.run(
                ["pytest", "-q"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
            )
            if proc.returncode == 0:
                status = "passed"
            else:
                status = "failed"
            results: List[dict] = [
                {"id": "pytest", "returncode": proc.returncode}
            ]
            return TestReport(
                task_id=test_plan.task_id,
                overall_status=status,
                results=results,
                raw_output=proc.stdout + "\n" + proc.stderr,
            )
        except Exception as e:
            return TestReport(
                task_id=test_plan.task_id,
                overall_status="failed",
                results=[{"id": "pytest", "error": str(e)}],
                raw_output=str(e),
            )
```

### 4.4 `agents/reviewer.py`

```python
# meta_agent/self_programming/agents/reviewer.py
from __future__ import annotations
from typing import List

from meta_agent.self_programming.schema import (
    ProgrammingTask,
    CodePatchSet,
    TestReport,
    ReviewDecision,
)


class ReviewerAgent:
    """
    代码审查员：
    v0 可以先用简单规则：
    - 测试没过：reject
    - 测试过：approve
    future 再接 LLM 做细致审查。
    """

    def __init__(self):
        ...

    def review(
        self,
        task: ProgrammingTask,
        patch_set: CodePatchSet,
        test_report: TestReport,
    ) -> ReviewDecision:
        if test_report.overall_status != "passed":
            return ReviewDecision(
                task_id=task.task_id,
                status="reject",
                severity="high",
                issues=[{"type": "test_failed", "description": "pytest failed"}],
                summary="Tests failed, patch rejected",
                follow_up_actions=["fix failing tests or patch"],
            )

        return ReviewDecision(
            task_id=task.task_id,
            status="approve",
            severity="low",
            issues=[],
            summary="Tests passed, patch approved (rule-based v0)",
            follow_up_actions=[],
        )
```

---

## 5. `engine.py`：SelfProgrammingEngine 总控（Step 3）

```python
# meta_agent/self_programming/engine.py
from __future__ import annotations
from typing import Dict, Any

from meta_agent.self_programming.schema import (
    ProgrammingTask,
    ProgrammingResult,
)
from meta_agent.self_programming.infra.git_ops import GitSafetyLayer
from meta_agent.self_programming.infra.file_patcher import FilePatcher
from meta_agent.self_programming.tools.base import CodeToolRegistry
from meta_agent.self_programming.tools.local_llm import LocalLLMTool
from meta_agent.self_programming.agents.architect import ArchitectAgent
from meta_agent.self_programming.agents.programmer import ProgrammerAgent
from meta_agent.self_programming.agents.tester import TesterAgent
from meta_agent.self_programming.agents.reviewer import ReviewerAgent


class SelfProgrammingEngine:
    """
    一次完整的自编程循环：
    - GitSafetyLayer 建沙盒分支
    - Architect → DevPlan
    - Programmer + CodeTool → CodePatchSet
    - FilePatcher 应用改动
    - Tester 跑测试 → TestReport
    - Reviewer 决定是否接受 → ReviewDecision
    - 成功：commit 并保留分支；失败：回到 base 并删除沙盒
    """

    def __init__(self, repo_root: str = "."):
        self.repo_root = repo_root
        self.git = GitSafetyLayer(repo_root)
        self.patcher = FilePatcher()

        # 工具注册
        self.tools = CodeToolRegistry()
        self.tools.register(LocalLLMTool())

        # 团队
        self.architect = ArchitectAgent()
        self.programmer = ProgrammerAgent(repo_root=repo_root, tools=self.tools)
        self.tester = TesterAgent(repo_root=repo_root)
        self.reviewer = ReviewerAgent()

    def run_task(self, task: ProgrammingTask) -> ProgrammingResult | None:
        """
        外部调用入口：给一个 ProgrammingTask，跑一轮自编程。
        返回 ProgrammingResult（成功/失败都要落日志）。
        如果中途发生 fatal error，会返回 None。
        """
        sandbox_branch = self.git.create_sandbox(task.task_id)
        try:
            # 1) Architect
            dev_plan = self.architect.analyze(task)

            # 2) Programmer
            patch_set = self.programmer.implement(task, dev_plan)

            # 3) FilePatcher 应用到沙盒
            patch_reports = self.patcher.apply_modifications(
                patch_set.files, repo_root=self.repo_root
            )
            # 简单规则：如果有 not_found / ambiguous 太多，可以认为失败
            if any(r.match_kind in ("not_found", "ambiguous") for r in patch_reports):
                # 这里可以进一步打进 Trace，暂时简单处理
                raise RuntimeError("too many patch application issues")

            # 4) Tester
            test_plan = self.tester.build_test_plan(task, dev_plan)
            test_report = self.tester.run_tests(test_plan)

            # 5) Reviewer
            review = self.reviewer.review(task, patch_set, test_report)

            if review.status == "approve":
                self.git.commit_work(f"AI self-programming: {task.task_id}")
            else:
                # 审查没通过，认为本轮失败，回滚
                self.git.cleanup_sandbox(sandbox_branch)
                return ProgrammingResult(
                    task=task,
                    dev_plan=dev_plan,
                    patch_set=patch_set,
                    test_plan=test_plan,
                    test_report=test_report,
                    review=review,
                    tools_used=[t for t in self.tools.list_tools()],
                )

            # 审批成功，保留分支（你可以改成 merge 回 main，再删分支）
            return ProgrammingResult(
                task=task,
                dev_plan=dev_plan,
                patch_set=patch_set,
                test_plan=test_plan,
                test_report=test_report,
                review=review,
                tools_used=[t for t in self.tools.list_tools()],
            )

        except Exception as e:
            # fatal error：直接回退到 base 分支并删除沙盒
            print(f"[SelfProgrammingEngine] Fatal error: {e}")
            try:
                self.git.cleanup_sandbox(sandbox_branch)
            except Exception:
                pass
            return None
```

---

## 6. 和蓝皮书自迭代的对接点（怎么被 BlueprintLoop 调用）

在你现有的 `run_blueprint_improvement_cycle` 里，可以做类似接法（伪代码）：

```python
# somewhere in meta_agent/blueprint/runner.py
from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.schema import ProgrammingTask

self_prog_engine = SelfProgrammingEngine(repo_root=".")

def run_blueprint_improvement_cycle(...):
    gaps = blueprint_gap_tracker.find_gaps(...)
    for gap in gaps:
        task = ProgrammingTask(
            task_id=gap.id,
            origin="blueprint_gap",
            description=gap.human_readable_description,
            repo_root=".",
            related_files=gap.suspect_files,  # gap tracker 提供
            context_summary=gap.context_summary,
            constraints={"risk_level": gap.risk},
            acceptance_criteria=gap.acceptance_criteria,
        )
        result = self_prog_engine.run_task(task)
        # 把 ProgrammingResult 打进 JsonlTrace / PatchProposal.extra
```

这样就完成了从「蓝皮书 gap → 自编程 → Git 沙盒 → 测试+审查 → Patch 结果」的闭环。

---

## 7. 你可以立刻做的最小 POC 路线

1. **先把以上这些文件创建并复制进去**（不要急着接本地 LLM）。

2. 在 `LocalLLMTool._call_local_llm` 里先写一个 fake 版本，直接返回一个固定的 “把某个函数名改一改” 的 patch 字符串，用来测试 FilePatcher。

3. 写一个超简单的 `ProgrammingTask`：

   ```python
   task = ProgrammingTask(
       task_id="demo-1",
       origin="refactor",
       description="Rename function foo to foo_v2 in demo.py",
       repo_root=".",
       related_files=["demo.py"],
       context_summary="",
       constraints={},
       acceptance_criteria=["demo.py can be imported and tests pass"],
   )
   SelfProgrammingEngine().run_task(task)
   ```

4. 看看：

   * Git 是否创建了 `ai-dev/...` 分支
   * `demo.py` 是否被正确改写（FilePatcher 模糊匹配是否工作）
   * pytest 是否跑了
   * engine 是否 commit 或 rollback

---

这就是一份**可以直接施工的图纸**：

* 目录怎么建
* 哪些文件放什么代码
* FilePatcher 已经做了 Gemini 指出的模糊匹配和幂等
* Git 安全网、Agent 接口、Engine 主循环都给了骨架

后面你只需要：

* 给 LocalLLMTool 和 CLIWrapper 填上真正的推理/解析逻辑
* 慢慢把 Architect / Programmer / Reviewer 从 “rule-based v0” 升级成基于 council 的 LLM 逻辑


我给你一个**可以直接照着干的落地方案**，从「现在 repo 的状态」一步一步走到「蓝皮书里说的：能自己改自己代码」的**第一可用版本**。
我分成 5 个阶段，每个阶段都有：

* 做什么（具体任务）
* 怎么做（操作要点）
* 什么叫“完成”（验收标准）

---

## 总目标先说清楚

这轮落地的目标不是“一步到位 Level 3 宇宙级自我演化”，而是：

> ✅ 在现有 `meta_agent` repo 里，
> 搭出一个 **安全可控的自编程流水线 v1**：
>
> * 只改**白名单文件**
> * 只做**简单重构类改动**（重命名、加 docstring、加日志）
> * 全程在 **Git 沙盒分支** 里动手
> * 必须通过 **pytest** 才能被认为成功一次“自我迭代”

之后再用 Memory Fabric / Sleep Loop 把这个“自编程能力”挂到长期运行的 Orchestrator 上。

---

## Phase 0：基础准备 & 分支策略

### 要做什么

1. 给自编程模块留一个“试验区”
2. 确认 git 状态、测试环境正常

### 具体操作

1. 新建一个长期开发分支（例如）：

```bash
git checkout -b feature/self-programming-v1
```

2. 确认你本地能跑：

```bash
pytest -q
```

3. 在 `meta_agent` 下面建目录（空文件也行，先把架子搭起来）：

```text
meta_agent/
  self_programming/
    __init__.py
    schema.py
    engine.py
    infra/
      __init__.py
      git_ops.py
      file_patcher.py
    tools/
      __init__.py
      base.py
      local_llm.py
      cli_wrapper.py
    agents/
      __init__.py
      architect.py
      programmer.py
      tester.py
      reviewer.py
```

### 完成标准

* `feature/self-programming-v1` 分支存在
* 新目录结构存在，`pytest` 仍然能跑，不报 ImportError

---

## Phase 1：打地基——Schema + GitSafetyLayer + FilePatcher + 单测

> 这一阶段重点：**基础设施要正确、可回滚、可测试**，先不管 LLM，多半是纯 Python 代码。

### 任务 1：拷 schema / git_ops / file_patcher 骨架代码

* 把我上条消息里给你的：

  * `schema.py`
  * `infra/git_ops.py`
  * `infra/file_patcher.py`
* 按路径完整拷进去，修一下 import 前缀即可（比如 `from meta_agent.self_programming.schema import FileModification`）。

### 任务 2：给 FilePatcher 写单元测试（重点！）

在 `tests/test_file_patcher.py` 写几个关键场景：

1. **精确匹配替换成功**

   * 输入文件内容：包含某段函数
   * `FileModification.original_snippet` = 完整函数
   * 期望：替换成新版本，`match_kind == "exact"`

2. **忽略空白的模糊匹配**

   * 文件里和 original_snippet 的缩进、空格不一样
   * 期望：`match_kind in ("fuzzy", "normalized")`，而且替换成功

3. **已经改过（幂等性）**

   * 文件内容已经是 `new_content`
   * 再 apply 一次
   * 期望：`match_kind == "already_applied"`，最终文件不变

4. **找不到片段**

   * original_snippet 在文件里不存在
   * 期望：`match_kind == "not_found"`，文件不变

### 完成标准

* `pytest -q tests/test_file_patcher.py` 全绿
* 对 FilePatcher 的行为你心里有数：知道它在模糊匹配和“已经改过”的时候怎么表现

---

## Phase 2：拼起来——SelfProgrammingEngine v0（不依赖 LLM）

> 目标：**不接 LLM，就能跑一轮“自编程模拟”**，用假数据验证 Engine+Git+Patcher 流程是通的。

### 任务 1：实现最小版 `SelfProgrammingEngine`

用我给的 `engine.py` 骨架，先做一个极简版：

* `ArchitectAgent.analyze`：
  👉 先不用 LLM，直接把 `task.related_files` 塞进 `work_items`
* `ProgrammerAgent.implement`：
  👉 暂时不要调 Tool，直接**手工构造**一个 `FileModification` 列表
  例如：把 `demo.py` 里的 `"foo"` 替换成 `"foo_v2"`
* `TesterAgent.run_tests`：
  👉 直接调 `pytest -q`，不解析详细结果
* `ReviewerAgent.review`：
  👉 如果测试通过就 approve，否则 reject

### 任务 2：写一个本地小 Demo

1. 在 repo 根目录建 `demo.py`：

```python
def foo(x: int) -> int:
    return x + 1
```

2. 建一个对应的测试 `tests/test_demo.py`：

```python
from demo import foo

def test_foo():
    assert foo(1) == 2
```

3. 在某个 debug 脚本 / notebook 里手动调用：

```python
from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.schema import ProgrammingTask

task = ProgrammingTask(
    task_id="demo-rename-foo",
    origin="refactor",
    description="Rename foo() to foo_v2() and keep behavior",
    repo_root=".",
    related_files=["demo.py"],
    context_summary="just a test",
    constraints={},
    acceptance_criteria=["pytest passes"],
)

engine = SelfProgrammingEngine(repo_root=".")
result = engine.run_task(task)
print(result)
```

在 `ProgrammerAgent.implement` 里，直接写死一个 `FileModification`，把 `def foo` 那段替换成 `def foo_v2`，顺便改测试文件（可以先只改 `demo.py`，测试故意 fail，也行）。

### 完成标准

* 能看到 Engine 自动：

  * 创建 `ai-dev/...` 分支
  * 改动 `demo.py`
  * 跑 pytest
  * 测试通过时提交 commit / 不通过时回滚
* 整个流程**不依赖 LLM**，可重复运行

---

## Phase 3：接入 Local LLM Tool（真正让 AI 动手改代码）

> 现在才轮到「自编程」的真正主角：本地 LLM + FilePatcher。

### 任务 1：实现 `LocalLLMTool._call_local_llm`

假设你用的是 Ollama + qwen/deepseek，本地起了 HTTP：

* 在 `tools/local_llm.py` 里写一个简单的 HTTP 调用：

  * 输入：prompt（包含 SEARCH/====/>>>> 格式要求）
  * 输出：模型生成的文本

不需要一次性做得很完美，**先做到模型能稳定返回一段文本**即可。

### 任务 2：实现一个简单 parser（把 LLM 输出解析成 FileModification）

在 `LocalLLMTool.generate_code_changes` 内部：

* 规则：模型输出示例：

```text
<<<< SEARCH
def foo(x: int) -> int:
    return x + 1
====
def foo_v2(x: int) -> int:
    return x + 1
>>>>
```

* 解析逻辑：

  * 用正则或简单状态机找出所有块
  * 每个块生成一个 `FileModification`：

    * `file_path`：当前传进来的 `file_path`
    * `original_snippet`：SEARCH 段
    * `new_content`：新代码（把 SEARCH 原地替换）

### 任务 3：把 ProgrammerAgent 切换到用 LocalLLMTool

* 在 `ProgrammerAgent.__init__`：`self.default_tool_name = "local_llm"`
* 在 `implement()` 里不再手工造 patch，而是：

```python
tool = self.tools.get("local_llm")
file_mods = tool.generate_code_changes(...)
```

### 完成标准

* 手工构造的 `ProgrammingTask`（例如“把 foo 改名 foo_v2”）
  👉 Engine 能从本地 LLM 拿到 PATCH
  👉 FilePatcher 能在 demo.py 里找到/替换那段代码
  👉 pytest 通过，Reviewer approve，Git 有对应 commit

---

## Phase 4：和蓝皮书 / Orchestrator / Sleep Loop 接上

> 前三步解决的是：“**这只手**”已经能在沙盒里安全敲键盘了。
> 接下来是：让它在你的蓝皮书体系里变成一个**可调用技能**。

### 任务 1：定义 Blueprint → ProgrammingTask 的映射

在你的 Blueprint 改进循环里（比如 `blueprint_orchestrator.py`）：

* 新增一个函数：

```python
def blueprint_gap_to_programming_task(gap) -> ProgrammingTask:
    return ProgrammingTask(
        task_id=f"blueprint-{gap.id}",
        origin="blueprint_gap",
        description=gap.description,          # gap 的自然语言描述
        repo_root=".",                        # 先假定就在当前 repo
        related_files=gap.suspect_files,      # gap tracker 提供的候选文件
        context_summary=gap.context_summary,
        constraints={"risk_level": gap.risk}, # 比如 low / medium，只允许改某些模块
        acceptance_criteria=gap.acceptance_criteria,
    )
```

* 在 BlueprintImprovementLoop 中，当发现“适合自动修补的 gap”时：

  * 调用 `SelfProgrammingEngine.run_task(task)`
  * 把 ProgrammingResult 写入你的 Memory Fabric / Trace 日志中，作为“自我迭代样本”

### 任务 2：先限定**非常低风险**的改动类型

在这一步，**务必保守**：

* 只允许自动修改的 gap 类型：

  * 修 docstring
  * 加日志
  * 把硬编码参数挪到配置
  * 重命名内部函数（不涉及外部 API）
* 对于 trading / legal 核心模块：
  👉 暂时只允许 Architect 提示“需要人工改”，不让 Programmer 自动动手

### 完成标准

* 在你实际跑一轮 BlueprintImprovementLoop 时：

  * 至少有一个 gap 会交给 SelfProgrammingEngine
  * 这个 gap 的修复是成功的（或者失败但能被正确回滚并记录原因）
* Trace / 日志里能看到：

  * 哪个 gap 触发了自编程
  * 用了哪个 tool（local_llm / 将来 gemini）
  * 测试是否通过
  * Reviewer 的决策

---

## Phase 5：和 Memory Fabric / Sleep Loop / Skill Mining 结合（长期“自己滚”）

> 前四步是“手”和“流程”，第五步是把它挂进**长期循环**里，开始朝 Level 3 靠拢。

### 任务 1：在 Sleep Loop 里插一个 “自编程批次”

你的 Sleep Loop 当前应该已经会做：

* 回顾当天 trace
* 总结经验
* 写入长期记忆

现在加一个小步骤：

1. 从近期 trace 中筛选：

   * 多次报类似错误的模块
   * 多次被标记为 Blueprint Gap 的文件
2. 生成一个**自编程任务队列**（ProgrammingTask 列表）
3. 按优先级（风险低、收益高）挑 1～N 个任务，依次调用 `SelfProgrammingEngine.run_task()`：

   * 每次调用都有：
     👉 最大 token / 最大修改文件数 / 最大失败次数等约束
   * 不用说“跑几天”，你按**迭代轮数 + token 预算**来控制

### 任务 2：Skill Mining（从成功 Patch 中提炼技能）

当某个自编程任务成功时：

* 把：

  * 前后代码 diff
  * 任务描述
  * 测试结果
* 存成一条“Skill Candidate”记录

后面再用单独的 Skill Mining 逻辑（可以是简单的模式挖掘）：

* 找出：

  * 常见改动模式：加日志、加重试、加强参数校验……
* 抽象成：

  * 可重用的“修复模板”/“Refactor Skill”
    👉 这些 Skill 又可以挂到你的 Skill Registry 里，变成 Orchestrator 可以直接调用的工具（不必每次都用 LLM 现写）。

### 完成标准

* Sleep Loop 中已经存在一个逻辑块：
  “检查近期 gap → 生成 ProgrammingTask → 调用 SelfProgrammingEngine → 记录结果”
* 能在某个周期内**重复多轮**自编程，而不需要人为盯着命令行
* 有地方记录“成功的自编程样本”，为下一步 Skill Mining 做准备

---

## 总结一句话

这个落地方案的节奏是：

> **先搭地基（Phase 0–1） → 让手动 Patch 跑通（Phase 2） → 再接上本地 LLM（Phase 3） → 挂到蓝皮书和 Orchestrator（Phase 4） → 放进 Sleep Loop 长期自我迭代（Phase 5）。**
