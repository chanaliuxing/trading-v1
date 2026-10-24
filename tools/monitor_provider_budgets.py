#!/usr/bin/env python3
"""
Monitor Provider Budgets

Display current budget usage, costs, and limits for all LLM providers.
"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from libs.provider_budget import get_all_budgets, get_daily_costs
from tabulate import tabulate


def main():
    print("=" * 80)
    print("Provider Budget Monitor")
    print("=" * 80)
    print()

    # Get all budgets
    budgets = get_all_budgets()

    if not budgets:
        print("⚠️  No budget data available (Redis may be unavailable)")
        return

    # Prepare table data
    table = []
    for provider, budget in budgets.items():
        if budget.get('degraded'):
            status = "⚠️  DEGRADED"
        elif budget['used'] >= budget['limit']:
            status = "🔴 EXCEEDED"
        elif budget['remaining'] < budget['limit'] * 0.2:  # < 20% remaining
            status = "🟡 LOW"
        else:
            status = "✅ OK"

        usage_pct = (budget['used'] / budget['limit'] * 100) if budget['limit'] > 0 else 0

        table.append([
            provider,
            budget['used'],
            budget['limit'],
            budget['remaining'],
            f"{usage_pct:.1f}%",
            status
        ])

    # Sort by usage percentage descending
    table.sort(key=lambda x: float(x[4].rstrip('%')), reverse=True)

    headers = ["Provider", "Used", "Limit", "Remaining", "Usage %", "Status"]
    print(tabulate(table, headers=headers, tablefmt="grid"))
    print()

    # Display costs
    print("Daily Costs:")
    print("-" * 80)
    costs = get_daily_costs()

    if 'error' in costs:
        print(f"⚠️  Cost calculation failed: {costs['error']}")
    else:
        cost_table = []
        for provider, cost in costs.items():
            if provider == 'total':
                continue
            cost_table.append([provider, f"${cost:.2f}"])

        print(tabulate(cost_table, headers=["Provider", "Cost"], tablefmt="simple"))
        print()
        print(f"{'Total Daily Cost:':<30} ${costs.get('total', 0):.2f}")

    print()
    print("=" * 80)

    # Check for any exceeded budgets
    exceeded = [p for p, b in budgets.items() if b['used'] >= b['limit']]
    if exceeded:
        print()
        print("⚠️  WARNING: The following providers have exceeded their daily budget:")
        for provider in exceeded:
            budget = budgets[provider]
            print(f"  - {provider}: {budget['used']}/{budget['limit']} calls")
            print(f"    Budget resets at: {budget.get('resets_at', 'unknown')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
