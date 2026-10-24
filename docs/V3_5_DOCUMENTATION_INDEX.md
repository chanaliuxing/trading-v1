# V3.5 文档索引

**快速导航**: 快速找到你需要的文档

---

## 🚀 我想...

### 快速开始 (5 分钟)

**我想快速了解 v3.5 有什么功能**
→ 阅读: [V3_5_README.md](V3_5_README.md) (5分钟)

**我想立即测试 v3.5**
→ 运行: `PYTHONPATH=. venv/bin/python tools/verify_v35_status.py`

**我想知道常用命令**
→ 查看: [V3_5_QUICK_REFERENCE.md](V3_5_QUICK_REFERENCE.md) (速查卡片)

---

### 使用和配置 (30 分钟)

**我想学习如何使用 v3.5**
→ 阅读: [V3_5_USER_GUIDE.md](V3_5_USER_GUIDE.md) (完整指南)

**我想配置安全门控**
→ 参考: [V3_5_USER_GUIDE.md#配置指南](V3_5_USER_GUIDE.md#配置指南)

**我想知道如何审批技能**
→ 参考: [V3_5_USER_GUIDE.md#场景-b-审批和部署技能](V3_5_USER_GUIDE.md#场景-b-审批和部署技能)

---

### 测试和验证 (15 分钟)

**我想查看测试结果**
→ 阅读: [V3_5_QUICK_TEST_SUMMARY.md](V3_5_QUICK_TEST_SUMMARY.md)

**我想运行测试**
→ 参考: [V3_5_USER_GUIDE.md#测试验证](V3_5_USER_GUIDE.md#测试验证)

**我想知道测试是否通过**
→ 查看: [V3_5_QUICK_TEST_SUMMARY.md#测试统计](V3_5_QUICK_TEST_SUMMARY.md#测试统计) (48/48 PASSED)

---

### 故障排查 (10 分钟)

**我遇到了问题**
→ 查看: [V3_5_USER_GUIDE.md#故障排查](V3_5_USER_GUIDE.md#故障排查)

**我想检查系统状态**
→ 运行: `PYTHONPATH=. venv/bin/python tools/verify_v35_status.py`

**我想紧急停止系统**
→ 参考: [V3_5_QUICK_REFERENCE.md#紧急操作](V3_5_QUICK_REFERENCE.md#紧急操作)

---

### 深度学习 (1-2 小时)

**我想了解技术实现细节**
→ 阅读: [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md) (~4441 行代码)

**我想了解架构设计**
→ 参考: [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#架构概览](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#架构概览)

**我想了解 7 层安全门控**
→ 参考: [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#安全门控详解](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#安全门控详解)

---

## 📚 按文档类型分类

### 入门文档

| 文档 | 描述 | 阅读时间 |
|------|------|----------|
| [V3_5_README.md](V3_5_README.md) | 项目总览和快速开始 | 5 分钟 |
| [V3_5_QUICK_REFERENCE.md](V3_5_QUICK_REFERENCE.md) | 命令和配置速查卡片 | 5 分钟 |
| [V3_5_USER_GUIDE.md](V3_5_USER_GUIDE.md) | 完整使用和测试指南 | 30 分钟 |

### 测试文档

| 文档 | 描述 |
|------|------|
| [V3_5_QUICK_TEST_SUMMARY.md](V3_5_QUICK_TEST_SUMMARY.md) | 测试结果总结 (48/48 PASSED) |
| [V3_5_TEST_RESULTS.md](V3_5_TEST_RESULTS.md) | 详细测试报告 |

### 技术文档

| 文档 | 描述 |
|------|------|
| [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md) | 完整实现细节 |

### 工具

| 工具 | 描述 | 命令 |
|------|------|------|
| **状态检查工具** | 验证系统配置和健康 | `PYTHONPATH=. venv/bin/python tools/verify_v35_status.py` |

---

## 🎯 按角色分类

### 我是新手开发者

**推荐阅读顺序**:
1. [V3_5_README.md](V3_5_README.md) - 了解 v3.5 是什么
2. [V3_5_QUICK_REFERENCE.md](V3_5_QUICK_REFERENCE.md) - 熟悉常用命令
3. [V3_5_USER_GUIDE.md](V3_5_USER_GUIDE.md) - 学习如何使用
4. 运行: `tools/verify_v35_status.py` - 验证系统状态

---

### 我是测试工程师

**推荐阅读顺序**:
1. [V3_5_QUICK_TEST_SUMMARY.md](V3_5_QUICK_TEST_SUMMARY.md) - 查看测试结果
2. [V3_5_USER_GUIDE.md#测试验证](V3_5_USER_GUIDE.md#测试验证) - 学习如何运行测试
3. 运行测试套件:
   ```bash
   PYTHONPATH=. venv/bin/pytest tests/unit/evolution/ -v
   PYTHONPATH=. venv/bin/pytest tests/integration/test_p5_e2e_memory_evolution.py -v
   ```

---

### 我是系统管理员

**推荐阅读顺序**:
1. [V3_5_README.md#安全配置推荐](V3_5_README.md#安全配置推荐) - 了解安全配置
2. [V3_5_USER_GUIDE.md#配置指南](V3_5_USER_GUIDE.md#配置指南) - 学习配置选项
3. [V3_5_QUICK_REFERENCE.md#监控指标](V3_5_QUICK_REFERENCE.md#监控指标) - 设置监控
4. 运行: `tools/verify_v35_status.py` - 定期检查系统状态

---

### 我是架构师

**推荐阅读顺序**:
1. [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md) - 完整技术实现
2. [V3_5_README.md#项目目标达成情况](V3_5_README.md#项目目标达成情况) - 验证目标达成
3. 代码审查:
   - `libs/evolution/governance.py` - 安全门控实现
   - `libs/memory/embedding_service.py` - BGE-M3 集成
   - `scripts/ai_trader/research/agent.py` - Research Agent

---

## 🔍 按主题分类

### 安全与治理

- [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#安全门控详解](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#安全门控详解)
- [V3_5_USER_GUIDE.md#安全最佳实践](V3_5_USER_GUIDE.md#安全最佳实践)
- [V3_5_QUICK_REFERENCE.md#安全配置组合](V3_5_QUICK_REFERENCE.md#安全配置组合)

### 记忆系统

- [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#记忆升级-bge-m3](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#记忆升级-bge-m3)
- [V3_5_USER_GUIDE.md#场景-c-语义记忆检索](V3_5_USER_GUIDE.md#场景-c-语义记忆检索)
- [V3_5_QUICK_TEST_SUMMARY.md#bge-m3-memory-tests](V3_5_QUICK_TEST_SUMMARY.md#bge-m3-memory-tests)

### 技能合成

- [V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#技能合成流程](V3_5_AUTONOMY_WITH_SAFETY_IMPLEMENTATION.md#技能合成流程)
- [V3_5_USER_GUIDE.md#场景-b-审批和部署技能](V3_5_USER_GUIDE.md#场景-b-审批和部署技能)
- [V3_5_QUICK_TEST_SUMMARY.md#skill-synthesis-tests](V3_5_QUICK_TEST_SUMMARY.md#skill-synthesis-tests)

### 配置管理

- [V3_5_USER_GUIDE.md#配置指南](V3_5_USER_GUIDE.md#配置指南)
- [V3_5_QUICK_REFERENCE.md#配置速查表](V3_5_QUICK_REFERENCE.md#配置速查表)
- [V3_5_README.md#安全配置推荐](V3_5_README.md#安全配置推荐)

---

## 📞 获取帮助

**问题**: 我找不到我需要的信息

**解决**:
1. 搜索本文档索引
2. 查看 [V3_5_QUICK_REFERENCE.md](V3_5_QUICK_REFERENCE.md) (速查表)
3. 阅读 [V3_5_USER_GUIDE.md#故障排查](V3_5_USER_GUIDE.md#故障排查)
4. 运行状态检查: `tools/verify_v35_status.py`

---

**最后更新**: 2025-12-04
**文档版本**: 1.0
