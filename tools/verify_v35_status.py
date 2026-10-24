#!/usr/bin/env python3
"""
V3.5 System Status Verification Tool

快速验证 Project Quentin v3.5 的配置和健康状态。

使用方法:
    PYTHONPATH=. venv/bin/python tools/verify_v35_status.py

输出:
    - 配置状态
    - 安全门控状态
    - 依赖检查
    - 数据健康
    - 建议和警告
"""

import sys
from pathlib import Path
import json
import yaml
from datetime import datetime

# ANSI 颜色代码
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"


def print_section(title):
    print(f"\n{BLUE}{'=' * 60}{RESET}")
    print(f"{BLUE}{title}{RESET}")
    print(f"{BLUE}{'=' * 60}{RESET}")


def print_status(label, status, message=""):
    status_icon = {
        "ok": f"{GREEN}✅{RESET}",
        "warn": f"{YELLOW}⚠️ {RESET}",
        "error": f"{RED}❌{RESET}",
        "info": f"{BLUE}ℹ️ {RESET}",
    }
    icon = status_icon.get(status, "  ")
    print(f"{icon} {label}: {message}")


def check_config_files():
    """检查配置文件是否存在"""
    print_section("1. 配置文件检查")

    config_yml = Path("config.yml")
    policy_json = Path("configs/research_policy.json")

    if config_yml.exists():
        print_status("config.yml", "ok", "存在")
    else:
        print_status("config.yml", "error", "不存在！")
        return None, None

    if policy_json.exists():
        print_status("research_policy.json", "ok", "存在")
    else:
        print_status("research_policy.json", "error", "不存在！")
        return None, None

    try:
        with open(config_yml) as f:
            config = yaml.safe_load(f)
        print_status("config.yml 解析", "ok", "有效 YAML")
    except Exception as e:
        print_status("config.yml 解析", "error", f"解析失败: {e}")
        return None, None

    try:
        with open(policy_json) as f:
            policy = json.load(f)
        print_status("research_policy.json 解析", "ok", "有效 JSON")
    except Exception as e:
        print_status("research_policy.json 解析", "error", f"解析失败: {e}")
        return None, None

    return config, policy


def check_autonomy_status(config, policy):
    """检查自主演化配置"""
    print_section("2. 自主演化状态")

    if not config or not policy:
        print_status("配置加载", "error", "配置文件加载失败")
        return

    # Global Kill Switch
    global_switch = config.get("features", {}).get("v3_autonomy", {}).get("enable_policy_self_application", False)
    if global_switch:
        print_status("Global Kill Switch", "warn", f"已启用 (enable_policy_self_application: {global_switch})")
        print(f"        {YELLOW}⚠️  系统处于自主模式！请确认这是测试环境{RESET}")
    else:
        print_status("Global Kill Switch", "ok", "已关闭 (安全模式)")

    # Policy-level permissions
    allow_code_evolution = policy.get("governance", {}).get("allow_code_evolution", False)
    allow_policy_updates = policy.get("governance", {}).get("allow_policy_updates", False)

    if allow_code_evolution:
        print_status("代码演化权限", "info", "已启用 (allow_code_evolution: true)")
    else:
        print_status("代码演化权限", "ok", "已禁用")

    if allow_policy_updates:
        print_status("策略自我修改", "error", "已启用 (allow_policy_updates: true)")
        print(f"        {RED}❌ 危险！系统可以修改策略文件{RESET}")
    else:
        print_status("策略自我修改", "ok", "已禁用")

    # Test coverage gate
    coverage_req = config.get("features", {}).get("v3_autonomy", {}).get("auto_merge_min_test_coverage_pct", 85)
    if coverage_req >= 90:
        print_status("测试覆盖率门槛", "ok", f"{coverage_req}% (严格)")
    elif coverage_req >= 80:
        print_status("测试覆盖率门槛", "info", f"{coverage_req}% (中等)")
    else:
        print_status("测试覆盖率门槛", "warn", f"{coverage_req}% (宽松)")

    # Rate limits
    max_patches = policy.get("governance", {}).get("max_patches_per_day", 0)
    if max_patches == 0:
        print_status("每日补丁限额", "ok", "0 (完全禁止)")
    elif max_patches <= 3:
        print_status("每日补丁限额", "ok", f"{max_patches} (严格)")
    else:
        print_status("每日补丁限额", "warn", f"{max_patches} (宽松)")


