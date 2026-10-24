#!/usr/bin/env python3
"""
IC to Sharpe Correlation Report

Analyzes the relationship between Model Layer (IC) and Strategy Layer (Sharpe)
metrics from historical ML training runs.

Usage:
    python tools/report_ic_to_sharpe.py --window-days 30 --min-samples 5
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any
import pandas as pd
import numpy as np
from scipy.stats import pearsonr, spearmanr
import structlog

logger = structlog.get_logger()


def load_ml_success_logs(days: int = 30) -> List[Dict[str, Any]]:
    """
    Load ML success logs from the last N days.

    Returns:
        List of dicts with job_id, job_type, metrics, timestamp
    """
    runs_base = Path("runs/ai_trader")
    if not runs_base.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    records = []

    # Scan date directories
    for date_dir in runs_base.glob("*"):
        if not date_dir.is_dir():
            continue

        # Parse date from dirname (YYYY-MM-DD format)
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
            if dir_date < cutoff:
                continue
        except ValueError:
            continue

        # Load ml_success.jsonl
        success_file = date_dir / "ml_success.jsonl"
        if not success_file.exists():
            continue

        try:
            with open(success_file, "r") as f:
                for line in f:
                    if line.strip():
                        record = json.loads(line)
                        records.append(record)
        except Exception as e:
            logger.warning("failed_to_load_success_log", path=str(success_file), error=str(e))

    return records


def extract_ic_sharpe_pairs(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Extract IC and Sharpe pairs from ML success records.

    Returns:
        DataFrame with columns: [job_id, job_type, date, ic, ir, sharpe, mdd]
    """
    pairs = []

    for rec in records:
        job_id = rec.get("job_id")
        job_type = rec.get("job_type")
        metrics = rec.get("metrics", {})
        timestamp = rec.get("ts", "")

        # Extract L1 metrics (Model Layer)
        ic = metrics.get("ic")
        ir = metrics.get("ir")

        # Extract L3 metrics (Strategy Layer)
        sharpe = metrics.get("sharpe")
        mdd = metrics.get("max_drawdown") or metrics.get("mdd")

        # Only include records with both IC and Sharpe
        if ic is not None and sharpe is not None:
            try:
                date = datetime.fromisoformat(timestamp).date() if timestamp else None
            except Exception:
                date = None

            pairs.append({
                "job_id": job_id,
                "job_type": job_type,
                "date": date,
                "ic": float(ic),
                "ir": float(ir) if ir is not None else 0.0,
                "sharpe": float(sharpe),
                "mdd": float(mdd) if mdd is not None else 0.0,
            })

    return pd.DataFrame(pairs)


def analyze_correlation(df: pd.DataFrame, min_samples: int = 5) -> Dict[str, Any]:
    """
    Analyze correlation between IC and Sharpe.

    Returns:
        {
            "n_samples": int,
            "ic_stats": {"mean": float, "std": float, "min": float, "max": float},
            "sharpe_stats": {...},
            "correlation": {
                "pearson": {"r": float, "pvalue": float},
                "spearman": {"rho": float, "pvalue": float}
            },
            "conclusion": str,
            "recommendations": [str]
        }
    """
    if len(df) < min_samples:
        return {
            "n_samples": len(df),
            "error": f"Insufficient samples (need >={min_samples})",
            "recommendations": ["Run more ML training jobs to collect data"]
        }

    # Calculate statistics
    ic_stats = {
        "mean": float(df["ic"].mean()),
        "std": float(df["ic"].std()),
        "min": float(df["ic"].min()),
        "max": float(df["ic"].max()),
    }

    sharpe_stats = {
        "mean": float(df["sharpe"].mean()),
        "std": float(df["sharpe"].std()),
        "min": float(df["sharpe"].min()),
        "max": float(df["sharpe"].max()),
    }

    # Calculate correlations
    pearson_r, pearson_p = pearsonr(df["ic"], df["sharpe"])
    spearman_rho, spearman_p = spearmanr(df["ic"], df["sharpe"])

    # Interpret correlation strength
    abs_rho = abs(spearman_rho)
    if abs_rho < 0.3:
        strength = "weak"
    elif abs_rho < 0.6:
        strength = "moderate"
    else:
        strength = "strong"

    direction = "positive" if spearman_rho > 0 else "negative"
    conclusion = f"{strength.capitalize()} {direction} correlation: "
    conclusion += f"higher IC → {'higher' if spearman_rho > 0 else 'lower'} Sharpe"

    # Generate recommendations
    recommendations = []

    if ic_stats["mean"] < 0.15:
        recommendations.append(
            "IC mean < 0.15: Model Layer predictive power is low. "
            "Suggest adding microstructure (orderbook) or cross-asset features (SPY/VIX)."
        )

    if sharpe_stats["mean"] < 1.0 and ic_stats["mean"] >= 0.2:
        recommendations.append(
            "IC adequate (>=0.2) but Sharpe < 1.0: Strategy Layer optimization needed. "
            "Suggest running alpha_loop to tune position sizing and risk parameters."
        )

    if abs_rho > 0.5:
        # Find IC threshold that predicts good Sharpe
        good_sharpe_threshold = 1.0
        good_samples = df[df["sharpe"] >= good_sharpe_threshold]
        if len(good_samples) >= 2:
            ic_threshold = good_samples["ic"].quantile(0.25)
            confidence = len(good_samples) / len(df)
            recommendations.append(
                f"Strong correlation detected: IC > {ic_threshold:.2f} usually leads to "
                f"Sharpe > {good_sharpe_threshold} ({confidence:.0%} confidence). "
                f"Consider setting policies.model.success_gates.min_ic={ic_threshold:.2f}"
            )

    if not recommendations:
        recommendations.append(
            "Metrics are within reasonable range. Continue monitoring and iterating."
        )

    return {
        "n_samples": len(df),
        "ic_stats": ic_stats,
        "sharpe_stats": sharpe_stats,
        "correlation": {
            "pearson": {"r": float(pearson_r), "pvalue": float(pearson_p)},
            "spearman": {"rho": float(spearman_rho), "pvalue": float(spearman_p)}
        },
        "conclusion": conclusion,
        "recommendations": recommendations
    }


