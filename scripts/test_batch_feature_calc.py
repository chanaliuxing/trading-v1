#!/usr/bin/env python3
"""
Test script for batch feature calculation
Verifies that feature calculator correctly splits symbols into batches
"""
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.market.features.feature_calculator import FeatureCalculator
from libs.logger import get_logger, configure_logging
from libs.config import config

logger = get_logger(__name__)


async def test_batch_processing():
    """Test batch processing functionality"""
    configure_logging()

    logger.info("=" * 80)
    logger.info("Testing Batch Feature Calculation")
    logger.info("=" * 80)

    # Display configuration
    universe = config.get('universe', [])
    batch_size = config.get('feature_calculator.batch_size', 5)
    max_memory = config.get('feature_calculator.max_memory_per_query', 1073741824)

    logger.info("Configuration:",
               universe_size=len(universe),
               universe=universe,
               batch_size=batch_size,
               max_memory_gb=round(max_memory / (1024**3), 2))

    # Test symbol chunking
    logger.info("\n" + "=" * 80)
    logger.info("Testing Symbol Chunking")
    logger.info("=" * 80)

    batches = FeatureCalculator._chunk_symbols(universe, batch_size)
    logger.info(f"Split {len(universe)} symbols into {len(batches)} batches:")
    for i, batch in enumerate(batches, 1):
        logger.info(f"  Batch {i}: {batch}")

    # Test feature calculator initialization
    logger.info("\n" + "=" * 80)
    logger.info("Testing Feature Calculator Initialization")
    logger.info("=" * 80)

    calc = FeatureCalculator(
        resolution="5s",
        horizon_minutes=15,
        waterline_delay_sec=2,
        poll_interval_sec=5.0
    )

    logger.info("FeatureCalculator created:",
               resolution=calc.resolution,
               batch_size=calc.batch_size,
               max_memory_gb=round(calc.max_memory_per_query / (1024**3), 2))

    # Start calculator and check symbol loading
    await calc.start()

    logger.info("FeatureCalculator started:",
               symbols_loaded=len(calc.symbols),
               symbols=calc.symbols)

    # Test SQL generation with batches
    logger.info("\n" + "=" * 80)
    logger.info("Testing SQL Generation with Batches")
    logger.info("=" * 80)

    if calc.symbols:
        test_batch = calc.symbols[:2]  # Use first 2 symbols for testing
        logger.info(f"Generating SQL for test batch: {test_batch}")

        sql = calc._build_feature_sql(symbols_batch=test_batch)

        # Check if symbol filter is in SQL
        if "symbol IN" in sql:
            logger.info("✅ Symbol filter correctly added to SQL")
            # Extract the symbol filter line
            for line in sql.split('\n'):
                if 'symbol IN' in line:
                    logger.info(f"   Filter: {line.strip()}")
        else:
            logger.error("❌ Symbol filter NOT found in SQL")

        # Check memory limit
        if f"max_memory_usage={calc.max_memory_per_query}" in sql:
            logger.info("✅ Memory limit correctly set in SQL")
        else:
            logger.error("❌ Memory limit NOT correctly set in SQL")

    # Stop calculator
    await calc.stop()
    logger.info("FeatureCalculator stopped")

    logger.info("\n" + "=" * 80)
    logger.info("Test Complete")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_batch_processing())
