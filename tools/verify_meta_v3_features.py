#!/usr/bin/env python3
"""
Meta v3 Feature Verification Script

Verifies Meta v3.1 (Sleep Loop) and Meta v3.2 (Memory Retrieval) implementations.

Usage:
    PYTHONPATH=. venv/bin/python tools/verify_meta_v3_features.py
"""
import json
from pathlib import Path
from datetime import datetime


def check_meta_v31_sleep_loop():
    """Verify Meta v3.1: Enhanced Sleep Loop"""
    print("=" * 60)
    print("Meta v3.1: Sleep Loop Enhancement")
    print("=" * 60)

    # Check if sleep_loop.py has LLM summary capability
    sleep_loop_file = Path("scripts/ai_trader/memory/sleep_loop.py")

    if not sleep_loop_file.exists():
        print("❌ sleep_loop.py not found")
        return False

    content = sleep_loop_file.read_text()

    checks = {
        "LLM Summary Function": "_generate_daily_summary" in content,
        "Async Main": "async def main_async" in content,
        "ProviderPool Import": "from scripts.ai_trader.brain.provider_fallback import ProviderPool" in content,
        "Execution Trace Collection": "execution_trace" in content,
        "Daily Summaries Output": "daily_summaries.jsonl" in content,
        "Fallback Summary": "_fallback_summary" in content,
    }

    all_pass = all(checks.values())

    for check, passed in checks.items():
        status = "✅" if passed else "❌"
        print(f"{status} {check}")

    # Check if daily_summaries.jsonl exists (optional)
    summaries_file = Path("runs/memory/daily_summaries.jsonl")
    if summaries_file.exists():
        try:
            with summaries_file.open("r") as f:
                lines = f.readlines()
                count = len([l for l in lines if l.strip()])
                print(f"✅ Found {count} existing daily summaries")

                # Show latest
                if lines:
                    latest = json.loads(lines[-1])
                    print(f"   Latest: {latest.get('date', 'N/A')[:10]}")
                    print(f"   Summary: {latest.get('summary', 'N/A')[:100]}...")
        except Exception as e:
            print(f"⚠️  daily_summaries.jsonl exists but cannot be read: {e}")
    else:
        print(f"ℹ️  No daily summaries yet (run sleep_loop.py to generate)")

    print()
    return all_pass


def check_meta_v32_memory_retrieval():
    """Verify Meta v3.2: Memory Retrieval"""
    print("=" * 60)
    print("Meta v3.2: Memory Retrieval & Orchestrator Integration")
    print("=" * 60)

    # Check memory_retrieval.py
    retrieval_file = Path("libs/memory/memory_retrieval.py")

    if not retrieval_file.exists():
        print("❌ memory_retrieval.py not found")
        return False

    content = retrieval_file.read_text()

    retrieval_checks = {
        "MemoryRetriever Class": "class MemoryRetriever" in content,
        "get_recent_summaries": "def get_recent_summaries" in content,
        "search_by_keyword": "def search_by_keyword" in content,
        "get_lessons_learned": "def get_lessons_learned" in content,
        "get_recommendations": "def get_recommendations" in content,
        "format_context_for_llm": "def format_context_for_llm" in content,
    }

    for check, passed in retrieval_checks.items():
        status = "✅" if passed else "❌"
        print(f"{status} {check}")

    # Check ResearchAgent integration
    print()
    print("ResearchAgent Integration:")
    agent_file = Path("scripts/ai_trader/research/agent.py")

    if not agent_file.exists():
        print("❌ agent.py not found")
        return False

    agent_content = agent_file.read_text()

    agent_checks = {
        "_retrieve_memory_context Method": "def _retrieve_memory_context" in agent_content,
        "Memory Context Injection": "memory_context = self._retrieve_memory_context" in agent_content,
        "Payload includes memory_context": '"memory_context": memory_context' in agent_content,
        "Prompt mentions memory": "historical memory" in agent_content or "memory_context" in agent_content,
    }

    for check, passed in agent_checks.items():
        status = "✅" if passed else "❌"
        print(f"{status} {check}")

    # Test retrieval (if summaries exist)
    print()
    print("Functional Test:")
    summaries_file = Path("runs/memory/daily_summaries.jsonl")

    if summaries_file.exists():
        try:
            from libs.memory.memory_retrieval import get_retriever

            retriever = get_retriever()

            # Test get_recent_summaries
            summaries = retriever.get_recent_summaries(days=7)
            print(f"✅ get_recent_summaries(7): {len(summaries)} summaries")

            # Test get_lessons_learned
            lessons = retriever.get_lessons_learned(days=14)
            print(f"✅ get_lessons_learned(14): {len(lessons)} lessons")

            # Test format_context_for_llm
            context = retriever.format_context_for_llm(days=7)
            print(f"✅ format_context_for_llm(7): {len(context)} chars")

            # Show sample
            if lessons:
                print(f"   Sample Lesson: {lessons[0][:80]}...")

        except ImportError as e:
            print(f"⚠️  Cannot import memory_retrieval: {e}")
        except Exception as e:
            print(f"❌ Functional test failed: {e}")
    else:
        print(f"ℹ️  No summaries to test retrieval (run sleep_loop.py first)")

    print()
    return all(retrieval_checks.values()) and all(agent_checks.values())