def generate_report(analysis: Dict[str, Any], output_dir: Path) -> None:
    """
    Generate JSON and Markdown reports.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON report
    json_path = output_dir / "ic_to_sharpe.json"
    with open(json_path, "w") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)
    logger.info("json_report_saved", path=str(json_path))

    # Markdown report
    md_path = output_dir / "ic_to_sharpe.md"
    with open(md_path, "w") as f:
        f.write("# IC → Sharpe Correlation Report\n\n")
        f.write(f"**Generated**: {datetime.now().isoformat()}\n\n")

        if "error" in analysis:
            f.write(f"## ⚠️ {analysis['error']}\n\n")
            f.write("### Recommendations\n\n")
            for rec in analysis.get("recommendations", []):
                f.write(f"- {rec}\n")
            return

        f.write(f"**Samples**: {analysis['n_samples']}\n\n")

        # IC Statistics
        ic_stats = analysis["ic_stats"]
        f.write("## Model Layer (IC)\n\n")
        f.write(f"- Mean IC: {ic_stats['mean']:.4f}\n")
        f.write(f"- Std IC: {ic_stats['std']:.4f}\n")
        f.write(f"- Range: [{ic_stats['min']:.4f}, {ic_stats['max']:.4f}]\n\n")

        # Sharpe Statistics
        sharpe_stats = analysis["sharpe_stats"]
        f.write("## Strategy Layer (Sharpe)\n\n")
        f.write(f"- Mean Sharpe: {sharpe_stats['mean']:.4f}\n")
        f.write(f"- Std Sharpe: {sharpe_stats['std']:.4f}\n")
        f.write(f"- Range: [{sharpe_stats['min']:.4f}, {sharpe_stats['max']:.4f}]\n\n")

        # Correlation
        corr = analysis["correlation"]
        f.write("## Correlation Analysis\n\n")
        f.write(f"- Pearson r: {corr['pearson']['r']:.4f} (p={corr['pearson']['pvalue']:.4f})\n")
        f.write(f"- Spearman ρ: {corr['spearman']['rho']:.4f} (p={corr['spearman']['pvalue']:.4f})\n\n")

        f.write(f"### Conclusion\n\n")
        f.write(f"{analysis['conclusion']}\n\n")

        # Recommendations
        f.write("### Recommendations\n\n")
        for i, rec in enumerate(analysis["recommendations"], 1):
            f.write(f"{i}. {rec}\n\n")

    logger.info("markdown_report_saved", path=str(md_path))


def main():
    parser = argparse.ArgumentParser(description="Analyze IC to Sharpe correlation")
    parser.add_argument("--window-days", type=int, default=30, help="Look back window in days")
    parser.add_argument("--min-samples", type=int, default=5, help="Minimum samples required")
    parser.add_argument("--output-dir", type=str, help="Output directory (default: runs/ai_trader/latest)")
    args = parser.parse_args()

    logger.info("ic_sharpe_analysis_start", window_days=args.window_days, min_samples=args.min_samples)

    # Load data
    records = load_ml_success_logs(days=args.window_days)
    logger.info("ml_success_logs_loaded", n_records=len(records))

    # Extract IC/Sharpe pairs
    df = extract_ic_sharpe_pairs(records)
    logger.info("ic_sharpe_pairs_extracted", n_pairs=len(df))

    if len(df) > 0:
        logger.info("sample_data", head=df.head(3).to_dict(orient="records"))

    # Analyze correlation
    analysis = analyze_correlation(df, min_samples=args.min_samples)

    # Determine output directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        # Use latest date directory
        runs_base = Path("runs/ai_trader")
        date_dirs = sorted([d for d in runs_base.glob("*") if d.is_dir()], reverse=True)
        if date_dirs:
            output_dir = date_dirs[0]
        else:
            output_dir = runs_base / datetime.now().strftime("%Y-%m-%d")

    # Generate reports
    generate_report(analysis, output_dir)

    # Print summary
    print("=" * 60)
    print("IC → Sharpe Correlation Analysis")
    print("=" * 60)
    if "error" in analysis:
        print(f"\n⚠️  {analysis['error']}")
    else:
        print(f"\nSamples: {analysis['n_samples']}")
        print(f"IC mean: {analysis['ic_stats']['mean']:.4f}")
        print(f"Sharpe mean: {analysis['sharpe_stats']['mean']:.4f}")
        print(f"Correlation (Spearman): {analysis['correlation']['spearman']['rho']:.4f}")
        print(f"\nConclusion: {analysis['conclusion']}")
        print("\nRecommendations:")
        for i, rec in enumerate(analysis["recommendations"], 1):
            print(f"  {i}. {rec}")

    print(f"\nReports saved to: {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
