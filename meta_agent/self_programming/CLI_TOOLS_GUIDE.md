# 🛠️ External CLI Tools Integration Guide

This guide shows how to use Gemini CLI, Codex CLI, and other external code generation tools with the self-programming system.

---

## 🎯 Overview

The self-programming system supports multiple LLM backends:

| Tool | Description | CLI Command |
|------|-------------|-------------|
| **local** | Local Ollama (qwen/deepseek) | Built-in |
| **gemini** | Google Gemini via CLI | `aichat`, `gemini-cli`, etc. |
| **codex** | OpenAI GPT-4/Codex via CLI | `openai`, custom scripts |
| **aider** | Aider AI pair programming | `aider` |
| **custom** | Any CLI tool | Your command |

---

## 📦 Setup Instructions

### Option 1: Gemini CLI (using `aichat`)

```bash
# 1. Install aichat
cargo install aichat
# OR
brew install aichat

# 2. Configure Gemini API key
export GOOGLE_API_KEY="your-key-here"

# 3. Test it
echo "Write a hello world function" | aichat --model gemini-pro

# 4. Use with self-programming
python scripts/run_self_programming.py \
    --task "Add docstrings" \
    --files demo.py \
    --use-llm \
    --tool gemini \
    --tool-cli aichat \
    --tool-model gemini-pro
```

**Supported `aichat` models:**
- `gemini-pro`
- `gemini-1.5-flash`
- `gemini-1.5-pro`

---

### Option 2: Codex/GPT-4 CLI (using OpenAI CLI)

```bash
# 1. Install OpenAI CLI
pip install openai

# 2. Set API key
export OPENAI_API_KEY="your-key-here"

# 3. Test it
echo "Write hello world" | openai api completions.create -m gpt-4 -p -

# 4. Use with self-programming
python scripts/run_self_programming.py \
    --task "Refactor this function" \
    --files demo.py \
    --use-llm \
    --tool codex \
    --tool-cli openai \
    --tool-model gpt-4
```

---

### Option 3: Aider (AI Pair Programming)

```bash
# 1. Install Aider
pip install aider-chat

# 2. Set API key
export OPENAI_API_KEY="your-key-here"

# 3. Use with self-programming
python scripts/run_self_programming.py \
    --task "Add error handling" \
    --files demo.py \
    --use-llm \
    --tool aider \
    --tool-model gpt-4

# Note: Aider modifies files directly, so use Git sandbox!
```

---

### Option 4: Custom CLI Tool

```bash
# Any tool that:
# - Reads prompt from stdin
# - Outputs code to stdout

# Example: Using llm CLI (simonw/llm)
pip install llm

# Use it:
python scripts/run_self_programming.py \
    --task "Add logging" \
    --files demo.py \
    --use-llm \
    --tool custom \
    --tool-cli llm \
    --tool-model gpt-3.5-turbo
```

---

## 🚀 Usage Examples

### Example 1: Gemini via aichat

```bash
python scripts/run_self_programming.py \
    --task "Add comprehensive type hints to all functions" \
    --files demo.py \
    --use-llm \
    --use-sandbox \
    --tool gemini \
    --tool-cli aichat \
    --tool-model gemini-1.5-flash
```

### Example 2: GPT-4 via OpenAI CLI

```bash
python scripts/run_self_programming.py \
    --task "Refactor calculate_sum to use reduce" \
    --files demo.py \
    --use-llm \
    --use-sandbox \
    --tool codex \
    --tool-model gpt-4
```

### Example 3: Programmatic Usage

```python
from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.integration import create_gap_from_todo

# Create task
gap = create_gap_from_todo(
    description="Add docstrings to all public methods",
    files=["demo.py"],
)

task = blueprint_gap_to_programming_task(gap)

# Use Gemini
engine = SelfProgrammingEngine(
    use_llm=True,
    tool="gemini",
    tool_config={
        "cli_command": "aichat",
        "model": "gemini-pro",
    }
)

result = engine.run(task, use_sandbox=True)
print(f"Status: {result.review.status}")
```

---

## 🔧 Tool Configuration

### LocalLLMTool (default)

```python
tool_config = {
    "runs_dir": "runs/ai_trader",  # Output directory
}

engine = SelfProgrammingEngine(
    tool="local",
    tool_config=tool_config
)
```

### GeminiCLITool

