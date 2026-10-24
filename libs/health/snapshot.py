import json
import re
import argparse # For parsing script defaults, not command line
import os
import clickhouse_connect
from typing import Dict, List, Any, Literal, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path


HealthStatus = Literal["healthy", "degraded", "blocked"]


@dataclass
class BarHealthStatus:
    """Bars (market data) health status"""
    status: HealthStatus
    latest_ts: Optional[str] = None
    age_hours: float = 0.0
    missing_symbols: List[str] = field(default_factory=list)
    coverage_pct: float = 0.0
    issues: List[str] = field(default_factory=list)


@dataclass
class FeatureHealthStatus:
    """Features health status"""
    status: HealthStatus
    latest_ts: Optional[str] = None
    age_hours: float = 0.0
    null_ratio: float = 0.0
    rebuild_needed: bool = False
    issues: List[str] = field(default_factory=list)


@dataclass
class LabelSymbolStatus:
    """Single symbol's label status"""
    symbol: str
    horizon_min: int
    rows: int = 0
    null_ratio: float = 0.0
    zero_ratio: float = 0.0
    age_hours: float = 0.0
    std_bps: float = 0.0
    mean_magnitude: float = 0.0
    status: HealthStatus = "healthy"
    issues: List[str] = field(default_factory=list)


@dataclass
class LabelHealthStatus:
    """Overall labels health status"""
    status: HealthStatus
    by_symbol: Dict[str, LabelSymbolStatus] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfigHealthStatus:
    """Configuration health status"""
    status: HealthStatus
    problems: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return asdict(self)


@dataclass
class SystemHealthSnapshot:
    """Complete system health snapshot"""
    timestamp: str
    symbols: List[str]
    horizons: List[int]

    bars: BarHealthStatus
    features: FeatureHealthStatus
    labels: LabelHealthStatus
    config: ConfigHealthStatus

    overall_status: HealthStatus = "healthy"
    blocking_issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemHealthSnapshot":
        """Construct from dict"""
        # TODO: Implement proper deserialization with nested dataclasses
        return cls(**data)


def build_system_health(
    symbols: List[str],
    horizons: List[int],
    table: str = "trading.features_1m_with_labels"
) -> SystemHealthSnapshot:
    """
    Build system health snapshot (unified entry point)
    """
    cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    # 1. Bars health check
    bars_status = _check_bars_health(cli, symbols)

    # 2. Features health check
    features_status = _check_features_health(cli, symbols, table)

    # 3. Labels health check
    labels_status = _check_labels_health(cli, symbols, horizons, table)

    # 4. Config health check
    config_status = check_ml_config_health_lib()

    # Aggregate overall status
    overall_status: HealthStatus = "healthy"
    blocking_issues = []

    if any(s.status == "blocked" for s in [bars_status, features_status, labels_status, config_status]):
        overall_status = "blocked"
    elif any(s.status == "degraded" for s in [bars_status, features_status, labels_status, config_status]):
        overall_status = "degraded"

    if bars_status.status == "blocked":
        blocking_issues.append(f"Bars: Missing symbols: {bars_status.missing_symbols}")

    if features_status.status == "blocked":
        blocking_issues.append(f"Features: Stale or high NULL ratio")
    
    if labels_status.status == "blocked":
        blocking_issues.extend([
            f"Labels: {sym_status.symbol}: {', '.join(sym_status.issues)}"
            for sym_status in labels_status.by_symbol.values()
            if sym_status.status == "blocked"
        ])
    
    if config_status.status == "blocked":
        blocking_issues.extend([p['message'] for p in config_status.problems])


    return SystemHealthSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        symbols=symbols,
        horizons=horizons,
        bars=bars_status,
        features=features_status,
        labels=labels_status,
        config=config_status,
        overall_status=overall_status,
        blocking_issues=blocking_issues
    )


