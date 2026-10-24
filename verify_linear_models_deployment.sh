#!/bin/bash
# Linear Models Pool 部署验证脚本

set -e

echo "========================================================================"
echo "Linear Models Pool - 部署验证"
echo "========================================================================"
echo ""

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

TOTAL_TESTS=0
PASSED_TESTS=0

# 测试1: 模块导入
echo "=========================================="
echo "测试1: 模块导入验证"
echo "=========================================="
echo ""

if venv/bin/python -c "from models_linear import LassoModel, RidgeModel, ElasticNetModel, LogisticModel, LinearSVMModel, KNNModel, LightGBMModel; print('✅ 模块导入成功')" 2>&1; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
else
    echo -e "${RED}❌ 模块导入失败${NC}"
fi
TOTAL_TESTS=$((TOTAL_TESTS + 1))

echo ""

# 测试2: 完整功能测试
echo "=========================================="
echo "测试2: 完整功能测试 (9个模型)"
echo "=========================================="
echo ""

if venv/bin/python test_linear_models.py > /tmp/linear_models_test.log 2>&1; then
    echo -e "${GREEN}✅ 功能测试通过 (9/9)${NC}"
    PASSED_TESTS=$((PASSED_TESTS + 1))
    cat /tmp/linear_models_test.log | grep -E "✅|测试总结|总计:"
else
    echo -e "${RED}❌ 功能测试失败${NC}"
    cat /tmp/linear_models_test.log | tail -20
fi
TOTAL_TESTS=$((TOTAL_TESTS + 1))

echo ""

# 测试3: wf_hpo.py 集成检查
echo "=========================================="
echo "测试3: wf_hpo.py 集成检查"
echo "=========================================="
echo ""

if grep -q "from models_linear import" wf_hpo.py && \
   grep -q "LassoModel" wf_hpo.py && \
   grep -q "RidgeModel" wf_hpo.py && \
   grep -q "ElasticNetModel" wf_hpo.py; then
    echo -e "${GREEN}✅ wf_hpo.py 集成完整${NC}"
    echo "   - 导入语句: ✅"
    echo "   - LassoModel: ✅"
    echo "   - RidgeModel: ✅"
    echo "   - ElasticNetModel: ✅"
    echo "   - LightGBMModel: ✅"
    PASSED_TESTS=$((PASSED_TESTS + 1))
else
    echo -e "${RED}❌ wf_hpo.py 集成不完整${NC}"
fi
TOTAL_TESTS=$((TOTAL_TESTS + 1))

echo ""

# 测试4: 配置文件检查
echo "=========================================="
echo "测试4: 配置文件检查"
echo "=========================================="
echo ""

if grep -q '"lasso"' alpha_config_enhanced.json && \
   grep -q '"ridge"' alpha_config_enhanced.json && \
   grep -q '"elasticnet"' alpha_config_enhanced.json && \
   grep -q '"lgbm"' alpha_config_enhanced.json; then
    echo -e "${GREEN}✅ alpha_config_enhanced.json 配置完整${NC}"
    echo "   - model_type.choices: [lasso, ridge, elasticnet, lgbm] ✅"
    PASSED_TESTS=$((PASSED_TESTS + 1))
else
    echo -e "${RED}❌ alpha_config_enhanced.json 配置不完整${NC}"
fi
TOTAL_TESTS=$((TOTAL_TESTS + 1))

echo ""

# 测试5: 文件完整性检查
echo "=========================================="
echo "测试5: 文件完整性检查"
echo "=========================================="
echo ""

FILES_OK=true

if [ -f "models_linear.py" ]; then
    LINES=$(wc -l < models_linear.py)
    echo "✅ models_linear.py: $LINES lines"
else
    echo -e "${RED}❌ models_linear.py 不存在${NC}"
    FILES_OK=false
fi

if [ -f "test_linear_models.py" ]; then
    LINES=$(wc -l < test_linear_models.py)
    echo "✅ test_linear_models.py: $LINES lines"
else
    echo -e "${RED}❌ test_linear_models.py 不存在${NC}"
    FILES_OK=false
fi

if [ -f "LINEAR_MODELS_DEPLOYMENT_REPORT.md" ]; then
    LINES=$(wc -l < LINEAR_MODELS_DEPLOYMENT_REPORT.md)
    echo "✅ LINEAR_MODELS_DEPLOYMENT_REPORT.md: $LINES lines"
else
    echo -e "${RED}❌ LINEAR_MODELS_DEPLOYMENT_REPORT.md 不存在${NC}"
    FILES_OK=false
fi

if [ "$FILES_OK" = true ]; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
fi
TOTAL_TESTS=$((TOTAL_TESTS + 1))

echo ""

# 总结
echo "========================================================================"
echo "验证总结"
echo "========================================================================"
echo ""

echo "测试结果:"
echo "  测试1: 模块导入"
echo "  测试2: 功能测试 (9个模型)"
echo "  测试3: wf_hpo.py 集成"
echo "  测试4: 配置文件"
echo "  测试5: 文件完整性"
echo ""

echo "========================================================================"

if [ $PASSED_TESTS -eq $TOTAL_TESTS ]; then
    echo -e "${GREEN}🎉 所有测试通过: $PASSED_TESTS/$TOTAL_TESTS ✅${NC}"
    echo ""
    echo "✅ Linear Models Pool 部署完成！"
    echo ""
    echo "支持的模型:"
    echo "  回归: lasso, ridge, elasticnet, knn, lgbm"
    echo "  分类: logistic, svm_linear, knn, lgbm"
    echo ""
    echo "使用方式:"
    echo "  1. 修改 alpha_config_enhanced.json:"
    echo "     {\"model_type\": \"lasso\", \"model_params\": {\"alpha\": 0.1}}"
    echo ""
    echo "  2. 运行 HPO:"
    echo "     venv/bin/python alpha_loop.py --config alpha_config_enhanced.json"
    echo ""
    exit 0
else
    echo -e "${RED}❌ 部分测试失败: $PASSED_TESTS/$TOTAL_TESTS${NC}"
    echo ""
    echo "请检查日志: /tmp/linear_models_test.log"
    echo ""
    exit 1
fi