```python
tool_config = {
    "cli_command": "aichat",       # CLI binary
    "model": "gemini-pro",         # Model name
    "extra_args": ["--no-stream"], # Additional flags
}

engine = SelfProgrammingEngine(
    tool="gemini",
    tool_config=tool_config
)
```

### CodexCLITool

```python
tool_config = {
    "cli_command": "openai",
    "model": "gpt-4",
    "extra_args": [],
}

engine = SelfProgrammingEngine(
    tool="codex",
    tool_config=tool_config
)
```

### Custom Tool

```python
from meta_agent.self_programming.tools.cli_wrapper import GenericCLITool

# Create custom tool
my_tool = GenericCLITool(
    cli_command="my-llm-wrapper",
    name="my_custom_tool",
    extra_args=["--temperature", "0.7"]
)

# Use directly in ProgrammerAgent
from meta_agent.self_programming.agents.programmer import ProgrammerAgent

programmer = ProgrammerAgent(
    use_llm=True,
    tool="custom",
    tool_config={
        "cli_command": "my-llm-wrapper",
        "extra_args": ["--temperature", "0.7"]
    }
)
```

---

## 📊 Comparison

| Feature | Local | Gemini | Codex | Aider |
|---------|-------|--------|-------|-------|
| **Cost** | Free | $$ | $$$ | $$$ |
| **Speed** | Fast | Medium | Medium | Slow |
| **Quality** | Good | Excellent | Excellent | Excellent |
| **Privacy** | ✅ Local | ❌ Cloud | ❌ Cloud | ❌ Cloud |
| **Setup** | Easy | Easy | Medium | Easy |
| **Offline** | ✅ Yes | ❌ No | ❌ No | ❌ No |

---

## 🐛 Troubleshooting

### Issue: "CLI command not found"

```bash
# Check if tool is installed
which aichat
which openai

# Install if needed
pip install openai  # For OpenAI CLI
cargo install aichat  # For aichat
```

### Issue: "API key not set"

```bash
# Gemini
export GOOGLE_API_KEY="your-key"

# OpenAI
export OPENAI_API_KEY="your-key"

# Verify
echo $GOOGLE_API_KEY
```

### Issue: "CLI timeout"

```python
# Increase timeout in tool_config
tool_config = {
    "cli_command": "aichat",
    "model": "gemini-pro",
    # CLI wrapper uses default 120s timeout
    # Can't configure via tool_config yet - modify cli_wrapper.py
}
```

### Issue: "Output parsing failed"

The CLI tool must output in `<<<< SEARCH` / `====` / `>>>>` format.

Check output format:
```bash
echo "Add docstring to foo()" | aichat --model gemini-pro
```

If format is wrong, you may need to:
1. Add explicit format instructions to the prompt
2. Or implement a custom parser

---

## 🎯 Best Practices

### 1. Use Git Sandbox

Always use `--use-sandbox` with external tools:
```bash
python scripts/run_self_programming.py \
    --task "..." \
    --files ... \
    --use-llm \
    --use-sandbox  # ← Important!
```

### 2. Start Small

Test with simple tasks first:
```bash
# Good first test
--task "Add a docstring to foo() function"

# Not recommended for first test
--task "Completely refactor the entire codebase"
```

### 3. Check Whitelist

Ensure your files are whitelisted:
```python
from meta_agent.self_programming.integration import is_file_whitelisted

if is_file_whitelisted("demo.py"):
    print("✅ Safe to modify")
```

### 4. Monitor Costs

External APIs have costs:
- Gemini: ~$0.001 per 1K tokens
- GPT-4: ~$0.03 per 1K tokens

Use `--dry-run` first to estimate token usage.

---

## 📚 Further Reading

- [aichat Documentation](https://github.com/sigoden/aichat)
- [OpenAI CLI Docs](https://platform.openai.com/docs/api-reference)
- [Aider Docs](https://aider.chat/)
- [Self-Programming README](./README.md)

---

## ✨ Quick Reference

```bash
# Local (free, private)
python scripts/run_self_programming.py --use-llm --tool local

# Gemini (cloud, cheap)
python scripts/run_self_programming.py --use-llm --tool gemini --tool-cli aichat

# GPT-4 (cloud, expensive, best quality)
python scripts/run_self_programming.py --use-llm --tool codex --tool-model gpt-4

# Custom tool
python scripts/run_self_programming.py --use-llm --tool custom --tool-cli my-tool
```

---

**💡 Tip**: Start with `--tool local` (free), then upgrade to Gemini/GPT-4 for complex tasks.