def _check_bars_health(cli, symbols: List[str]) -> BarHealthStatus:
    """Check health of bars data"""
    # This is a simplified check. A real implementation would be more thorough.
    status: HealthStatus = "healthy"
    latest_ts = None
    age_hours = 0.0
    missing_symbols = []
    issues = []

    # Configurable thresholds with holiday/weekend relaxation
    def _is_holiday_or_weekend() -> bool:
        if os.environ.get("HOLIDAY_MODE", "").lower() in ("1", "true", "yes", "on"):
            return True
        return datetime.now(timezone.utc).weekday() >= 5

    age_block_hours = float(os.environ.get("HEALTH_BARS_BLOCK_HOURS", 24))
    age_degrade_hours = float(os.environ.get("HEALTH_BARS_DEGRADE_HOURS", 4))
    holiday_age_multiplier = float(os.environ.get("HOLIDAY_AGE_MULTIPLIER", 2.0))
    if _is_holiday_or_weekend():
        age_block_hours *= holiday_age_multiplier
        age_degrade_hours *= holiday_age_multiplier

    try:
        # Check latest timestamp for each symbol
        query = f"""
        SELECT symbol, max(ts) as latest_ts
        FROM trading.bars_1m
        WHERE symbol IN {tuple(symbols)}
        GROUP BY symbol
        """
        result = cli.query(query)
        
        found_symbols = {row['symbol'] for row in result.result_rows}
        missing_symbols = list(set(symbols) - found_symbols)
        
        if missing_symbols:
            status = "blocked"
            issues.append(f"Missing symbols: {missing_symbols}")

        if result.result_rows:
            latest_ts_obj = min(row['latest_ts'] for row in result.result_rows)
            latest_ts = latest_ts_obj.isoformat()
            age_seconds = (datetime.now(timezone.utc) - latest_ts_obj).total_seconds()
            age_hours = age_seconds / 3600

            if age_hours > age_block_hours:
                status = "blocked"
                issues.append(f"Data is {age_hours:.1f} hours old")
            elif age_hours > age_degrade_hours:
                status = "degraded"
                issues.append(f"Data is {age_hours:.1f} hours old")

    except Exception as e:
        status = "blocked"
        issues.append(f"Error checking bars health: {e}")

    return BarHealthStatus(
        status=status,
        latest_ts=latest_ts,
        age_hours=age_hours,
        missing_symbols=missing_symbols,
        coverage_pct=(len(symbols) - len(missing_symbols)) / len(symbols) if symbols else 0.0,
        issues=issues
    )

def _check_features_health(cli, symbols: List[str], table: str) -> FeatureHealthStatus:
    """Check health of features data"""
    status: HealthStatus = "healthy"
    latest_ts = None
    age_hours = 0.0
    null_ratio = 0.0
    issues = []

    def _is_holiday_or_weekend() -> bool:
        if os.environ.get("HOLIDAY_MODE", "").lower() in ("1", "true", "yes", "on"):
            return True
        return datetime.now(timezone.utc).weekday() >= 5

    age_block_hours = float(os.environ.get("HEALTH_FEATURE_BLOCK_HOURS", 24))
    age_degrade_hours = float(os.environ.get("HEALTH_FEATURE_DEGRADE_HOURS", 4))
    null_thresh = float(os.environ.get("HEALTH_FEATURE_NULL_RATIO", 0.5))
    holiday_age_multiplier = float(os.environ.get("HOLIDAY_AGE_MULTIPLIER", 2.0))
    if _is_holiday_or_weekend():
        age_block_hours *= holiday_age_multiplier
        age_degrade_hours *= holiday_age_multiplier

    try:
        query = f"""
        SELECT
            max(ts) as latest_ts,
            avg(isNull(vwap)) as null_ratio
        FROM {table}
        WHERE symbol IN {tuple(symbols)}
        """
        result = cli.query(query)
        if result.result_rows:
            row = result.result_rows[0]
            latest_ts_obj = row['latest_ts']
            if latest_ts_obj:
                latest_ts = latest_ts_obj.isoformat()
                age_seconds = (datetime.now(timezone.utc) - latest_ts_obj).total_seconds()
                age_hours = age_seconds / 3600
                if age_hours > age_block_hours:
                    status = "blocked"
                    issues.append(f"Features are {age_hours:.1f} hours old")
                elif age_hours > age_degrade_hours:
                    status = "degraded"
                    issues.append(f"Features are {age_hours:.1f} hours old")
            
            null_ratio = row['null_ratio']
            if null_ratio > null_thresh:
                status = "blocked"
                issues.append(f"High NULL ratio: {null_ratio:.1%}")

    except Exception as e:
        status = "blocked"
        issues.append(f"Error checking features health: {e}")

    return FeatureHealthStatus(
        status=status,
        latest_ts=latest_ts,
        age_hours=age_hours,
        null_ratio=null_ratio,
        issues=issues
    )

