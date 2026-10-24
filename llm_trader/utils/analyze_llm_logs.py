#!/usr/bin/env python3
"""LLM Log Analysis Tool

Analyze LLM request/response logs to understand:
- Performance metrics (latency, success rate)
- Decision patterns (BUY/SELL/HOLD distribution)
- Cost analysis (estimated API costs)
- Error patterns
- Provider comparison

Usage:
    # Analyze today's logs
    python analyze_llm_logs.py

    # Analyze specific date
    python analyze_llm_logs.py --date 2025-10-21

    # Analyze date range
    python analyze_llm_logs.py --from 2025-10-20 --to 2025-10-21

    # Export to CSV
    python analyze_llm_logs.py --export results.csv

    # Show detailed stats
    python analyze_llm_logs.py --verbose
"""

import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict, Counter
import statistics


class LLMLogAnalyzer:
    """Analyze LLM interaction logs"""

    # Estimated API costs per 1K tokens (approximate, update as needed)
    COSTS_PER_1K = {
        "gpt-4o": {"input": 0.005, "output": 0.015},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "gemini-2.0-flash-exp": {"input": 0.0001, "output": 0.0003},
        "deepseek-chat": {"input": 0.00014, "output": 0.00028},
        "deepseek-r1": {"input": 0.00055, "output": 0.0019},
    }

    def __init__(self, log_dir: str = "logs/llm"):
        self.log_dir = Path(log_dir)
        self.requests: List[Dict] = []
        self.responses: List[Dict] = []
        self.interactions: List[Dict] = []

    def load_combined_log(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None):
        """Load combined JSONL log"""
        combined_file = self.log_dir / "llm_interactions.jsonl"

        if not combined_file.exists():
            print(f"No combined log found at {combined_file}")
            return

        with open(combined_file) as f:
            for line in f:
                if not line.strip():
                    continue

                entry = json.loads(line)
                timestamp = datetime.fromisoformat(entry["timestamp"])

                # Filter by date range
                if start_date and timestamp < start_date:
                    continue
                if end_date and timestamp > end_date:
                    continue

                if entry["type"] == "request":
                    self.requests.append(entry)
                elif entry["type"] == "response":
                    self.responses.append(entry)

        # Match requests with responses
        response_by_id = {r["request_id"]: r for r in self.responses}
        for req in self.requests:
            req_id = req["request_id"]
            if req_id in response_by_id:
                resp = response_by_id[req_id]
                self.interactions.append({"request": req, "response": resp})

        print(f"Loaded {len(self.requests)} requests, {len(self.responses)} responses")
        print(f"Matched {len(self.interactions)} complete interactions")

    def load_daily_logs(self, date: datetime):
        """Load logs for a specific date"""
        date_str = date.strftime("%Y-%m-%d")
        daily_dir = self.log_dir / date_str

        if not daily_dir.exists():
            print(f"No logs found for {date_str}")
            return

        # Load requests
        requests_dir = daily_dir / "requests"
        if requests_dir.exists():
            for file in requests_dir.glob("*.json"):
                with open(file) as f:
                    self.requests.append(json.load(f))

        # Load responses
        responses_dir = daily_dir / "responses"
        if responses_dir.exists():
            for file in responses_dir.glob("*.json"):
                with open(file) as f:
                    self.responses.append(json.load(f))

        # Match
        response_by_id = {r["request_id"]: r for r in self.responses}
        for req in self.requests:
            req_id = req["request_id"]
            if req_id in response_by_id:
                resp = response_by_id[req_id]
                self.interactions.append({"request": req, "response": resp})

        print(f"Loaded {len(self.requests)} requests, {len(self.responses)} responses for {date_str}")

    def analyze_performance(self) -> Dict[str, Any]:
        """Analyze performance metrics"""
        if not self.interactions:
            return {}

        latencies = [i["response"]["latency_ms"] for i in self.interactions]
        success_count = sum(1 for i in self.interactions if i["response"]["success"])

        # Group by provider
        by_provider = defaultdict(list)
        for interaction in self.interactions:
            provider = interaction["request"]["provider"]
            by_provider[provider].append(interaction["response"]["latency_ms"])

        provider_stats = {}
        for provider, lats in by_provider.items():
            provider_stats[provider] = {
                "count": len(lats),
                "avg_latency_ms": statistics.mean(lats),
                "median_latency_ms": statistics.median(lats),
                "p95_latency_ms": statistics.quantiles(lats, n=20)[18] if len(lats) > 1 else lats[0],
                "min_latency_ms": min(lats),
                "max_latency_ms": max(lats),
            }

        return {
            "total_calls": len(self.interactions),
            "success_count": success_count,
            "failure_count": len(self.interactions) - success_count,
            "success_rate": success_count / len(self.interactions),
            "avg_latency_ms": statistics.mean(latencies),
            "median_latency_ms": statistics.median(latencies),
            "p95_latency_ms": statistics.quantiles(latencies, n=20)[18] if len(latencies) > 1 else latencies[0],
            "min_latency_ms": min(latencies),
            "max_latency_ms": max(latencies),
            "by_provider": provider_stats,
        }

    def analyze_decisions(self) -> Dict[str, Any]:
        """Analyze trading decisions"""
        decisions = []
        confidences = []
        by_provider = defaultdict(lambda: {"BUY": 0, "SELL": 0, "HOLD": 0})
        by_symbol = defaultdict(lambda: {"BUY": 0, "SELL": 0, "HOLD": 0})

        for interaction in self.interactions:
            if not interaction["response"]["success"]:
                continue

            resp_data = interaction["response"]["response_data"]
            decision = resp_data.get("decision", "HOLD")
            confidence = resp_data.get("confidence", 0)
            provider = interaction["request"]["provider"]
            symbol = interaction["request"]["symbol"]

            decisions.append(decision)
            if confidence:
                confidences.append(confidence)

            by_provider[provider][decision] += 1
            by_symbol[symbol][decision] += 1

        decision_counts = Counter(decisions)

        return {
            "total_decisions": len(decisions),
            "decision_distribution": dict(decision_counts),
            "buy_rate": decision_counts.get("BUY", 0) / len(decisions) if decisions else 0,
            "sell_rate": decision_counts.get("SELL", 0) / len(decisions) if decisions else 0,
            "hold_rate": decision_counts.get("HOLD", 0) / len(decisions) if decisions else 0,
            "avg_confidence": statistics.mean(confidences) if confidences else 0,
            "median_confidence": statistics.median(confidences) if confidences else 0,
            "by_provider": dict(by_provider),
            "by_symbol": dict(by_symbol),
        }

    def analyze_errors(self) -> Dict[str, Any]:
        """Analyze error patterns"""
        errors = []
        error_by_provider = defaultdict(int)
        error_by_type = defaultdict(int)

        for interaction in self.interactions:
            if not interaction["response"]["success"]:
                error_msg = interaction["response"].get("error", "Unknown error")
                provider = interaction["request"]["provider"]

                errors.append({"provider": provider, "error": error_msg, "timestamp": interaction["response"]["timestamp"]})

                error_by_provider[provider] += 1

                # Categorize error
                if "timeout" in error_msg.lower():
                    error_by_type["timeout"] += 1
                elif "rate" in error_msg.lower() or "limit" in error_msg.lower():
                    error_by_type["rate_limit"] += 1
                elif "auth" in error_msg.lower() or "api key" in error_msg.lower():
                    error_by_type["authentication"] += 1
                elif "json" in error_msg.lower() or "parse" in error_msg.lower():
                    error_by_type["parsing"] += 1
                else:
                    error_by_type["other"] += 1

        return {
            "total_errors": len(errors),
            "error_rate": len(errors) / len(self.interactions) if self.interactions else 0,
            "errors_by_provider": dict(error_by_provider),
            "errors_by_type": dict(error_by_type),
            "recent_errors": errors[-10:] if len(errors) > 10 else errors,
        }

    def estimate_costs(self) -> Dict[str, Any]:
        """Estimate API costs (rough approximation)"""
        costs_by_provider = defaultdict(float)
        total_cost = 0

        for interaction in self.interactions:
            provider = interaction["request"]["provider"]
            model = interaction["request"]["model"]

            # Estimate tokens (very rough: ~4 chars per token)
            req_data = str(interaction["request"]["request_data"])
            resp_data = str(interaction["response"]["response_data"])

            input_tokens = len(req_data) / 4
            output_tokens = len(resp_data) / 4

            # Get cost per 1K tokens
            if model in self.COSTS_PER_1K:
                cost_info = self.COSTS_PER_1K[model]
                call_cost = (input_tokens / 1000 * cost_info["input"]) + (output_tokens / 1000 * cost_info["output"])
                costs_by_provider[provider] += call_cost
                total_cost += call_cost

        return {
            "total_estimated_cost": total_cost,
            "costs_by_provider": dict(costs_by_provider),
            "note": "Costs are rough estimates based on ~4 chars/token. Actual costs may vary.",
        }

    def generate_report(self, verbose: bool = False) -> str:
        """Generate analysis report"""
        perf = self.analyze_performance()
        decisions = self.analyze_decisions()
        errors = self.analyze_errors()
        costs = self.estimate_costs()

        report = []
        report.append("=" * 80)
        report.append("LLM LOG ANALYSIS REPORT")
        report.append("=" * 80)
        report.append("")

        # Performance
        report.append("PERFORMANCE METRICS")
        report.append("-" * 80)
        if perf:
            report.append(f"Total API Calls: {perf['total_calls']}")
            report.append(f"Success Rate: {perf['success_rate']:.2%}")
            report.append(f"Avg Latency: {perf['avg_latency_ms']:.1f}ms")
            report.append(f"Median Latency: {perf['median_latency_ms']:.1f}ms")
            report.append(f"P95 Latency: {perf['p95_latency_ms']:.1f}ms")
            report.append(f"Min/Max Latency: {perf['min_latency_ms']:.1f}ms / {perf['max_latency_ms']:.1f}ms")
            report.append("")

            report.append("By Provider:")
            for provider, stats in perf["by_provider"].items():
                report.append(f"  {provider}:")
                report.append(f"    Calls: {stats['count']}")
                report.append(f"    Avg Latency: {stats['avg_latency_ms']:.1f}ms")
                report.append(f"    P95 Latency: {stats['p95_latency_ms']:.1f}ms")
        report.append("")

        # Decisions
        report.append("TRADING DECISIONS")
        report.append("-" * 80)
        if decisions:
            report.append(f"Total Decisions: {decisions['total_decisions']}")
            report.append(f"BUY Rate: {decisions['buy_rate']:.2%}")
            report.append(f"SELL Rate: {decisions['sell_rate']:.2%}")
            report.append(f"HOLD Rate: {decisions['hold_rate']:.2%}")
            report.append(f"Avg Confidence: {decisions['avg_confidence']:.3f}")
            report.append("")

            if verbose:
                report.append("By Provider:")
                for provider, dist in decisions["by_provider"].items():
                    report.append(f"  {provider}: BUY={dist['BUY']}, SELL={dist['SELL']}, HOLD={dist['HOLD']}")
                report.append("")

                report.append("By Symbol:")
                for symbol, dist in sorted(decisions["by_symbol"].items()):
                    report.append(f"  {symbol}: BUY={dist['BUY']}, SELL={dist['SELL']}, HOLD={dist['HOLD']}")
        report.append("")

        # Errors
        report.append("ERROR ANALYSIS")
        report.append("-" * 80)
        if errors:
            report.append(f"Total Errors: {errors['total_errors']}")
            report.append(f"Error Rate: {errors['error_rate']:.2%}")
            report.append("")

            if errors["errors_by_provider"]:
                report.append("Errors by Provider:")
                for provider, count in errors["errors_by_provider"].items():
                    report.append(f"  {provider}: {count}")
                report.append("")

            if errors["errors_by_type"]:
                report.append("Errors by Type:")
                for error_type, count in errors["errors_by_type"].items():
                    report.append(f"  {error_type}: {count}")
        report.append("")

        # Costs
        report.append("COST ESTIMATION")
        report.append("-" * 80)
        if costs:
            report.append(f"Total Estimated Cost: ${costs['total_estimated_cost']:.4f}")
            report.append("")
            report.append("By Provider:")
            for provider, cost in costs["costs_by_provider"].items():
                report.append(f"  {provider}: ${cost:.4f}")
            report.append("")
            report.append(f"Note: {costs['note']}")
        report.append("")

        report.append("=" * 80)

        return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(description="Analyze LLM interaction logs")
    parser.add_argument("--date", type=str, help="Specific date to analyze (YYYY-MM-DD)")
    parser.add_argument("--from", dest="from_date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--log-dir", type=str, default="logs/llm", help="Log directory path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed stats")
    parser.add_argument("--export", type=str, help="Export to CSV file")

    args = parser.parse_args()

    analyzer = LLMLogAnalyzer(log_dir=args.log_dir)

    # Determine date range
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
        analyzer.load_daily_logs(target_date)
    elif args.from_date or args.to_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d") if args.from_date else None
        end = datetime.strptime(args.to_date, "%Y-%m-%d") if args.to_date else None
        analyzer.load_combined_log(start_date=start, end_date=end)
    else:
        # Default: today
        today = datetime.now()
        analyzer.load_daily_logs(today)

    # Generate and print report
    report = analyzer.generate_report(verbose=args.verbose)
    print(report)

    # Export if requested
    if args.export and analyzer.interactions:
        print(f"\nExporting to {args.export}...")
        import csv

        with open(args.export, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "timestamp",
                    "provider",
                    "model",
                    "symbol",
                    "decision",
                    "quantity",
                    "confidence",
                    "latency_ms",
                    "success",
                ]
            )

            for interaction in analyzer.interactions:
                req = interaction["request"]
                resp = interaction["response"]
                resp_data = resp.get("response_data", {})

                writer.writerow(
                    [
                        resp["timestamp"],
                        req["provider"],
                        req["model"],
                        req["symbol"],
                        resp_data.get("decision", ""),
                        resp_data.get("quantity", ""),
                        resp_data.get("confidence", ""),
                        resp["latency_ms"],
                        resp["success"],
                    ]
                )
        print(f"✓ Exported {len(analyzer.interactions)} interactions to {args.export}")


if __name__ == "__main__":
    main()
