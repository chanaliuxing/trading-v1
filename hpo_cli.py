# -*- coding: utf-8 -*-
"""
hpo_cli.py
----------
CLI runner for walk-forward HPO using wf_hpo.WalkForwardHPO.

Usage:
  python hpo_cli.py --config /path/to/config.json --out_dir /path/to/outdir --print-summary --plots

Config format: see hpo_config_example.json or hpo_config_autofolds.json
"""

from __future__ import annotations
import os, sys, json, argparse

def _load_config(path: str):
    if path.endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    try:
        import yaml  # optional
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        raise SystemExit(f"Unsupported config format or missing PyYAML for YAML: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="JSON (or YAML with PyYAML) config path")
    ap.add_argument("--out_dir", required=False, default="./wf_out", help="Directory to write CSV/Parquet/JSON outputs")
    ap.add_argument("--print-summary", action="store_true", help="Print OOS summary to stdout")
    ap.add_argument("--plots", action="store_true", help="Also export PNG charts")
    args = ap.parse_args()

    cfg = _load_config(args.config)
    from wf_hpo import WalkForwardHPO
    runner = WalkForwardHPO(cfg)
    result = runner.run()
    paths = runner.export_tables(result, args.out_dir)

    if args.plots:
        plot_paths = runner.export_plots(result, args.out_dir)
        paths.update(plot_paths)

    print("Artifacts:")
    for k, v in paths.items():
        print(f"  {k}: {v}")
    if args.print_summary:
        print("\nOOS Summary:")
        print(json.dumps(result.get("oos_summary", {}), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
