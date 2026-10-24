#!/usr/bin/env python3
"""
Automatic AB Weight Adjuster with TAMS Integration

Dynamically adjusts AB test weights based on prompt performance.
Integrates with TAMS (Target Asset Management System) for exposure limits.

Rules:
1. If challenger Sharpe > champion Sharpe * 1.1, increase challenger weight
2. If challenger MDD > champion MDD * 1.2, decrease challenger weight
3. Never exceed TAMS exposure limits
4. Gradual weight shifts (max 0.05 per adjustment)
5. Minimum observation period: 100 decisions

Usage:
    python scripts/ai_trader/ab_weight_adjuster.py --runs-dir runs/ai_trader --dry-run
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import yaml

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from libs.logger import get_logger
from libs import metrics

logger = get_logger(__name__)


class ABWeightAdjuster:
    def __init__(self, runs_dir: Path, config_path: Path, lookback_days: int = 7):
        self.runs_dir = runs_dir
        self.config_path = config_path
        self.lookback_days = lookback_days
        self.log = logger

        # Load current config
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        self.ab_config = self.config.get('policies', {}).get('ab', {})
        self.tams_config = self.config.get('risk_management', {}).get('tams', {})

    def get_current_weights(self) -> Dict[str, float]:
        """Get current AB weights from config."""
        weights = self.ab_config.get('default_weights', {})
        return {
            'champion': weights.get('champion', 0.7),
            'challenger': weights.get('challenger', 0.3)
        }

    def collect_ab_performance(self) -> Dict[str, Dict]:
        """Collect performance metrics for champion vs challenger.

        Returns:
            {
                'champion': {'sharpe': X, 'mdd': Y, 'pnl': Z, 'decisions': N},
                'challenger': {...}
            }
        """
        perf = {
            'champion': {'sharpe': 0.0, 'mdd': 0.0, 'pnl': 0.0, 'decisions': 0, 'trades': []},
            'challenger': {'sharpe': 0.0, 'mdd': 0.0, 'pnl': 0.0, 'decisions': 0, 'trades': []}
        }

        # Scan runs directory for AB performance data
        start_date = datetime.now() - timedelta(days=self.lookback_days)

        for date_dir in sorted(self.runs_dir.glob("????-??-??")):
            try:
                date = datetime.strptime(date_dir.name, "%Y-%m-%d")
                if date < start_date:
                    continue

                # Look for AB performance files
                for ab_file in date_dir.glob("ab_performance_*.json"):
                    with open(ab_file, 'r') as f:
                        data = json.load(f)

                    for group in ['champion', 'challenger']:
                        if group in data:
                            gdata = data[group]
                            perf[group]['sharpe'] += gdata.get('sharpe', 0.0)
                            perf[group]['mdd'] = max(perf[group]['mdd'], gdata.get('mdd', 0.0))
                            perf[group]['pnl'] += gdata.get('pnl', 0.0)
                            perf[group]['decisions'] += gdata.get('decisions', 0)
                            perf[group]['trades'].extend(gdata.get('trades', []))

            except Exception as e:
                self.log.warning("ab_perf_scan_failed", date_dir=str(date_dir), err=str(e))

        # Average Sharpe across days
        for group in ['champion', 'challenger']:
            days_with_data = len([d for d in perf[group]['trades'] if d])
            if days_with_data > 0:
                perf[group]['sharpe'] /= days_with_data

        return perf

    def calculate_new_weights(
        self,
        current_weights: Dict[str, float],
        performance: Dict[str, Dict]
    ) -> Tuple[Dict[str, float], str]:
        """Calculate new AB weights based on performance.

        Returns:
            (new_weights, reason)
        """
        champ_perf = performance['champion']
        chall_perf = performance['challenger']

        # Check minimum observation requirement
        min_decisions = 100
        if champ_perf['decisions'] < min_decisions or chall_perf['decisions'] < min_decisions:
            return current_weights, f"insufficient_data (champion: {champ_perf['decisions']}, challenger: {chall_perf['decisions']})"

        # Load adjustment rules
        rules = self.ab_config.get('promote_rules', {})
        sharpe_gain_ratio = rules.get('sharpe_gain_ratio', 1.05)
        mdd_improve_ratio = rules.get('mdd_improve_ratio', 0.9)

        # Calculate performance ratios
        sharpe_ratio = chall_perf['sharpe'] / max(champ_perf['sharpe'], 0.1)  # Avoid div by zero
        mdd_ratio = chall_perf['mdd'] / max(champ_perf['mdd'], 0.01)

        # Adjustment logic
        new_weights = current_weights.copy()
        max_shift = 0.05  # Maximum weight change per adjustment
        reason = "no_change"

        # Promote challenger if significantly better
        if sharpe_ratio > 1.1 and mdd_ratio < 1.2:
            shift = min(max_shift, 0.1 * (sharpe_ratio - 1.0))  # Scale shift by performance
            new_weights['challenger'] = min(0.5, current_weights['challenger'] + shift)
            new_weights['champion'] = 1.0 - new_weights['challenger']
            reason = f"challenger_outperforms (sharpe_ratio={sharpe_ratio:.2f}, mdd_ratio={mdd_ratio:.2f})"

        # Demote challenger if performing worse
        elif sharpe_ratio < 0.9 or mdd_ratio > 1.3:
            shift = min(max_shift, 0.1 * (1.0 - sharpe_ratio))
            new_weights['challenger'] = max(0.1, current_weights['challenger'] - shift)
            new_weights['champion'] = 1.0 - new_weights['challenger']
            reason = f"challenger_underperforms (sharpe_ratio={sharpe_ratio:.2f}, mdd_ratio={mdd_ratio:.2f})"

        # Full promotion if challenger dominates
        elif sharpe_ratio > sharpe_gain_ratio and mdd_ratio < mdd_improve_ratio:
            new_weights['champion'] = 0.3
            new_weights['challenger'] = 0.7
            reason = f"challenger_dominates (sharpe_ratio={sharpe_ratio:.2f}, mdd_ratio={mdd_ratio:.2f})"

        # Ensure weights sum to 1.0
        total = new_weights['champion'] + new_weights['challenger']
        new_weights['champion'] /= total
        new_weights['challenger'] /= total

        return new_weights, reason

    def check_tams_limits(
        self,
        new_weights: Dict[str, float],
        performance: Dict[str, Dict]
    ) -> Tuple[bool, Optional[str]]:
        """Check if new weights violate TAMS exposure limits.

        Returns:
            (allowed, reason)
        """
        if not self.tams_config:
            return True, None

        max_exposure = self.tams_config.get('max_total_exposure', 1.0)
        max_per_prompt = self.tams_config.get('max_per_prompt_exposure', 0.5)

        # Calculate expected exposures
        champ_exposure = new_weights['champion'] * abs(performance['champion']['pnl'])
        chall_exposure = new_weights['challenger'] * abs(performance['challenger']['pnl'])
        total_exposure = champ_exposure + chall_exposure

        # Check limits
        if total_exposure > max_exposure:
            return False, f"total_exposure_exceeded ({total_exposure:.2f} > {max_exposure:.2f})"

        if chall_exposure > max_per_prompt:
            return False, f"challenger_exposure_exceeded ({chall_exposure:.2f} > {max_per_prompt:.2f})"

        return True, None

    def apply_weights(self, new_weights: Dict[str, float], dry_run: bool = True) -> bool:
        """Apply new weights to config file.

        Args:
            new_weights: New AB weights
            dry_run: If True, don't actually write to file

        Returns:
            Success status
        """
        if dry_run:
            self.log.info("ab_weights_dry_run", new_weights=new_weights)
            print(f"[DRY RUN] Would update weights to: {new_weights}")
            return True

        try:
            # Update config
            if 'policies' not in self.config:
                self.config['policies'] = {}
            if 'ab' not in self.config['policies']:
                self.config['policies']['ab'] = {}
            if 'default_weights' not in self.config['policies']['ab']:
                self.config['policies']['ab']['default_weights'] = {}

            self.config['policies']['ab']['default_weights']['champion'] = round(new_weights['champion'], 3)
            self.config['policies']['ab']['default_weights']['challenger'] = round(new_weights['challenger'], 3)

            # Backup current config
            backup_path = self.config_path.with_suffix(f'.yaml.ab_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
            with open(self.config_path, 'r') as f:
                with open(backup_path, 'w') as b:
                    b.write(f.read())

            # Write new config
            with open(self.config_path, 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)

            self.log.info("ab_weights_updated", new_weights=new_weights, backup=str(backup_path))

            # Update metrics
            metrics.prompt_ab_weight.labels(ab_group='champion').set(new_weights['champion'])
            metrics.prompt_ab_weight.labels(ab_group='challenger').set(new_weights['challenger'])

            return True

        except Exception as e:
            self.log.error("ab_weights_apply_failed", err=str(e), exc_info=True)
            return False

    def run(self, dry_run: bool = True) -> Dict:
        """Run AB weight adjustment.

        Returns:
            Adjustment result dict
        """
        self.log.info("ab_adjuster_start", lookback_days=self.lookback_days, dry_run=dry_run)

        # Get current weights
        current_weights = self.get_current_weights()
        self.log.info("ab_current_weights", weights=current_weights)

        # Collect performance
        performance = self.collect_ab_performance()
        self.log.info("ab_performance",
            champion_sharpe=performance['champion']['sharpe'],
            champion_decisions=performance['champion']['decisions'],
            challenger_sharpe=performance['challenger']['sharpe'],
            challenger_decisions=performance['challenger']['decisions']
        )

        # Calculate new weights
        new_weights, reason = self.calculate_new_weights(current_weights, performance)
        self.log.info("ab_weights_calculated", new_weights=new_weights, reason=reason)

        # Check TAMS limits
        allowed, tams_reason = self.check_tams_limits(new_weights, performance)
        if not allowed:
            self.log.warning("ab_weights_rejected_tams", reason=tams_reason)
            return {
                'status': 'rejected',
                'reason': tams_reason,
                'current_weights': current_weights,
                'proposed_weights': new_weights
            }

        # Apply weights
        if new_weights != current_weights:
            success = self.apply_weights(new_weights, dry_run=dry_run)
            status = 'applied' if success and not dry_run else 'dry_run' if dry_run else 'failed'
        else:
            success = True
            status = 'no_change'

        result = {
            'status': status,
            'reason': reason,
            'current_weights': current_weights,
            'new_weights': new_weights,
            'performance': {
                'champion': {
                    'sharpe': performance['champion']['sharpe'],
                    'mdd': performance['champion']['mdd'],
                    'decisions': performance['champion']['decisions']
                },
                'challenger': {
                    'sharpe': performance['challenger']['sharpe'],
                    'mdd': performance['challenger']['mdd'],
                    'decisions': performance['challenger']['decisions']
                }
            }
        }

        # Save result
        output_dir = self.runs_dir / datetime.now().strftime("%Y-%m-%d")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"ab_weight_adjustment_{datetime.now().strftime('%H%M%S')}.json"
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2)

        self.log.info("ab_adjuster_complete", result=result, output=str(output_file))

        return result


def main():
    parser = argparse.ArgumentParser(description='Automatic AB weight adjuster')
    parser.add_argument('--runs-dir', default='runs/ai_trader', help='Runs directory')
    parser.add_argument('--config', default='scripts/ai_trader/config/ai_trader.yaml', help='Config file')
    parser.add_argument('--lookback-days', type=int, default=7, help='Days to look back for performance data')
    parser.add_argument('--dry-run', action='store_true', help='Dry run mode (no config changes)')
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    config_path = Path(args.config)

    if not runs_dir.exists():
        print(f"❌ Error: Runs directory not found: {runs_dir}")
        sys.exit(1)

    if not config_path.exists():
        print(f"❌ Error: Config file not found: {config_path}")
        sys.exit(1)

    adjuster = ABWeightAdjuster(runs_dir, config_path, args.lookback_days)
    result = adjuster.run(dry_run=args.dry_run)

    print("\n" + "="*80)
    print("AB Weight Adjustment Result")
    print("="*80)
    print(f"Status: {result['status']}")
    print(f"Reason: {result['reason']}")
    print(f"\nCurrent Weights:")
    print(f"  Champion:   {result['current_weights']['champion']:.3f}")
    print(f"  Challenger: {result['current_weights']['challenger']:.3f}")
    print(f"\nNew Weights:")
    print(f"  Champion:   {result['new_weights']['champion']:.3f}")
    print(f"  Challenger: {result['new_weights']['challenger']:.3f}")
    print(f"\nPerformance:")
    print(f"  Champion:   Sharpe={result['performance']['champion']['sharpe']:.3f}, "
          f"Decisions={result['performance']['champion']['decisions']}")
    print(f"  Challenger: Sharpe={result['performance']['challenger']['sharpe']:.3f}, "
          f"Decisions={result['performance']['challenger']['decisions']}")
    print("="*80)

    sys.exit(0 if result['status'] in ['applied', 'no_change', 'dry_run'] else 1)


if __name__ == '__main__':
    main()