def check_grafana_dashboard():
    """Verify Grafana dashboard exists"""
    print("=" * 60)
    print("P5.5: Grafana Dashboard")
    print("=" * 60)

    dashboard_file = Path("infra/grafana/dashboards/p3_p4_memory_evolution.json")

    if not dashboard_file.exists():
        print("❌ Grafana dashboard not found")
        return False

    try:
        with dashboard_file.open("r") as f:
            dashboard = json.load(f)

        panels = dashboard.get("dashboard", {}).get("panels", [])
        print(f"✅ Dashboard found with {len(panels)} panels")

        for panel in panels:
            title = panel.get("title", "Unknown")
            print(f"   - {title}")

        print()
        return True
    except Exception as e:
        print(f"❌ Failed to parse dashboard: {e}")
        return False


def check_prometheus_metrics():
    """Verify Prometheus metrics exporter"""
    print("=" * 60)
    print("P5.4: Prometheus Metrics Exporter")
    print("=" * 60)

    exporter_file = Path("tools/p3_p4_metrics_exporter.py")

    if not exporter_file.exists():
        print("❌ Metrics exporter not found")
        return False

    content = exporter_file.read_text()

    metrics = [
        ("memory_fabric_sts_records_total", "MEMORY_STS_GAUGE"),
        ("memory_fabric_mts_records_total", "MEMORY_MTS_GAUGE"),
        ("memory_fabric_lts_records_total", "MEMORY_LTS_GAUGE"),
        ("patch_approval_queue_total", "PATCH_QUEUE_GAUGE"),
        ("evolution_target_priority_score", "EVOLUTION_TARGET_PRIORITY"),
        ("patch_benefit_score", "PATCH_BENEFIT_SCORE"),
        ("patch_risk_score", "PATCH_RISK_SCORE"),
    ]

    print("Defined Metrics:")
    all_found = True
    for metric_name, variable_name in metrics:
        # Check for either the metric string or the variable name
        if metric_name in content or variable_name in content:
            print(f"✅ {metric_name}")
        else:
            print(f"❌ {metric_name}")
            all_found = False

    print()
    return all_found


def main():
    print()
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "Meta v3 Feature Verification" + " " * 20 + "║")
    print("╚" + "=" * 58 + "╝")
    print()

    results = {}

    # Check each component
    results["Meta v3.1"] = check_meta_v31_sleep_loop()
    results["Meta v3.2"] = check_meta_v32_memory_retrieval()
    results["P5.4"] = check_prometheus_metrics()
    results["P5.5"] = check_grafana_dashboard()

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    all_pass = all(results.values())

    for component, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} {component}")

    print()
    if all_pass:
        print("🎉 All Meta v3 features verified successfully!")
        return 0
    else:
        print("⚠️  Some features failed verification. Check output above.")
        return 1


if __name__ == "__main__":
    exit(main())
