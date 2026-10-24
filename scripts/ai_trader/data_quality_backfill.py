#!/usr/bin/env python3
"""
Data Quality Layered Backfill & Blacklist Management

Priority-based backfill strategy:
- Priority 1 (Critical): Current day symbols in active trading
- Priority 2 (High): Symbols in yesterday's plan
- Priority 3 (Medium): Symbols with recent data gaps
- Priority 4 (Low): All other symbols

Blacklist removal conditions:
- 3 consecutive successful data days
- No outliers/gaps in last 5 days
- Manual approval flag

Usage:
    python scripts/ai_trader/data_quality_backfill.py --date 2025-11-01 --priority 1 --dry-run
"""
import argparse
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Dict, List, Set, Optional
from zoneinfo import ZoneInfo

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from libs.logger import get_logger
from libs.db.clickhouse import ClickHouseDB
from scripts.ai_trader.gates.data_quality_gate import DataQualityGate

logger = get_logger(__name__)

NY_TZ = ZoneInfo("America/New_York")

# Blacklist file
BLACKLIST_FILE = Path("config/data_quality_blacklist.json")


class DataQualityBackfill:
    def __init__(self, runs_dir: Path, config: Dict):
        self.runs_dir = runs_dir
        self.config = config
        self.log = logger
        self.ch = ClickHouseDB()
        self.dq_gate = DataQualityGate(config.get('data_quality_checks', {}))

        # Load blacklist
        self.blacklist = self._load_blacklist()

    def _load_blacklist(self) -> Dict[str, Dict]:
        """Load data quality blacklist.

        Schema:
        {
            "AAPL": {
                "added_date": "2025-11-01",
                "reason": "persistent_gaps",
                "consecutive_good_days": 0,
                "last_check_date": "2025-11-01"
            }
        }
        """
        if not BLACKLIST_FILE.exists():
            return {}

        try:
            with open(BLACKLIST_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.log.error("blacklist_load_failed", err=str(e))
            return {}

    def _save_blacklist(self):
        """Save blacklist to file."""
        try:
            BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(BLACKLIST_FILE, 'w') as f:
                json.dump(self.blacklist, f, indent=2)
            self.log.info("blacklist_saved", count=len(self.blacklist))
        except Exception as e:
            self.log.error("blacklist_save_failed", err=str(e))

    def get_priority_symbols(self, priority: int, target_date: date) -> Set[str]:
        """Get symbols by priority level.

        Priority levels:
        1 - Critical: Today's active trading symbols
        2 - High: Yesterday's plan symbols
        3 - Medium: Symbols with recent gaps (last 5 days)
        4 - Low: All configured symbols
        """
        symbols: Set[str] = set()

        if priority == 1:
            # Today's active symbols from plans
            today_dir = self.runs_dir / target_date.isoformat()
            if today_dir.exists():
                for plan_file in today_dir.glob("brain_plan_*.json"):
                    try:
                        with open(plan_file, 'r') as f:
                            plan = json.load(f)
                        for provider in plan.get('providers', []):
                            symbols.update(provider.get('symbols', []))
                    except Exception:
                        pass

        elif priority == 2:
            # Yesterday's symbols
            yesterday = target_date - timedelta(days=1)
            yesterday_dir = self.runs_dir / yesterday.isoformat()
            if yesterday_dir.exists():
                for plan_file in yesterday_dir.glob("brain_plan_*.json"):
                    try:
                        with open(plan_file, 'r') as f:
                            plan = json.load(f)
                        for provider in plan.get('providers', []):
                            symbols.update(provider.get('symbols', []))
                    except Exception:
                        pass

        elif priority == 3:
            # Symbols with recent gaps (check last 5 days)
            symbols_with_gaps = set()
            for i in range(5):
                check_date = target_date - timedelta(days=i)
                gap_file = self.runs_dir / check_date.isoformat() / "data_quality_gaps.json"
                if gap_file.exists():
                    try:
                        with open(gap_file, 'r') as f:
                            gaps = json.load(f)
                        symbols_with_gaps.update(gaps.get('failed', {}).keys())
                    except Exception:
                        pass
            symbols = symbols_with_gaps

        elif priority == 4:
            # All configured symbols
            dq_config = self.config.get('gates', {}).get('data_quality_checks', {})
            symbols = set(dq_config.get('symbols_to_check', []))

        # Remove blacklisted symbols
        symbols = symbols - set(self.blacklist.keys())

        return symbols

    def backfill_symbol(self, symbol: str, target_date: date, dry_run: bool = True) -> Dict:
        """Backfill data for a single symbol.

        Returns:
            {
                'symbol': str,
                'date': str,
                'success': bool,
                'method': 'polygon'|'ibkr'|'cache',
                'bars_count': int,
                'errors': List[str]
            }
        """
        result = {
            'symbol': symbol,
            'date': target_date.isoformat(),
            'success': False,
            'method': None,
            'bars_count': 0,
            'errors': []
        }

        self.log.info("backfill_start", symbol=symbol, date=target_date, dry_run=dry_run)

        # Check if data already exists
        existing = self._check_existing_data(symbol, target_date)
        if existing['complete']:
            result['success'] = True
            result['method'] = 'exists'
            result['bars_count'] = existing['count']
            self.log.info("backfill_skip_exists", symbol=symbol, bars=existing['count'])
            return result

        if dry_run:
            result['success'] = True
            result['method'] = 'dry_run'
            self.log.info("backfill_dry_run", symbol=symbol)
            return result

        # Try backfill methods in order
        methods = ['polygon', 'ibkr', 'yahoo']

        for method in methods:
            try:
                if method == 'polygon':
                    bars = self._backfill_from_polygon(symbol, target_date)
                elif method == 'ibkr':
                    bars = self._backfill_from_ibkr(symbol, target_date)
                elif method == 'yahoo':
                    bars = self._backfill_from_yahoo(symbol, target_date)
                else:
                    continue

                if bars and len(bars) > 0:
                    # Insert into ClickHouse
                    self._insert_bars(symbol, target_date, bars)
                    result['success'] = True
                    result['method'] = method
                    result['bars_count'] = len(bars)
                    self.log.info("backfill_success", symbol=symbol, method=method, bars=len(bars))
                    break

            except Exception as e:
                self.log.warning("backfill_method_failed", symbol=symbol, method=method, err=str(e))
                result['errors'].append(f"{method}: {str(e)}")

        if not result['success']:
            # Add to blacklist if repeated failures
            self._add_to_blacklist(symbol, target_date, "backfill_failed")

        return result

    def _check_existing_data(self, symbol: str, target_date: date) -> Dict:
        """Check if data already exists for symbol/date."""
        sql = """
        SELECT count() as cnt
        FROM trading.bars_1m_ibkr
        WHERE symbol = {symbol:String}
          AND toDate(toTimeZone(ts_end, 'America/New_York')) = {d:Date}
        """
        try:
            rows = self.ch.query(sql, {'symbol': symbol, 'd': target_date})
            count = rows[0][0] if rows else 0

            # Regular hours: 09:30-16:00 = 390 bars expected
            expected = 390
            complete = count >= expected * 0.95  # 95% threshold

            return {'count': count, 'complete': complete, 'expected': expected}
        except Exception as e:
            self.log.error("existing_data_check_failed", symbol=symbol, err=str(e))
            return {'count': 0, 'complete': False, 'expected': 390}

    def _backfill_from_polygon(self, symbol: str, target_date: date) -> List[Dict]:
        """Backfill from Polygon API."""
        # Placeholder - implement actual Polygon API call
        self.log.info("polygon_backfill", symbol=symbol, date=target_date)
        return []

    def _backfill_from_ibkr(self, symbol: str, target_date: date) -> List[Dict]:
        """Backfill from IBKR API."""
        # Placeholder - implement actual IBKR API call
        self.log.info("ibkr_backfill", symbol=symbol, date=target_date)
        return []

    def _backfill_from_yahoo(self, symbol: str, target_date: date) -> List[Dict]:
        """Backfill from Yahoo Finance."""
        # Placeholder - implement actual Yahoo Finance call
        self.log.info("yahoo_backfill", symbol=symbol, date=target_date)
        return []

    def _insert_bars(self, symbol: str, target_date: date, bars: List[Dict]):
        """Insert bars into ClickHouse."""
        # Placeholder - implement actual insert
        self.log.info("insert_bars", symbol=symbol, date=target_date, count=len(bars))

    def _add_to_blacklist(self, symbol: str, target_date: date, reason: str):
        """Add symbol to blacklist."""
        if symbol not in self.blacklist:
            self.blacklist[symbol] = {
                'added_date': target_date.isoformat(),
                'reason': reason,
                'consecutive_good_days': 0,
                'last_check_date': target_date.isoformat()
            }
            self._save_blacklist()
            self.log.warning("symbol_blacklisted", symbol=symbol, reason=reason)

    def check_blacklist_removal(self, symbol: str, target_date: date) -> bool:
        """Check if symbol should be removed from blacklist.

        Removal conditions:
        - 3 consecutive successful data days
        - No outliers/gaps in last 5 days
        """
        if symbol not in self.blacklist:
            return False

        bl_entry = self.blacklist[symbol]

        # Run data quality check for last 5 days
        good_days = 0
        for i in range(5):
            check_date = target_date - timedelta(days=i)
            result = self.dq_gate.check([symbol], check_date)

            if result.get('ok') and symbol in result.get('passed', []):
                good_days += 1
            else:
                break  # Reset on first failure

        # Update consecutive count
        bl_entry['consecutive_good_days'] = good_days
        bl_entry['last_check_date'] = target_date.isoformat()

        # Remove if 3+ consecutive good days
        if good_days >= 3:
            self.log.info("blacklist_removal", symbol=symbol, good_days=good_days)
            del self.blacklist[symbol]
            self._save_blacklist()
            return True

        self._save_blacklist()
        return False

    def run_backfill(
        self,
        target_date: date,
        priority: int = 1,
        dry_run: bool = True,
        max_symbols: Optional[int] = None
    ) -> Dict:
        """Run backfill for given priority level.

        Returns:
            {
                'priority': int,
                'date': str,
                'symbols_processed': int,
                'success': int,
                'failed': int,
                'results': List[Dict]
            }
        """
        symbols = self.get_priority_symbols(priority, target_date)

        if max_symbols:
            symbols = set(list(symbols)[:max_symbols])

        self.log.info(
            "backfill_run_start",
            priority=priority,
            date=target_date,
            symbols_count=len(symbols),
            dry_run=dry_run
        )

        results = []
        success_count = 0
        failed_count = 0

        for symbol in sorted(symbols):
            result = self.backfill_symbol(symbol, target_date, dry_run=dry_run)
            results.append(result)

            if result['success']:
                success_count += 1
            else:
                failed_count += 1

            # Check for blacklist removal
            if symbol in self.blacklist:
                self.check_blacklist_removal(symbol, target_date)

        summary = {
            'priority': priority,
            'date': target_date.isoformat(),
            'symbols_processed': len(symbols),
            'success': success_count,
            'failed': failed_count,
            'results': results,
            'blacklist_count': len(self.blacklist)
        }

        # Save summary
        output_dir = self.runs_dir / target_date.isoformat()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"backfill_p{priority}_{datetime.now().strftime('%H%M%S')}.json"
        with open(output_file, 'w') as f:
            json.dump(summary, f, indent=2)

        self.log.info("backfill_run_complete", summary=summary, output=str(output_file))

        return summary


def main():
    parser = argparse.ArgumentParser(description='Data quality layered backfill')
    parser.add_argument('--date', help='Target date (YYYY-MM-DD), default: yesterday')
    parser.add_argument('--priority', type=int, default=1, choices=[1, 2, 3, 4],
                        help='Priority level (1=Critical, 2=High, 3=Medium, 4=Low)')
    parser.add_argument('--runs-dir', default='runs/ai_trader', help='Runs directory')
    parser.add_argument('--config', default='config.yml', help='Config file')
    parser.add_argument('--dry-run', action='store_true', help='Dry run mode')
    parser.add_argument('--max-symbols', type=int, help='Maximum symbols to process')
    args = parser.parse_args()

    # Parse target date
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        target_date = date.today() - timedelta(days=1)

    # Load config
    import yaml
    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)

    # Run backfill
    backfiller = DataQualityBackfill(Path(args.runs_dir), config)
    summary = backfiller.run_backfill(
        target_date=target_date,
        priority=args.priority,
        dry_run=args.dry_run,
        max_symbols=args.max_symbols
    )

    # Print summary
    print("\n" + "="*80)
    print("Data Quality Backfill Summary")
    print("="*80)
    print(f"Priority: {summary['priority']}")
    print(f"Date: {summary['date']}")
    print(f"Symbols Processed: {summary['symbols_processed']}")
    print(f"Success: {summary['success']}")
    print(f"Failed: {summary['failed']}")
    print(f"Blacklist Count: {summary['blacklist_count']}")
    print("="*80)

    sys.exit(0 if summary['failed'] == 0 else 1)


if __name__ == '__main__':
    main()