def _label_thresholds() -> Dict[str, float]:
    """Load label thresholds from env with holiday/weekend relaxation."""
    max_null_ratio = float(os.environ.get("HEALTH_LABEL_MAX_NULL_RATIO", 0.1))
    min_std_bps = float(os.environ.get("HEALTH_LABEL_MIN_STD_BPS", 10))
    max_age_hours = float(os.environ.get("HEALTH_LABEL_MAX_AGE_HOURS", 48))

    if os.environ.get("HOLIDAY_MODE", "").lower() in ("1", "true", "yes", "on") or datetime.now(timezone.utc).weekday() >= 5:
        mult = float(os.environ.get("HOLIDAY_AGE_MULTIPLIER", 2.0))
        max_age_hours *= mult
        # Optionally relax std threshold on holidays
        min_std_bps = float(os.environ.get("HOLIDAY_LABEL_MIN_STD_BPS", min_std_bps))

    return {
        "max_null_ratio": max_null_ratio,
        "min_std_bps": min_std_bps,
        "max_age_hours": max_age_hours,
    }


def _check_labels_health(
    cli,
    symbols: List[str],
    horizons: List[int],
    table: str
) -> LabelHealthStatus:
    """Check health of labels data"""
    by_symbol = {}
    overall_status: HealthStatus = "healthy"
    thresholds = _label_thresholds()
    summary_data = {
        "overall_ready": True,
        "labels_checked": 0,
        "labels_missing_data": 0,
        "labels_high_null": 0,
        "labels_low_std": 0,
        "high_null_symbols": [], # Track symbols with high nulls
        "labels_stale": 0,
        "thresholds": thresholds
    }
    
    for symbol in symbols:
        for horizon in horizons:
            label_col = f"label_h{horizon}"
            key = f"{symbol}_h{horizon}"
            
            # --- Check for label column existence in the target table ---
            # This is critical to avoid ClickHouse query errors if the label column is simply missing.
            label_col_exists = False
            try:
                # Use system.columns to check for label column metadata
                meta_query = f"""
                SELECT 1 FROM system.columns
                WHERE database = currentDatabase() AND table = splitByChar('.', %(table)s)[2] AND name = %(label_col)s
                LIMIT 1
                """
                meta_result = cli.query(meta_query, parameters={"table": table, "label_col": label_col})
                label_col_exists = bool(meta_result.result_rows)
            except Exception as e:
                # Log error but don't block. Assume column doesn't exist or other issue.
                print(f"Error checking for label column {label_col} in {table}: {e}") # TODO: use proper logger
            
            if not label_col_exists:
                issues = [f"Label column '{label_col}' does not exist in table '{table}'"]
                by_symbol[key] = LabelSymbolStatus(symbol=symbol, horizon_min=horizon, status="blocked", issues=issues)
                summary_data["overall_ready"] = False
                summary_data["labels_missing_data"] += 1
                continue # Skip further checks for this symbol/horizon combination
            # --- End label column existence check ---

            try:
                # Check label stats
                query = f"""
                SELECT
                    count() as total_rows,
                    countIf({label_col} IS NULL) / count() as null_ratio,
                    countIf(abs({label_col}) < 0.001) / count() as zero_ratio,
                    stddevPop({label_col}) * 10000 as std_bps,
                    avg(abs({label_col})) as mean_magnitude,
                    max(ts) as latest_ts
                FROM {table}
                WHERE symbol = %(symbol)s
                    AND ts >= now() - INTERVAL 7 DAY  -- ⚡️ 修改这里：从 90 天改为 7 天
                    AND toHour(ts) >= 9
                    AND toHour(ts) < 16
                """
                result = cli.query(query, parameters={'symbol': symbol})
                
                if not result.result_rows or result.result_rows[0]['total_rows'] == 0:
                    by_symbol[key] = LabelSymbolStatus(symbol=symbol, horizon_min=horizon, status="blocked", issues=["No data in last 7 days"])
                    summary_data["overall_ready"] = False
                    summary_data["labels_missing_data"] += 1
                    continue
                
                row = result.result_rows[0]
                rows = row['total_rows']
                null_ratio = row['null_ratio'] if row['null_ratio'] is not None else 1.0
                zero_ratio = row['zero_ratio'] if row['zero_ratio'] is not None else 0.0
                std_bps = row['std_bps'] if row['std_bps'] is not None else 0.0
                mean_magnitude = row['mean_magnitude'] if row['mean_magnitude'] is not None else 0.0

                latest_ts_obj = row['latest_ts']
                age_hours = (datetime.now(timezone.utc) - latest_ts_obj).total_seconds() / 3600 if latest_ts_obj else 999

                status: HealthStatus = "healthy"
                issues = []
                
                if null_ratio > summary_data["thresholds"]["max_null_ratio"]:
                    status = "blocked"
                    issues.append(f"High NULL ratio ({null_ratio:.1%})")
                    if symbol not in summary_data["high_null_symbols"]:
                        summary_data["high_null_symbols"].append(symbol)
                    summary_data["labels_high_null"] += 1
                    
                if std_bps < summary_data["thresholds"]["min_std_bps"]:
                    if status == "healthy": status = "degraded"
                    issues.append(f"Low standard deviation ({std_bps:.1f} bps)")
                    summary_data["labels_low_std"] += 1

                if age_hours > summary_data["thresholds"]["max_age_hours"]:
                    if status == "healthy": status = "degraded"
                    issues.append(f"Labels are stale ({age_hours:.1f} hours old)")
                    summary_data["labels_stale"] += 1

                if status == "blocked":
                    summary_data["overall_ready"] = False


                by_symbol[key] = LabelSymbolStatus(
                    symbol=symbol,
                    horizon_min=horizon,
                    rows=rows,
                    null_ratio=null_ratio,
                    zero_ratio=zero_ratio,
                    age_hours=age_hours,
                    std_bps=std_bps,
                    mean_magnitude=mean_magnitude,
                    status=status,
                    issues=issues
                )
                summary_data["labels_checked"] += 1

            except Exception as e:
                by_symbol[key] = LabelSymbolStatus(symbol=symbol, horizon_min=horizon, status="blocked", issues=[f"Query failed: {e}"])
                summary_data["overall_ready"] = False
                summary_data["labels_missing_data"] += 1
    
    if any(s.status == "blocked" for s in by_symbol.values()):
        overall_status = "blocked"
    elif any(s.status == "degraded" for s in by_symbol.values()):
        overall_status = "degraded"

    return LabelHealthStatus(
        status=overall_status,
        by_symbol=by_symbol,
        summary=summary_data
    )



