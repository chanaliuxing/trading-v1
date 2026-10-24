#!/bin/bash
# 组织文档 - 移动非 CLI 使用的 MD 文档到 docs/

set -e

echo "=================================================="
echo "Documentation Organization Script"
echo "=================================================="
echo ""

# 保留在根目录的文档（CLI 工具使用）
KEEP_IN_ROOT=(
    "README.md"
    "CLAUDE.md"
    ".cursorrules"
    ".clinerules"
    "LICENSE"
    "LICENSE.md"
)

# 检查是否在正确的目录
if [ ! -f "CLAUDE.md" ]; then
    echo "❌ Error: Must run from trading repository root"
    exit 1
fi

echo "Files that will stay in root (used by CLI tools):"
for file in "${KEEP_IN_ROOT[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✓ $file"
    fi
done
echo ""

# 创建目标目录
mkdir -p docs/archive
mkdir -p docs/implementation
mkdir -p docs/deployment
mkdir -p docs/guides

echo "Target directories created:"
echo "  docs/archive/         - Old phase/status documents"
echo "  docs/implementation/  - Implementation reports"
echo "  docs/deployment/      - Deployment guides"
echo "  docs/guides/          - User guides"
echo ""

read -p "Continue with moving documents? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Moving documents..."
echo ""

MOVED=0

# 函数：检查文件是否应该保留在根目录
should_keep() {
    local file="$1"
    for keep in "${KEEP_IN_ROOT[@]}"; do
        if [ "$file" == "$keep" ]; then
            return 0
        fi
    done
    return 1
}

# 函数：安全移动文件
safe_move() {
    local src="$1"
    local dst_dir="$2"

    if [ -f "$src" ]; then
        if should_keep "$src"; then
            return
        fi

        mkdir -p "$dst_dir"
        git mv "$src" "$dst_dir/" 2>/dev/null || mv "$src" "$dst_dir/"
        echo "  ✓ $src → $dst_dir/"
        MOVED=$((MOVED + 1))
    fi
}

# 移动 Phase/Status 文档到 archive
echo "[1/4] Archiving phase and status documents..."
for file in PHASE*.md OPTIONS_*.md *_STATUS.md *_COMPLETE.md *_SUMMARY.md *_EVIDENCE.md; do
    if [ -f "$file" ]; then
        safe_move "$file" "docs/archive"
    fi
done

# 移动实现报告
echo "[2/4] Moving implementation reports..."
for file in *_IMPLEMENTATION*.md *_INTEGRATION*.md *_MIGRATION*.md *_REFACTOR*.md *_FIX*.md; do
    if [ -f "$file" ]; then
        safe_move "$file" "docs/implementation"
    fi
done

# 移动部署文档
echo "[3/4] Moving deployment documents..."
for file in *_DEPLOYMENT*.md *_QUICK_START.md *_GUIDE.md BACKTEST_GUIDE.md; do
    if [ -f "$file" ]; then
        safe_move "$file" "docs/deployment"
    fi
done

# 移动其他所有 MD 文件（除了保留列表）
echo "[4/4] Moving remaining documentation..."
for file in *.md; do
    if [ -f "$file" ] && ! should_keep "$file"; then
        # 根据文件名决定目标目录
        if [[ "$file" =~ (PLAN|ACTION|TODO|REPORT|INDEX) ]]; then
            safe_move "$file" "docs/archive"
        else
            safe_move "$file" "docs/guides"
        fi
    fi
done

echo ""
echo "=================================================="
echo "Organization Summary"
echo "=================================================="
echo ""
echo "Total documents moved: $MOVED"
echo ""
echo "Documents remaining in root:"
for file in "${KEEP_IN_ROOT[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✓ $file"
    fi
done
echo ""
echo "Documents organized in docs/:"
echo "  docs/archive/         - $(ls docs/archive/*.md 2>/dev/null | wc -l | tr -d ' ') files"
echo "  docs/implementation/  - $(ls docs/implementation/*.md 2>/dev/null | wc -l | tr -d ' ') files"
echo "  docs/deployment/      - $(ls docs/deployment/*.md 2>/dev/null | wc -l | tr -d ' ') files"
echo "  docs/guides/          - $(ls docs/guides/*.md 2>/dev/null | wc -l | tr -d ' ') files"
echo ""
echo "✅ Documentation organization complete!"
echo ""
echo "Next steps:"
echo "  1. Review changes: git status"
echo "  2. Check that CLI tools still work (CLAUDE.md, README.md are in root)"
echo "  3. Commit: git add -A && git commit -m 'docs: organize documentation into docs/ directory'"
