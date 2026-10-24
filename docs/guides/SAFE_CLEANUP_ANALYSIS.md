# 安全的仓库清理分析

**Date:** 2025-12-11
**Status:** ⚠️ 需要谨慎 - 很多根目录文件是系统必需的

---

## 重要发现

### ❌ 不能随意移动的文件

经过分析，发现以下问题：

1. **config.yml 直接引用根目录脚本**
   ```yaml
   alpha_loop: venv/bin/python alpha_loop.py ...
   apply_params: venv/bin/python apply_extras_params.py ...
   ```
   如果移动这些文件，config.yml 中的所有命令都会失效！

2. **某些脚本被当作模块导入**
   - `alpha_loop.py` 被 `test_hpo_fix_complete.py` 导入
   - 移动会破坏导入路径

3. **PYTHONPATH 依赖**
   - 很多脚本假设在项目根目录运行
   - 移动后相对导入会失败

---

## 正确的清理策略

### ✅ 安全的操作（只影响 .gitignore）

**可以安全执行：**
1. 更新 .gitignore（已完成 ✅）
2. 清理真正的临时文件
3. 删除明显的垃圾文件

**不移动任何 .py 文件！**

---

## 实际应该清理的内容

### 1. .gitignore 优化（已完成 ✅）

已添加规则排除：
- `*.zip`, `*.tar.gz`, `*.pth`, `*.pkl`, `*.parquet`
- `*.sql`, `*.backup`, `*.dump`
- `models/`, `checkpoints/`, `data/raw/`, `data/processed/`

### 2. 安全删除的文件类型

**可以安全删除（如果存在）：**
```bash
# 临时文件
*.tmp
*.bak
*.swp
*.swo
*~
.DS_Store

# Python 缓存
__pycache__/
*.pyc
*.pyo
*.pyd

# 日志文件（已在 logs/ 目录，受 .gitignore 保护）
# 不需要额外操作

# 备份文件（已在 backup/ 目录，受 .gitignore 保护）
# 不需要额外操作
```

### 3. 文档整理（安全）

**可以移动的非代码文件：**
- `*.md` 文档可以组织到 `docs/` 目录
- 但要保留根目录的 `README.md`, `CLAUDE.md`

---

## 为什么不能移动 Python 文件

### 问题 1: config.yml 硬编码路径

```yaml
# config.yml 中的命令
commands:
  alpha_loop: venv/bin/python alpha_loop.py ...
  apply_params: venv/bin/python apply_extras_params.py ...
```

如果移动 `alpha_loop.py` → `scripts/ml/alpha_loop.py`，则需要：
1. 更新 config.yml
2. 更新 ai_trader.yaml
3. 更新所有文档
4. 测试所有引用路径

**风险：** 高 - 可能破坏生产系统

### 问题 2: 相对导入

很多根目录脚本使用相对导入：
```python
from libs.config import config
from libs.db.clickhouse import ClickHouseDB
```

这些导入假设脚本在项目根目录。移动后会失败。

### 问题 3: 模块导入

```python
# test_hpo_fix_complete.py
from alpha_loop import _infer_space_from_grid
```

如果移动 `alpha_loop.py`，这个导入会失败。

---

## 推荐的安全清理方案

### Phase 1: .gitignore 优化（✅ 已完成）

```gitignore
# 已添加的规则
*.zip
*.tar.gz
*.pth
*.pkl
*.parquet
*.sql
*.backup
models/
checkpoints/
data/raw/
data/processed/
```

### Phase 2: 清理明显的垃圾文件

**安全删除（如果存在）：**
```bash
# 查找临时文件
find . -name "*.tmp" -o -name "*.bak" -o -name "*~" -o -name ".DS_Store"

# 查找空目录
find . -type d -empty

# 清理 Python 缓存
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete
find . -name "*.pyo" -delete
```

### Phase 3: 文档整理（低风险）