def get_label_columns(cli, table: str = "trading.features_1m_with_labels") -> List[str]:
    """
    获取数据库中所有label列
    """
    table_name = table.split('.')[-1]
    safe_settings = {
        "max_memory_usage": int(os.environ.get("CH_LABEL_QUERY_MEM", 200_000_000)),  # tighten to 200MB
        "max_threads": int(os.environ.get("CH_LABEL_QUERY_THREADS", 1)),
        "use_uncompressed_cache": 0,
    }
    query = f"""
    SELECT name
    FROM system.columns
    WHERE database = 'trading'
        AND table = '{table_name}'
        AND name LIKE 'label%'
    ORDER BY name
    """
    try:
        result = cli.query(query, settings=safe_settings)
    except Exception:
        # Best-effort fallback without settings
        result = cli.query(query)
    
    # Handle both list of dicts and list of tuples
    rows = []
    if hasattr(result, 'result_rows'):
        rows = result.result_rows  # clickhouse-connect client
    elif isinstance(result, list):
        rows = result  # clickhouse-driver client

    output = []
    for row in rows:
        if isinstance(row, dict):
            output.append(row['name'])
        elif isinstance(row, (list, tuple)) and len(row) > 0:
            output.append(row[0])
            
    return output


def check_oof_policies_config_lib(cli, config_file: str = "config/oof_policies.json") -> Dict[str, Any]:
    """
    检查 oof_policies.json 配置健康度
    """
    issues = []
    warnings = []
    try:
        with open(config_file) as f:
            oof_config = json.load(f)
    except FileNotFoundError:
        issues.append({
            "type": "config_file_missing",
            "severity": "critical",
            "message": f"Config file not found: {config_file}"
        })
        return {
            "config_file": config_file, "status": "error", "issues": issues, "warnings": warnings
        }
    except json.JSONDecodeError as e:
        issues.append({
            "type": "config_parse_error",
            "severity": "critical",
            "message": f"Failed to parse JSON: {e}"
        })
        return {
            "config_file": config_file, "status": "error", "issues": issues, "warnings": warnings
        }

    horizon_min = oof_config.get("defaults", {}).get("horizon_min", 5)
    label_col = f"label_h{horizon_min}"

    # 检查label列是否存在
    existing_labels = get_label_columns(cli)

    if label_col not in existing_labels:
        issues.append({
            "type": "schema_mismatch",
            "severity": "critical",
            "config_file": config_file,
            "expected_label": label_col,
            "existing_labels": existing_labels,
            "message": f"Config requires {label_col}, but only {existing_labels} exist in database",
            "fix": {
                "option1": f"Generate missing label: venv/bin/python tools/generate_label.py --horizon {horizon_min}",
                "option2": f"Switch to existing label: Update horizon_min to {int(existing_labels[0].split('_h')[1]) if existing_labels else 'N/A'}"
            }
        })
    return {
        "config_file": config_file, "horizon_min": horizon_min, "expected_label": label_col,
        "existing_labels": existing_labels, "issues": issues, "warnings": warnings
    }


