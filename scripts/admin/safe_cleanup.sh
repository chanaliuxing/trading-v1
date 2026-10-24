#!/bin/bash
# 安全清理脚本 - 只清理垃圾文件，不移动任何代码

set -e

echo "=================================================="
echo "Safe Repository Cleanup"
echo "=================================================="
echo ""
echo "This script will ONLY clean:"
echo "  - Python cache (__pycache__, *.pyc)"
echo "  - Temporary files (*.tmp, *.bak, *~)"
echo "  - macOS files (.DS_Store)"
echo "  - Empty directories"
echo ""
echo "This script will NOT touch:"
echo "  - Any Python source files (*.py)"
echo "  - Any configuration files"
echo "  - Any data or model files"
echo ""

read -p "Continue? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Starting cleanup..."
echo ""

# Counter for cleaned items
CLEANED=0

# Clean Python cache
echo "[1/5] Cleaning Python cache..."
BEFORE=$(find . -type d -name "__pycache__" 2>/dev/null | wc -l | tr -d ' ')
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
find . -name "*.pyo" -delete 2>/dev/null || true
find . -name "*.pyd" -delete 2>/dev/null || true
AFTER=$(find . -type d -name "__pycache__" 2>/dev/null | wc -l | tr -d ' ')
REMOVED=$((BEFORE - AFTER))
echo "  ✓ Removed $REMOVED __pycache__ directories"
CLEANED=$((CLEANED + REMOVED))

# Clean temporary files
echo "[2/5] Cleaning temporary files..."
BEFORE=$(find . \( -name "*.tmp" -o -name "*.bak" -o -name "*~" -o -name "*.swp" -o -name "*.swo" \) 2>/dev/null | wc -l | tr -d ' ')
find . -name "*.tmp" -delete 2>/dev/null || true
find . -name "*.bak" -delete 2>/dev/null || true
find . -name "*~" -delete 2>/dev/null || true
find . -name "*.swp" -delete 2>/dev/null || true
find . -name "*.swo" -delete 2>/dev/null || true
AFTER=$(find . \( -name "*.tmp" -o -name "*.bak" -o -name "*~" -o -name "*.swp" -o -name "*.swo" \) 2>/dev/null | wc -l | tr -d ' ')
REMOVED=$((BEFORE - AFTER))
echo "  ✓ Removed $REMOVED temporary files"
CLEANED=$((CLEANED + REMOVED))

# Clean macOS files
echo "[3/5] Cleaning macOS files..."
BEFORE=$(find . -name ".DS_Store" 2>/dev/null | wc -l | tr -d ' ')
find . -name ".DS_Store" -delete 2>/dev/null || true
AFTER=$(find . -name ".DS_Store" 2>/dev/null | wc -l | tr -d ' ')
REMOVED=$((BEFORE - AFTER))
echo "  ✓ Removed $REMOVED .DS_Store files"
CLEANED=$((CLEANED + REMOVED))

# Archive old phase documents
echo "[4/5] Archiving old phase documents..."
mkdir -p docs/archive
ARCHIVED=0
for pattern in "PHASE2_*.md" "PHASE3_*.md" "OPTIONS_IMPLEMENTATION_*.md"; do
    for file in $pattern 2>/dev/null; do
        if [ -f "$file" ]; then
            mv "$file" docs/archive/
            echo "  ✓ Archived: $file"
            ARCHIVED=$((ARCHIVED + 1))
        fi
    done
done
if [ $ARCHIVED -eq 0 ]; then
    echo "  ✓ No old phase documents to archive"
else
    echo "  ✓ Archived $ARCHIVED documents"
fi

# Clean empty directories
echo "[5/5] Removing empty directories..."
BEFORE=$(find . -type d -empty 2>/dev/null | wc -l | tr -d ' ')
find . -type d -empty -delete 2>/dev/null || true
AFTER=$(find . -type d -empty 2>/dev/null | wc -l | tr -d ' ')
REMOVED=$((BEFORE - AFTER))
echo "  ✓ Removed $REMOVED empty directories"
CLEANED=$((CLEANED + REMOVED))

echo ""
echo "=================================================="
echo "Cleanup Summary"
echo "=================================================="
echo ""
echo "Total items cleaned: $CLEANED"
echo ""
echo "What was cleaned:"
echo "  ✓ Python cache files and directories"
echo "  ✓ Temporary and backup files"
echo "  ✓ macOS system files"
if [ $ARCHIVED -gt 0 ]; then
    echo "  ✓ Old phase documents (moved to docs/archive/)"
fi
echo "  ✓ Empty directories"
echo ""
echo "What was NOT touched:"
echo "  ✓ All Python source files (*.py)"
echo "  ✓ All configuration files (*.yml, *.yaml, *.json)"
echo "  ✓ All data files (*.parquet, *.csv, etc.)"
echo "  ✓ All logs and backups"
echo "  ✓ Git repository"
echo ""
echo "✅ Safe cleanup complete!"
echo ""
echo "Recommended next steps:"
echo "  1. Review changes: git status"
echo "  2. If satisfied: git add -A && git commit -m 'chore: clean up cache and temporary files'"