def check_module_protection(policy):
    """检查模块保护配置"""
    print_section("3. 模块保护")

    if not policy:
        return

    governance = policy.get("governance", {})
    allowed = governance.get("allowed_modules", [])
    forbidden = governance.get("forbidden_modules", [])

    print_status("允许模块数量", "info", f"{len(allowed)} 个")
    for module in allowed[:5]:  # 显示前5个
        print(f"        - {module}")
    if len(allowed) > 5:
        print(f"        ... 还有 {len(allowed) - 5} 个")

    print_status("禁止模块数量", "info", f"{len(forbidden)} 个")

    # 检查关键模块是否被保护
    critical_modules = ["kernel/", "risk/", "execution/", "libs/db/", "services/"]
    protected = [m for m in critical_modules if m in forbidden]

    if len(protected) == len(critical_modules):
        print_status("关键模块保护", "ok", f"所有 {len(critical_modules)} 个关键模块已保护")
    else:
        unprotected = [m for m in critical_modules if m not in forbidden]
        print_status("关键模块保护", "warn", f"{len(unprotected)} 个关键模块未保护: {unprotected}")


def check_dependencies():
    """检查依赖是否安装"""
    print_section("4. 依赖检查")

    deps = {
        "sentence_transformers": "BGE-M3 embeddings",
        "chromadb": "Vector store (ChromaDB)",
        "faiss": "Vector store (FAISS)",
        "yaml": "配置文件解析",
    }

    for package, description in deps.items():
        try:
            __import__(package)
            print_status(description, "ok", f"{package} 已安装")
        except ImportError:
            print_status(description, "error", f"{package} 未安装")


def check_data_health():
    """检查数据文件健康"""
    print_section("5. 数据健康")

    # 检查关键目录和文件
    paths = {
        "runs/memory/vector_store": "Vector store 目录",
        "runs/memory/skill_approval_queue.jsonl": "Skill 审批队列",
        "config/skill_registry.json": "Skill 注册表",
        "logs": "日志目录",
    }

    for path_str, description in paths.items():
        path = Path(path_str)
        if path.exists():
            if path.is_file():
                size = path.stat().st_size
                size_str = f"{size:,} bytes" if size < 1024 else f"{size/1024:.1f} KB"
                print_status(description, "ok", f"存在 ({size_str})")
            else:
                print_status(description, "ok", "存在")
        else:
            print_status(description, "warn", "不存在")

    # 检查待审批技能数量
    queue_file = Path("runs/memory/skill_approval_queue.jsonl")
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                lines = [line for line in f if line.strip()]
                pending_count = len([l for l in lines if '"status": "pending_approval"' in l])
                print_status("待审批技能", "info", f"{pending_count} 个")
        except Exception as e:
            print_status("待审批技能", "warn", f"无法读取: {e}")


def check_governance_logs():
    """检查最近的 governance 决策"""
    print_section("6. 最近的 Governance 决策")

    log_file = Path("logs/trading_system.log")
    if not log_file.exists():
        print_status("日志文件", "warn", "不存在")
        return

    try:
        # 读取最后 1000 行
        with open(log_file) as f:
            lines = f.readlines()[-1000:]

        # 查找 governance_summary
        governance_entries = [l for l in lines if "governance_summary" in l]

        if governance_entries:
            latest = governance_entries[-1]
            print_status("最近的 governance 记录", "ok", f"找到 {len(governance_entries)} 条记录")

            # 尝试提取关键信息
            if "blocked_experiments" in latest:
                print(f"        最新记录: {latest.strip()[:100]}...")
        else:
            print_status("Governance 记录", "info", "未找到 (可能还未运行过 research cycle)")

    except Exception as e:
        print_status("日志分析", "warn", f"无法分析: {e}")


