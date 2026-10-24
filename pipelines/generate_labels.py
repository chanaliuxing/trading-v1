"""Generate forward return labels for ML training."""
import argparse
from clickhouse_driver import Client


def generate_labels(
    host: str = "localhost",
    port: int = 9000,
    database: str = "trading",
    source_table: str = "features_1m",
    target_table: str = "features_1m_with_labels",
    symbols: list[str] = None,
    label_horizons: list[int] = None,
):
    """Generate labels from forward returns.

    Args:
        source_table: Table with features (features_1m)
        target_table: Output table with labels
        symbols: List of symbols to process
        label_horizons: Forward periods in minutes (e.g., [5, 15, 30])
    """
    if symbols is None:
        symbols = ['TSLA', 'AAPL', 'NVDA']
    if label_horizons is None:
        label_horizons = [5, 15, 30]

    client = Client(host=host, port=port, database=database)

    # Create target table with label columns
    # ✅ FIX: Use leadInFrame instead of lead, and handle edge cases
    label_calculations = []
    for h in label_horizons:
        label_calc = f"""
        CASE
            WHEN f.close = 0 THEN NULL
            WHEN f.close IS NULL THEN NULL
            WHEN f.lead_close_{h} = 0 THEN NULL
            WHEN f.lead_close_{h} IS NULL THEN NULL
            ELSE (f.lead_close_{h} - f.close) / f.close
        END AS label_h{h}
        """
        label_calculations.append(label_calc.strip())

    # Build subquery with leadInFrame for each horizon
    lead_cols = []
    for h in label_horizons:
        lead_cols.append(
            f"leadInFrame(f.close, {h}) OVER (PARTITION BY f.symbol ORDER BY f.ts "
            f"ROWS BETWEEN CURRENT ROW AND {h} FOLLOWING) AS lead_close_{h}"
        )

    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {target_table}
    ENGINE = ReplacingMergeTree()
    ORDER BY (symbol, ts)
    AS
    SELECT
        sub.*,
        {", ".join(label_calculations)}
    FROM (
        SELECT
            f.*,
            {", ".join(lead_cols)}
        FROM {source_table} f
        WHERE f.symbol IN {tuple(symbols) if len(symbols) > 1 else f"('{symbols[0]}')"}
    ) sub
    """

    print(f"Creating table: {target_table}")
    print(f"With labels for horizons: {label_horizons} minutes")
    print(f"Symbols: {symbols}")

    try:
        # Drop existing table if exists
        client.execute(f"DROP TABLE IF EXISTS {target_table}")
        print(f"Dropped existing table: {target_table}")

        # Create new table with labels
        client.execute(create_table_sql)
        print(f"✓ Created table: {target_table}")

        # Verify
        count = client.execute(f"SELECT count(*) FROM {target_table}")[0][0]
        print(f"✓ Total rows: {count:,}")

        # Show sample (skip if horizons don't include h5)
        sample_cols = ["symbol", "ts", "close"] + [f"label_h{h}" for h in label_horizons[:3]]
        first_label_col = f"label_h{label_horizons[0]}"
        sample = client.execute(f"""
            SELECT {', '.join(sample_cols)}
            FROM {target_table}
            WHERE {first_label_col} IS NOT NULL
            LIMIT 5
        """)
        print("\nSample rows:")
        for row in sample:
            print(f"  {row}")

        # Statistics (dynamically build based on horizons)
        stats_select = ["symbol", "count(*) as total"]
        for h in label_horizons:
            stats_select.append(f"countIf(label_h{h} > 0) as h{h}_positive")
            stats_select.append(f"round(avg(label_h{h}), 6) as avg_h{h}")

        first_label_col = f"label_h{label_horizons[0]}"
        stats = client.execute(f"""
            SELECT
                {', '.join(stats_select)}
            FROM {target_table}
            WHERE {first_label_col} IS NOT NULL
            GROUP BY symbol
        """)
        print("\nLabel statistics:")
        header = "Symbol | Total"
        for h in label_horizons:
            header += f" | H{h}+ | Avg(H{h})"
        print(header)

        for row in stats:
            line = f"{row[0]:6} | {row[1]:5}"
            for i, h in enumerate(label_horizons):
                pos_idx = 2 + i * 2
                avg_idx = 3 + i * 2
                line += f" | {row[pos_idx]:4} | {row[avg_idx]:8.4f}"
            print(line)

        print(f"\n✓ SUCCESS: Labels generated in {target_table}")
        return True

    except Exception as e:
        print(f"✗ ERROR: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate forward return labels")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--database", default="trading")
    parser.add_argument("--source-table", default="features_1m")
    parser.add_argument("--target-table", default="features_1m_with_labels")
    parser.add_argument("--symbols", nargs="+", default=["TSLA", "AAPL", "NVDA"])
    parser.add_argument("--horizons", nargs="+", type=int, default=[5, 15, 30])

    args = parser.parse_args()

    generate_labels(
        host=args.host,
        port=args.port,
        database=args.database,
        source_table=args.source_table,
        target_table=args.target_table,
        symbols=args.symbols,
        label_horizons=args.horizons,
    )