def check_training_script_config_lib(cli, script_file: str = "pipelines/train_xgb_with_oof.py") -> Dict[str, Any]:
    """
    检查训练脚本的默认label配置
    """
    issues = []
    warnings = []
    script_path = Path(script_file)
    if not script_path.exists():
        issues.append({
            "type": "script_missing",
            "severity": "high",
            "message": f"Training script not found: {script_file}"
        })
        return {
            "script_file": script_file, "issues": issues, "warnings": warnings
        }

    script_content = script_path.read_text()
    # 查找 label_col 默认值
    label_col_match = re.search(r'label_col:\s*str\s*=\s*["\']([^"\']+)["\']', script_content)

    if label_col_match:
        default_label = label_col_match.group(1)
        # 检查是否存在
        existing_labels = get_label_columns(cli)
        if default_label not in existing_labels:
            warnings.append({
                "type": "script_default_mismatch",
                "severity": "medium",
                "script_file": script_file,
                "default_label": default_label,
                "existing_labels": existing_labels,
                "message": f"Script default label {default_label} doesn't exist",
                "fix": f"Update script or generate {default_label}"
            })
    return {
        "script_file": script_file,
        "default_label": label_col_match.group(1) if label_col_match else None,
        "issues": issues, "warnings": warnings
    }


def check_ml_config_health_lib(
    oof_config_file: str = "config/oof_policies.json",
    training_script_file: str = "pipelines/train_xgb_with_oof.py",
    cli: Optional[Any] = None
) -> ConfigHealthStatus:
    """
    Check ML configuration health and consistency.
    This is the unified function to be used by both tools and libs.
    """
    problems = []
    warnings = []
    
    # If no client is passed, create a new one.
    if cli is None:
        cli = clickhouse_connect.get_client(host='localhost', port=8123, database='trading')

    oof_check = check_oof_policies_config_lib(cli, oof_config_file)
    problems.extend(oof_check.get("issues", []))
    warnings.extend(oof_check.get("warnings", []))

    script_check = check_training_script_config_lib(cli, training_script_file)
    problems.extend(script_check.get("issues", []))
    warnings.extend(script_check.get("warnings", []))

    # Cross-check consistency
    if oof_check.get("expected_label") and script_check.get("default_label"):
        if oof_check["expected_label"] != script_check["default_label"]:
            warnings.append({
                "type": "config_inconsistency",
                "severity": "medium",
                "message": f"oof_policies.json expects {oof_check['expected_label']}, but script defaults to {script_check['default_label']}",
                "fix": "Align configurations"
            })

    status: HealthStatus = "blocked" if problems else ("degraded" if warnings else "healthy")

    return ConfigHealthStatus(
        status=status,
        problems=problems,
        warnings=warnings,
        checks=[oof_check, script_check]
    )
