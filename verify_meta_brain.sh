#!/bin/bash
# Meta大脑系统快速验证脚本
# 用法: ./verify_meta_brain.sh

set -e

echo "=========================================="
echo "Meta大脑系统快速验证"
echo "=========================================="
echo

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 成功/失败计数
SUCCESS=0
FAILED=0

check() {
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓${NC} $1"
        SUCCESS=$((SUCCESS + 1))
    else
        echo -e "${RED}✗${NC} $1"
        FAILED=$((FAILED + 1))
    fi
}

# 1. 检查E2E测试
echo "1. 运行E2E测试..."
python -m pytest tests/integration/test_meta_brain_e2e.py -q > /dev/null 2>&1
check "E2E测试通过"
echo

# 2. 检查Self-Programming
echo "2. 检查Self-Programming系统..."
python -c "from meta_agent.self_programming.engine import SelfProgrammingEngine; SelfProgrammingEngine()" > /dev/null 2>&1
check "SelfProgrammingEngine可导入"

python -m pytest tests/test_file_patcher.py -q > /dev/null 2>&1
check "FilePatcher测试通过"
echo

# 3. 检查Skill系统
echo "3. 检查Skill系统..."
python -c "from libs.skills.registry import SkillRegistry; SkillRegistry('config/skill_registry.json')" > /dev/null 2>&1
check "SkillRegistry可导入"

python -c "from libs.skills.runtime import SkillRuntime; SkillRuntime('config/skill_registry.json')" > /dev/null 2>&1
check "SkillRuntime可导入"

python -c "from libs.evolution.evolution_chamber import EvolutionChamber" > /dev/null 2>&1
check "EvolutionChamber可导入"
echo

# 4. 检查配置文件
echo "4. 检查配置文件..."
if [ -f "config/skill_registry.json" ]; then
    skill_count=$(python -c "import json; data=json.load(open('config/skill_registry.json')); print(len(data.get('skills', {})))" 2>/dev/null || echo "0")
    echo -e "${GREEN}✓${NC} skill_registry.json存在 (${skill_count}个技能)"
    SUCCESS=$((SUCCESS + 1))
else
    echo -e "${RED}✗${NC} skill_registry.json缺失"
    FAILED=$((FAILED + 1))
fi
echo

# 5. 检查关键脚本
echo "5. 检查关键脚本..."
scripts=(
    "scripts/ai_trader/auto_loop.py"
    "scripts/ai_trader/evolvers/skill_approver.py"
    "scripts/ai_trader/memory/sleep_loop.py"
    "scripts/ai_trader/monitoring_dashboard.py"
)

for script in "${scripts[@]}"; do
    if [ -f "$script" ]; then
        echo -e "${GREEN}✓${NC} $(basename $script)"
        SUCCESS=$((SUCCESS + 1))
    else
        echo -e "${RED}✗${NC} $(basename $script) 缺失"
        FAILED=$((FAILED + 1))
    fi
done
echo

# 6. 总结
echo "=========================================="
echo "验证结果"
echo "=========================================="
echo -e "${GREEN}成功: $SUCCESS${NC}"
echo -e "${RED}失败: $FAILED${NC}"
echo

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ 所有检查通过！系统就绪。${NC}"
    echo
    echo "下一步:"
    echo "1. 运行E2E测试: python -m pytest tests/integration/test_meta_brain_e2e.py -v -s"
    echo "2. 启动主循环: python scripts/ai_trader/auto_loop.py"
    echo "3. 启动仪表盘: streamlit run scripts/ai_trader/monitoring_dashboard.py"
    echo
    echo "详细文档: ai/META_BRAIN_TEST_RESULTS.md"
    exit 0
else
    echo -e "${YELLOW}⚠️  有 $FAILED 项检查失败${NC}"
    echo "请查看上述输出并修复问题"
    exit 1
fi