**可以安全移动的文档：**
```bash
# 创建 docs/archive/ 存放旧文档
mkdir -p docs/archive

# 移动明显过时的文档（需要人工判断）
# 例如：PHASE2_*.md, PHASE3_*.md 等完成状态文档
mv PHASE2_*.md docs/archive/ 2>/dev/null || true
mv PHASE3_*.md docs/archive/ 2>/dev/null || true
mv OPTIONS_*.md docs/archive/ 2>/dev/null || true
```

### Phase 4: Git 清理（仅在必要时）

**如果有大文件意外提交到 git：**
```bash
# 查找 git 仓库中的大文件
git rev-list --objects --all | \
  git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | \
  awk '/^blob/ {print substr($0,6)}' | \
  sort --numeric-sort --key=2 | \
  tail -n 20

# 如果发现大文件，使用 git-filter-repo 清理（慎用！）
# pip install git-filter-repo
# git filter-repo --path-glob '*.pth' --invert-paths
```

---

## 正确的仓库组织（未来）

### 长期目标：渐进式重构

**不要一次性移动所有文件！而是：**

1. **新功能使用新结构**
   - 新脚本直接放到 `scripts/` 子目录
   - 新工具放到 `tools/`

2. **逐步迁移旧脚本**
   - 选择一个低风险的脚本
   - 移动 + 更新所有引用
   - 测试完整后再移动下一个

3. **使用符号链接过渡**
   ```bash
   # 移动文件
   mv alpha_loop.py scripts/ml/

   # 创建兼容性符号链接
   ln -s scripts/ml/alpha_loop.py alpha_loop.py

   # 更新引用后，删除符号链接
   ```

---

## 安全清理脚本（替代版）

**创建 `scripts/admin/safe_cleanup.sh`：**

```bash
#!/bin/bash
# 安全清理脚本 - 只清理真正的垃圾文件，不移动代码

set -e

echo "Safe Repository Cleanup"
echo "======================"
echo ""

# 清理 Python 缓存
echo "Cleaning Python cache..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
find . -name "*.pyo" -delete 2>/dev/null || true
echo "✓ Python cache cleaned"

# 清理临时文件
echo "Cleaning temporary files..."
find . -name "*.tmp" -delete 2>/dev/null || true
find . -name "*.bak" -delete 2>/dev/null || true
find . -name "*~" -delete 2>/dev/null || true
find . -name ".DS_Store" -delete 2>/dev/null || true
echo "✓ Temporary files cleaned"

# 移动明显过时的文档到 archive
echo "Archiving old phase documents..."
mkdir -p docs/archive
mv PHASE2_*.md docs/archive/ 2>/dev/null || true
mv PHASE3_*.md docs/archive/ 2>/dev/null || true
echo "✓ Old documents archived"

# 清理空目录
echo "Removing empty directories..."
find . -type d -empty -delete 2>/dev/null || true
echo "✓ Empty directories removed"

echo ""
echo "Safe cleanup complete!"
echo ""
echo "What was cleaned:"
echo "  - Python cache (__pycache__, *.pyc)"
echo "  - Temporary files (*.tmp, *.bak, *~, .DS_Store)"
echo "  - Old phase documents (moved to docs/archive/)"
echo "  - Empty directories"
echo ""
echo "What was NOT touched:"
echo "  - All Python source files (*.py)"
echo "  - All configuration files"
echo "  - All data files"
echo "  - Git repository"
```

---

## 总结

### ✅ 已安全完成
1. .gitignore 更新（防止未来提交大文件）

### ⏭️ 可以安全执行
1. 清理 Python 缓存
2. 删除临时文件
3. 归档旧文档

### ❌ 不要执行
1. ~~移动根目录的 Python 脚本~~（会破坏系统）
2. ~~重命名核心文件~~（会破坏引用）
3. ~~大规模重构目录结构~~（风险太高）

### 📋 正确的做法

**立即可做：**
- 使用 `safe_cleanup.sh` 清理垃圾文件
- 保持 .gitignore 更新

**长期规划：**
- 新代码使用规范的目录结构
- 渐进式迁移旧代码（一次一个文件）
- 充分测试每次迁移

---

**教训：** 不要盲目移动代码文件，要先分析依赖关系！

**状态：** ⚠️ 之前的清理脚本已作废，请使用安全版本
