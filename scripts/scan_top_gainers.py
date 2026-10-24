"""
IBKR Top Gainers Scanner
连接到 IBKR Gateway 获取当日涨幅榜

用法:
    python scripts/scan_top_gainers.py --top 20
"""
import os
import sys
import time
import argparse
import threading
from datetime import datetime
from typing import List, Dict

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ib_insync import IB, Stock, util
from libs.config import config
from libs.logger import get_logger

logger = get_logger(__name__)


class TopGainersScanner:
    """IBKR 涨幅榜扫描器"""

    def __init__(self):
        self.ib = IB()
        self.results: List[Dict] = []
        self.scan_complete = threading.Event()

        # 从配置获取连接参数
        ib_cfg = config.ib_config
        self.host = ib_cfg['host']
        self.port = ib_cfg['port']
        self.client_id = ib_cfg['client_id'] + 200  # 使用不同的 client_id
        self.mode = ib_cfg['mode']

        logger.info(
            f"Scanner initialized",
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            mode=self.mode
        )

    def connect(self):
        """连接到 IBKR Gateway"""
        try:
            logger.info("Connecting to IBKR Gateway...", host=self.host, port=self.port)
            self.ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=15
            )
            logger.info("✅ Connected to IBKR Gateway")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to IBKR Gateway: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IBKR Gateway")

    def scan_top_gainers(
        self,
        num_rows: int = 20,
        instrument: str = "STK",
        location: str = "STK.US.MAJOR",
        scan_code: str = "TOP_PERC_GAIN",
        min_price: float = 1.0,
        min_volume: int = 100000,
        min_market_cap: float = 0.0
    ) -> List[Dict]:
        """
        扫描涨幅榜

        Args:
            num_rows: 返回多少条结果（最多50）
            instrument: 证券类型 (STK=股票)
            location: 市场代码
            scan_code: 扫描类型 (TOP_PERC_GAIN=涨幅榜, TOP_PERC_GAIN_PREMARKET=盘前涨幅榜)
            min_price: 最低价格过滤
            min_volume: 最低成交量过滤
            min_market_cap: 最低市值过滤（单位：亿美元）

        Returns:
            扫描结果列表
        """
        logger.info(
            f"Starting scanner",
            num_rows=num_rows,
            instrument=instrument,
            location=location,
            scan_code=scan_code
        )

        try:
            # 使用 ib_insync 的 reqScannerData (同步方法)
            from ib_insync import ScannerSubscription

            # 构造扫描订阅
            sub = ScannerSubscription()
            sub.numberOfRows = min(num_rows, 50)  # 最多50行
            sub.instrument = instrument
            sub.locationCode = location
            sub.scanCode = scan_code

            # 过滤条件
            if min_price > 0:
                sub.abovePrice = min_price
            if min_volume > 0:
                sub.aboveVolume = min_volume
            if min_market_cap > 0:
                # IBKR 市值单位是百万美元，我们用亿美元作为输入
                sub.marketCapAbove = min_market_cap * 100  # 转换为百万美元

            # 执行扫描 (ib_insync 的同步方法)
            scan_data = self.ib.reqScannerData(sub)

            results = []
            for data in scan_data:
                contract = data.contractDetails.contract
                result = {
                    'rank': data.rank + 1,
                    'symbol': contract.symbol,
                    'conId': contract.conId,
                    'exchange': contract.exchange,
                    'currency': contract.currency,
                    'secType': contract.secType,
                    'distance': data.distance if hasattr(data, 'distance') else None,
                    'timestamp': datetime.now()
                }
                results.append(result)

                logger.debug(
                    f"Rank {result['rank']}: {result['symbol']}",
                    conId=result['conId'],
                    exchange=result['exchange']
                )

            logger.info(f"Scanner completed, got {len(results)} results")
            return results

        except Exception as e:
            logger.error(f"Scanner failed: {e}")
            return []

    def get_quotes_for_symbols(self, results: List[Dict], timeout: int = 10) -> List[Dict]:
        """
        为扫描结果获取实时报价

        Args:
            results: 扫描结果
            timeout: 超时时间（秒）

        Returns:
            包含报价的结果列表
        """
        if not results:
            return []

        logger.info(f"Fetching quotes for {len(results)} symbols...")

        contracts = []
        for result in results:
            contract = Stock(
                symbol=result['symbol'],
                exchange='SMART',
                currency='USD'
            )
            contracts.append(contract)

        try:
            # 批量订阅市场数据
            tickers = self.ib.reqTickers(*contracts)

            # 等待数据就绪
            self.ib.sleep(timeout)

            # 更新结果
            for i, ticker in enumerate(tickers):
                if i < len(results):
                    results[i]['last_price'] = ticker.last if ticker.last == ticker.last else None  # 检查 NaN
                    results[i]['bid'] = ticker.bid if ticker.bid == ticker.bid else None
                    results[i]['ask'] = ticker.ask if ticker.ask == ticker.ask else None
                    results[i]['volume'] = ticker.volume if hasattr(ticker, 'volume') else None
                    results[i]['change'] = ticker.change if hasattr(ticker, 'change') else None
                    results[i]['change_pct'] = (ticker.change / ticker.last * 100) if ticker.last and ticker.change else None

            logger.info(f"Quotes fetched successfully")

        except Exception as e:
            logger.error(f"Failed to fetch quotes: {e}")

        return results

    def print_results(self, results: List[Dict], with_quotes: bool = False, is_premarket: bool = False):
        """打印扫描结果"""
        if not results:
            print("\n⚠️  No results found")
            return

        market_type = "Pre-Market" if is_premarket else "Regular Market"
        print(f"\n{'='*80}")
        print(f"📈 IBKR Top % Gainers ({market_type}) - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*80}")

        if with_quotes:
            print(f"\n{'Rank':<6s} {'Symbol':<10s} {'Last':>10s} {'Change':>10s} {'Change%':>10s} {'Volume':>12s}")
            print(f"{'-'*80}")

            for result in results:
                last_price = f"${result['last_price']:.2f}" if result.get('last_price') else "N/A"
                change = f"{result['change']:+.2f}" if result.get('change') else "N/A"
                change_pct = f"{result['change_pct']:+.2f}%" if result.get('change_pct') else "N/A"
                volume = f"{result['volume']:,}" if result.get('volume') else "N/A"

                print(f"{result['rank']:<6d} {result['symbol']:<10s} {last_price:>10s} {change:>10s} {change_pct:>10s} {volume:>12s}")
        else:
            print(f"\n{'Rank':<6s} {'Symbol':<10s} {'ConId':>12s} {'Exchange':<12s}")
            print(f"{'-'*80}")

            for result in results:
                print(f"{result['rank']:<6d} {result['symbol']:<10s} {result['conId']:>12d} {result['exchange']:<12s}")

        print(f"{'='*80}\n")

    def save_to_csv(self, results: List[Dict], filename: str = None):
        """保存结果到 CSV"""
        if not results:
            logger.warning("No results to save")
            return

        import pandas as pd

        if filename is None:
            img_dir = os.path.join(PROJECT_ROOT, 'img')
            os.makedirs(img_dir, exist_ok=True)
            filename = os.path.join(img_dir, f"top_gainers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

        df = pd.DataFrame(results)
        df.to_csv(filename, index=False)
        logger.info(f"Results saved to: {filename}")
        print(f"\n📄 Results saved to: {filename}")


def main():
    parser = argparse.ArgumentParser(description="Scan IBKR Top % Gainers")
    parser.add_argument('--top', type=int, default=20, help='Number of top gainers to fetch (max 50)')
    parser.add_argument('--premarket', action='store_true', help='Scan pre-market gainers (9:00-9:30 AM)')
    parser.add_argument('--min_price', type=float, default=1.0, help='Minimum price filter')
    parser.add_argument('--min_volume', type=int, default=100000, help='Minimum volume filter')
    parser.add_argument('--min_market_cap', type=float, default=0.0, help='Minimum market cap in billions (e.g., 50 for $50B)')
    parser.add_argument('--quotes', action='store_true', help='Fetch real-time quotes')
    parser.add_argument('--save', action='store_true', help='Save results to CSV')

    args = parser.parse_args()

    # 确定扫描类型
    if args.premarket:
        scan_code = "TOP_PERC_GAIN_PREMARKET"
        logger.info("Using pre-market scanner")
    else:
        scan_code = "TOP_PERC_GAIN"
        logger.info("Using regular market scanner")

    scanner = TopGainersScanner()

    try:
        # 连接
        if not scanner.connect():
            logger.error("Failed to connect, exiting")
            return

        # 扫描
        results = scanner.scan_top_gainers(
            num_rows=args.top,
            scan_code=scan_code,
            min_price=args.min_price,
            min_volume=args.min_volume,
            min_market_cap=args.min_market_cap
        )

        if not results:
            print("\n⚠️  No results returned from scanner")
            return

        # 获取报价（可选）
        if args.quotes:
            results = scanner.get_quotes_for_symbols(results, timeout=5)

        # 打印结果
        scanner.print_results(results, with_quotes=args.quotes, is_premarket=args.premarket)

        # 保存到 CSV（可选）
        if args.save:
            scanner.save_to_csv(results)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    finally:
        scanner.disconnect()


if __name__ == '__main__':
    # 修复 asyncio 兼容性
    util.patchAsyncio()
    main()