def generate_recommendations(config, policy):
    """生成配置建议"""
    print_section("7. 配置建议")

    if not config or not policy:
        print_status("建议生成", "error", "配置加载失败，无法生成建议")
        return

    recommendations = []

    # 检查是否是生产配置
    global_switch = config.get("features", {}).get("v3_autonomy", {}).get("enable_policy_self_application", False)
    allow_policy_updates = policy.get("governance", {}).get("allow_policy_updates", False)

    if global_switch and allow_policy_updates:
        recommendations.append(("error", "❌ 危险配置：同时启用了自主演化和策略自我修改"))

    if global_switch:
        recommendations.append(("warn", "⚠️  自主模式已启用，确保这是测试环境"))

    if allow_policy_updates:
        recommendations.append(("error", "❌ 强烈建议禁用 allow_policy_updates"))

    coverage_req = config.get("features", {}).get("v3_autonomy", {}).get("auto_merge_min_test_coverage_pct", 85)
    if coverage_req < 85:
        recommendations.append(("warn", f"⚠️  测试覆盖率门槛较低 ({coverage_req}%)，建议 ≥85%"))

    max_patches = policy.get("governance", {}).get("max_patches_per_day", 0)
    if max_patches > 5:
        recommendations.append(("warn", f"⚠️  每日补丁限额较高 ({max_patches})，建议 ≤3"))

    # 检查是否有推荐配置
    if not recommendations:
        print_status("配置状态", "ok", "配置合理，未发现明显问题")
    else:
        for level, msg in recommendations:
            if level == "error":
                print(f"{RED}{msg}{RESET}")
            elif level == "warn":
                print(f"{YELLOW}{msg}{RESET}")

    # 给出模式建议
    print(f"\n{BLUE}推荐配置模式:{RESET}")
    if not global_switch and not allow_policy_updates:
        print(f"{GREEN}✅ 当前模式: 只读/Shadow 模式 (推荐新手){RESET}")
    elif global_switch and not allow_policy_updates:
        print(f"{YELLOW}⚠️  当前模式: 渐进式自主模式 (需谨慎监控){RESET}")
    else:
        print(f"{RED}❌ 当前模式: 危险配置 (不推荐){RESET}")


def print_summary():
    """打印总结"""
    print_section("8. 总结")

    print(f"""
{BLUE}快速测试命令:{RESET}
    # 测试 Governance
    PYTHONPATH=. venv/bin/pytest tests/unit/evolution/test_governance_v35.py -v

    # 运行 Research Cycle
    PYTHONPATH=. venv/bin/python scripts/ai_trader/run_research_cycle.py

    # 审批技能
    PYTHONPATH=. venv/bin/python scripts/admin/review_skills.py

{BLUE}更多文档:{RESET}
    - 用户指南: docs/V3_5_USER_GUIDE.md
    - 快速参考: docs/V3_5_QUICK_REFERENCE.md
    - 测试总结: docs/V3_5_QUICK_TEST_SUMMARY.md

{BLUE}紧急停止:{RESET}
    编辑 config.yml，设置:
    features.v3_autonomy.enable_policy_self_application: false
""")


def main():
    print(f"{BLUE}")
    print("=" * 60)
    print("Project Quentin v3.5 System Status Verification")
    print("=" * 60)
    print(f"{RESET}")
    print(f"检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 执行检查
    config, policy = check_config_files()
    check_autonomy_status(config, policy)
    check_module_protection(policy)
    check_dependencies()
    check_data_health()
    check_governance_logs()
    generate_recommendations(config, policy)
    print_summary()

    print(f"\n{GREEN}检查完成！{RESET}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}检查被中断{RESET}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{RED}检查失败: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
