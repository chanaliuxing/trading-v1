"""LLM Auto Trading Bot - Main trading loop

V2.0: Enhanced with time awareness, memory system, and adaptive learning
"""

import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from .ny_time import now_ny, NY_TZ
import traceback
import logging
import sys
import os
import time
from pathlib import Path

from .providers import BaseLLMProvider, LLMResponse
from .providers.openai_provider import OpenAIProvider
from .providers.gemini_provider import GeminiProvider
from .providers.deepseek_provider import DeepSeekProvider
from .providers.deepseek_nvidia_provider import DeepSeekNvidiaProvider
from .providers.ollama_provider import OllamaProvider
from llm_trader.data.market_data_fetcher import MarketDataFetcher
from llm_trader.trading.ibkr_executor import IBKRExecutor, OrderAction
from .portfolio.portfolio_manager import PortfolioManager
from llm_trader.utils.config import Config
try:
    from libs.skills.runtime import SkillRuntime
except Exception:
    SkillRuntime = None

# V2.0: Import new modules (with fallback for backwards compatibility)
try:
    from llm_trader.utils.time_awareness import TimeAwareness
    from llm_trader.utils.memory_system import MemorySystem
    from llm_trader.utils.prompt_builder import PromptBuilder
    from llm_trader.utils.prompt_builder_r1 import R1PromptBuilder
    V2_AVAILABLE = True
except ImportError:
    TimeAwareness = None
    MemorySystem = None
    PromptBuilder = None
    R1PromptBuilder = None
    V2_AVAILABLE = False

# V2.5: Multi-timeframe aggregator imports
try:
    from llm_trader.realtime_aggregator import RealtimeAggregator
    from llm_trader.higher_tf_context import HigherTFContextLoader
    from llm_trader.time_manager import TimeManager
    from llm_trader.prompt_builder_universal import UniversalPromptBuilder
    from llm_trader.prompt_builder_compact import CompactPromptBuilder
    AGGREGATOR_AVAILABLE = True
except ImportError:
    RealtimeAggregator = None
    HigherTFContextLoader = None
    TimeManager = None
    UniversalPromptBuilder = None
    CompactPromptBuilder = None
    AGGREGATOR_AVAILABLE = False

# V2.7: Sentiment analysis system
try:
    from llm_trader.sentiment.sentiment_engine import SentimentEngine
    SENTIMENT_AVAILABLE = True
except ImportError:
    SentimentEngine = None
    SENTIMENT_AVAILABLE = False


def setup_logging(provider_name: str = "llm_trader") -> tuple:
    """Setup logging to both file and console

    Args:
        provider_name: Name of the provider (used in log filename)

    Returns:
        Tuple of (main_logger, api_log_file_path)
    """
    # Create logs directory if it doesn't exist
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Create log filenames with timestamp
    timestamp = now_ny().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{provider_name}_{timestamp}.log"
    api_log_file = log_dir / f"{provider_name}_{timestamp}_api_detailed.txt"

    # ===== Main Logger =====
    logger = logging.getLogger("LLMTraderBot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)

    # Main file handler
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)

    # Add handlers
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    # Create API detailed log file (will write directly, not via logging)
    api_log_file.touch()

    # Log initial message
    logger.info(f"Logging initialized. Main log: {log_file}")
    logger.info(f"                      API detailed log: {api_log_file}")

    return logger, str(api_log_file)


class LLMTraderBot:
    """LLM-powered auto trading bot"""

    def __init__(
        self,
        provider_name: str,
        symbols: List[str],
        config: Config,
        initial_cash: float = 50000.0,
        dry_run: bool = True,
        enable_memory: bool = False,  # V2.0: Enable memory system
        enable_time_awareness: bool = False,  # V2.0: Enable time awareness
        enable_adaptive_frequency: bool = False,  # V2.0: Adaptive polling
        # V2.5: Aggregator parameters
        use_aggregator: bool = True,
        prefill_5s_mins: int = 20,
        higher_tf_refresh_sec: int = 300,
        close_at: Optional[str] = None,
        close_before: Optional[int] = None,
        forbid_new_positions_before: int = 5,
        start_trading_at: Optional[str] = None,
        stop_trading_at: Optional[str] = None,
        # V2.6: Compact prompt system (DEFAULT: ON for 50-81% token savings)
        use_compact_prompt: bool = True,
        reflection_interval_minutes: int = 5,
        # V2.2: Short selling (DEFAULT: OFF for safety)
        allow_short_selling: bool = False,
        # V2.3: Ledger system (DEFAULT: NoopLedger)
        ledger: Optional[Any] = None,
        session_id: Optional[str] = None,
    ):
        """Initialize LLM trader bot

        Args:
            provider_name: LLM provider (openai, gemini, deepseek, deepseek_nvidia)
            symbols: List of symbols to trade
            config: Configuration object
            initial_cash: Initial cash amount
            dry_run: If True, don't execute real trades (simulation only)
            enable_memory: Enable V2.0 memory system
            enable_time_awareness: Enable V2.0 time awareness
            enable_adaptive_frequency: Enable V2.0 adaptive polling frequency
            use_aggregator: Enable V2.5 realtime multi-timeframe aggregator
            prefill_5s_mins: Minutes of 5s bars to prefill on startup
            higher_tf_refresh_sec: Seconds between 1h/1d context refresh
            close_at: Fixed EOD flatten time (e.g., "15:55")
            close_before: Minutes before market close to flatten
            forbid_new_positions_before: Minutes before close to forbid new positions
            use_compact_prompt: Enable V2.6 compact prompt system (83% token reduction)
            reflection_interval_minutes: Minutes between reflection prompts (default: 5)
            allow_short_selling: Allow short selling (default: False)
            ledger: Ledger manager for session-based accounting (default: NoopLedger)
            session_id: Unique session identifier for this bot instance
        """
        self.provider_name = provider_name
        self.symbols = symbols
        self.config = config
        self.initial_cash = initial_cash
        self.dry_run = dry_run

        # V2.3: Ledger system
        from .ledger_manager import NoopLedger
        self.ledger = ledger if ledger is not None else NoopLedger()
        self.session_id = session_id

        # Setup logging (auto-creates logs/provider_timestamp.log and _api_detailed.txt)
        self.logger, self.api_log_file = setup_logging(provider_name)

        # V2.0 flags
        self.enable_memory = enable_memory and V2_AVAILABLE
        self.enable_time_awareness = enable_time_awareness and V2_AVAILABLE
        self.enable_adaptive_frequency = enable_adaptive_frequency and V2_AVAILABLE

        # V2.0: Initialize new modules (if enabled)
        self.time_awareness = TimeAwareness() if self.enable_time_awareness else None
        self.memory_system = MemorySystem() if self.enable_memory else None
        self.prompt_builder = None

        if self.enable_memory and self.time_awareness:
            # Use R1-specific prompt builder for deepseek_nvidia, standard for others
            if self.provider_name == "deepseek_nvidia" and R1PromptBuilder:
                self.prompt_builder = R1PromptBuilder(self.time_awareness, self.memory_system)
                self.logger.info("[V2] Using R1-optimized prompt builder for DeepSeek R1")
            else:
                self.prompt_builder = PromptBuilder(self.time_awareness, self.memory_system)

        # V2.5: Aggregator setup
        self.use_aggregator = use_aggregator and AGGREGATOR_AVAILABLE
        self.prefill_5s_mins = prefill_5s_mins
        self.higher_tf_refresh_sec = higher_tf_refresh_sec

        # V2.5: Initialize aggregator components
        if self.use_aggregator:
            self.aggregator = RealtimeAggregator()
            self.higher_loader = None  # Will be initialized in start()
            self.higher_cache = {}
            self.last_higher_refresh = 0

            # Time manager for EOD/opening hours logic
            self.time_manager = TimeManager(
                close_at=close_at,
                close_before_min=close_before,
                forbid_new_positions_before_min=forbid_new_positions_before,
                start_trading_at=start_trading_at,
                stop_trading_at=stop_trading_at,
            )

            # Read prompt overrides from environment
            prompt_id = os.getenv("LLM_PROMPT_ID", "").strip() or None
            prompt_file = os.getenv("LLM_PROMPT_FILE", "").strip() or None
            prompt_text = None
            if prompt_file:
                try:
                    prompt_text = Path(prompt_file).read_text("utf-8")
                    self.logger.info(f"[Prompt Override] Loaded prompt from file: {prompt_file}")
                except Exception as e:
                    self.logger.warning(f"[Prompt Override] Failed to read prompt file: {e}")
                    prompt_text = None
            if prompt_id:
                self.logger.info(f"[Prompt Override] Using prompt ID: {prompt_id}")

            # Universal prompt builder
            self.universal_prompt_builder = UniversalPromptBuilder(
                json_mode="schema" if provider_name in ("openai", "deepseek_nvidia") else "object",
                style="reasoning",
                prompt_id=prompt_id,
                prompt_text=prompt_text,
            )
        else:
            self.aggregator = None
            self.time_manager = None
            self.universal_prompt_builder = None

        # V2.6: Compact prompt system
        self.use_compact_prompt = use_compact_prompt and AGGREGATOR_AVAILABLE
        self.reflection_interval_minutes = reflection_interval_minutes
        self.compact_prompt_builder = None
        self.last_reflection_cycle = 0

        if self.use_compact_prompt and CompactPromptBuilder:
            # Read prompt overrides from environment (reuse from above if already in aggregator scope)
            prompt_id = os.getenv("LLM_PROMPT_ID", "").strip() or None
            prompt_file = os.getenv("LLM_PROMPT_FILE", "").strip() or None
            prompt_text = None
            if prompt_file:
                try:
                    prompt_text = Path(prompt_file).read_text("utf-8")
                except Exception:
                    prompt_text = None

            # Create compact prompt builder for execution mode
            self.compact_prompt_builder = CompactPromptBuilder(
                mode="execution",
                adaptive_trim=True,
                max_tokens_budget=2000,
                prompt_id=prompt_id,
                prompt_text=prompt_text,
            )
            self.logger.info("[V2.6] Compact prompt system enabled (execution mode)")
            self.logger.info(f"[V2.6] Reflection interval: {reflection_interval_minutes} minutes")

        # V2.7: Sentiment analysis system
        self.sentiment_engine = None
        self.sentiment_cache = None
        self.sentiment_cache_timestamp = 0
        self.sentiment_refresh_interval = 3600  # 1 hour cache (news mostly pre-market)

        if SENTIMENT_AVAILABLE and SentimentEngine:
            try:
                self.sentiment_engine = SentimentEngine(
                    fear_weight=0.3,
                    flow_weight=0.4,
                    narrative_weight=0.3
                )
                self.logger.info("[V2.7] Sentiment analysis system enabled (3-dimensional)")
            except Exception as e:
                self.logger.warning(f"[V2.7] Failed to initialize sentiment engine: {e}")
                self.sentiment_engine = None
        else:
            self.logger.info("[V2.7] Sentiment analysis disabled (not available)")

        # Initialize components
        self.llm_provider = self._create_llm_provider()
        self.data_fetcher = MarketDataFetcher(
            ch_host=config.ch_host,
            ch_port=config.ch_port,
            ch_user=config.ch_user,
            ch_password=config.ch_password,
            ch_database=config.ch_database,
            redis_url=config.redis_url,
        )
        self.portfolio = PortfolioManager(
            initial_cash=initial_cash,
            memory_system=self.memory_system,  # V2.0: Pass memory system
            allow_short_selling=allow_short_selling,  # V2.2: Pass short selling flag
        )
        self.ibkr_executor = None if dry_run else IBKRExecutor(
            host=config.ib_host,
            port=config.ib_port,
            # client_id is auto-generated randomly (1000-9999) to avoid conflicts
            # with smart retry if the ID is already in use
        )

        # Log execution mode clearly
        if dry_run:
            self.logger.info("=" * 80)
            self.logger.info("🎮 DRY RUN MODE - Simulating trades (no real orders to IBKR)")
            self.logger.info("   To enable LIVE trading, remove --dry-run flag")
            self.logger.info("=" * 80)
        else:
            self.logger.info("=" * 80)
            self.logger.info("📡 LIVE TRADING MODE - Real orders will be sent to IBKR")
            self.logger.info(f"   IBKR: {config.ib_host}:{config.ib_port} (client_id will be auto-assigned)")
            self.logger.info("=" * 80)

        self.running = False
        self.cycle_count = 0
        self.last_reflection_time = None  # V2.0: Track last reflection

        # V2.8: Track session start equity for daily PnL calculation
        self.session_start_equity = initial_cash

        # V2.13: Lightweight pending order plans for dry-run/monitoring
        # Structure: {symbol: [{"side":"BUY|SELL","qty":int,"px":float,"expire_ts":float}]}
        self.pending_orders: Dict[str, List[Dict[str, Any]]] = {}

        # V2.12: Data health monitoring (stop LLM calls when data is stale)
        from datetime import timezone
        self.data_health = {
            "status": "HEALTHY",  # HEALTHY / DEGRADED / UNHEALTHY / RECOVERING
            "consecutive_failures": 0,
            "last_good_data_time": datetime.now(timezone.utc),
            "failure_count": 0,
            "recovery_checks_passed": 0,
        }

        self.data_health_config = {
            "stale_threshold_sec": 10,          # Data older than 10s is stale (RTH)
            "stale_threshold_sec_overnight": 30, # Relaxed threshold for OVERNIGHT session (low liquidity)
            "stale_threshold_sec_pre": 20,       # Relaxed threshold for PRE session
            "stale_threshold_sec_afterhours": 20, # Relaxed threshold for AFTERHOURS session
            "degraded_after_failures": 2,       # Enter DEGRADED after 2 consecutive failures
            "unhealthy_after_failures": 5,      # Enter UNHEALTHY after 5 consecutive failures (stop LLM)
            "recovery_checks_needed": 3,        # Need 3 consecutive good checks to recover
            "max_wait_time_sec": 300,           # Alert if no good data for 5 minutes
            "min_bars_required": 5,             # Minimum 1s bars required for healthy data
        }

        self.skill_runtime = self._init_skill_runtime()

    def _init_skill_runtime(self) -> Optional[SkillRuntime]:
        """Best-effort bootstrap for dynamic skills (fails open)."""
        if SkillRuntime is None:
            return None
        try:
            return SkillRuntime(registry_path="config/skill_registry.json", runs_dir="runs/ai_trader")
        except Exception:
            return None

    def _maybe_refresh_skill_runtime(self) -> None:
        if not self.skill_runtime:
            return
        try:
            self.skill_runtime.refresh()
        except Exception:
            pass

    @staticmethod
    def _decision_to_payload(decision: LLMResponse) -> Dict[str, Any]:
        return {
            "decision": decision.decision,
            "symbol": decision.symbol,
            "quantity": decision.quantity,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "horizon": getattr(decision, "horizon", None),
            "expected_edge_bps": getattr(decision, "expected_edge_bps", None),
            "provider_name": decision.provider_name if hasattr(decision, "provider_name") else None,
        }

    @staticmethod
    def _apply_payload_to_decision(decision: LLMResponse, payload: Dict[str, Any]) -> LLMResponse:
        decision.decision = payload.get("decision", decision.decision)
        decision.symbol = payload.get("symbol", decision.symbol)
        decision.quantity = payload.get("quantity", decision.quantity)
        decision.confidence = payload.get("confidence", decision.confidence)
        decision.reasoning = payload.get("reasoning", decision.reasoning)
        if "horizon" in payload:
            decision.horizon = payload.get("horizon")
        if "expected_edge_bps" in payload:
            decision.expected_edge_bps = payload.get("expected_edge_bps")
        if "provider_name" in payload:
            decision.provider_name = payload.get("provider_name")
        return decision

    def _apply_skill_decision_hooks(
        self,
        decision: LLMResponse,
        symbol: str,
        market_data: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        time_ctx: Dict[str, Any],
    ) -> LLMResponse:
        if not self.skill_runtime:
            return decision
        try:
            payload = self._decision_to_payload(decision)
            ctx = {
                "symbol": symbol,
                "provider": self.provider_name,
                "time_context": time_ctx,
                "market_data": market_data,
                "portfolio_state": portfolio_state,
            }
            patched = self.skill_runtime.apply_decision_hooks(payload, context=ctx)
            return self._apply_payload_to_decision(decision, patched)
        except Exception:
            return decision

    def _apply_skill_risk_hooks(
        self,
        decision: LLMResponse,
        symbol: str,
        portfolio_state: Dict[str, Any],
        time_ctx: Dict[str, Any],
    ) -> LLMResponse:
        if not self.skill_runtime:
            return decision
        try:
            payload = {
                "decision": self._decision_to_payload(decision),
                "symbol": symbol,
                "provider": self.provider_name,
                "portfolio_state": portfolio_state,
                "time_context": time_ctx,
            }
            patched = self.skill_runtime.apply_risk_hooks(payload, context={"symbol": symbol, "provider": self.provider_name})
            dec_patch = patched.get("decision", patched)
            if isinstance(dec_patch, dict):
                return self._apply_payload_to_decision(decision, dec_patch)
            return decision
        except Exception:
            return decision

    def _create_llm_provider(self) -> BaseLLMProvider:
        """Create LLM provider based on name"""
        llm_config = self.config.get_llm_config(self.provider_name)

        providers = {
            "openai": OpenAIProvider,
            "gemini": GeminiProvider,
            "deepseek": DeepSeekProvider,
            "deepseek_nvidia": DeepSeekNvidiaProvider,
            "ollama": OllamaProvider,
        }

        if self.provider_name not in providers:
            raise ValueError(f"Unknown provider: {self.provider_name}")

        provider_class = providers[self.provider_name]

        # Ollama needs base_url instead of api_key
        if self.provider_name == "ollama":
            return provider_class(
                model=llm_config["model"],
                base_url=llm_config.get("base_url", "http://localhost:11434"),
            )
        else:
            return provider_class(
                api_key=llm_config["api_key"],
                model=llm_config["model"],
            )

    async def start(self):
        """Start the trading bot"""
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"LLM Auto Trading Bot - {self.provider_name.upper()}")
        self.logger.info(f"{'='*80}")
        self.logger.info(f"Mode: {'DRY RUN (Simulation)' if self.dry_run else 'LIVE TRADING'}")
        self.logger.info(f"Initial Cash: ${self.initial_cash:,.2f}")
        self.logger.info(f"Symbols: {', '.join(self.symbols)}")
        self.logger.info(f"Provider: {self.provider_name} - {self.llm_provider.model}")
        self.logger.info(f"{'='*80}\n")

        # Connect to data sources
        await self.data_fetcher.connect()

        # V2.13: Load rescue orders if present (pending planned exits)
        try:
            from datetime import date as _D
            runs_dir = Path("runs/ai_trader") / _D.today().isoformat()
            ro = runs_dir / "rescue_orders.json"
            if ro.exists():
                import json as _J
                obj = _J.loads(ro.read_text("utf-8"))
                orders = obj.get("orders") or []
                self.pending_orders = {}
                import time as _t
                now_ts = _t.time()
                for o in orders:
                    sym = str(o.get("symbol") or "").upper()
                    if not sym:
                        continue
                    arr = self.pending_orders.get(sym, [])
                    try:
                        arr.append({
                            "side": str(o.get("side") or "SELL").upper(),
                            "qty": int(o.get("qty") or 0),
                            "px": float(o.get("px")) if o.get("px") is not None else None,
                            "mode": str(o.get("mode") or "limit").lower(),
                            "expire_ts": now_ts + int(o.get("tw_s") or 900),
                        })
                    except Exception:
                        continue
                    self.pending_orders[sym] = arr
                if self.pending_orders:
                    self.logger.info(f"Loaded rescue orders for {len(self.pending_orders)} symbols")
        except Exception as e:
            self.logger.info(f"Rescue orders load failed: {e}")

        # V2.5: Initialize higher TF loader (needs CH client)
        if self.use_aggregator:
            self.higher_loader = HigherTFContextLoader(self.data_fetcher.ch_client)

            # Prefill aggregator with historical 5s data
            if self.prefill_5s_mins > 0:
                self.logger.info(f"\n=== Prefilling aggregator with {self.prefill_5s_mins}m of 5s bars ===")
                for symbol in self.symbols:
                    try:
                        bars_5s = await self.data_fetcher.prefill_5s_from_clickhouse(
                            symbol, minutes=self.prefill_5s_mins
                        )
                        if bars_5s:
                            self.aggregator.seed_with_5s(symbol, bars_5s)
                            self.logger.info(f"  ✅ {symbol}: Prefilled with {len(bars_5s)} x 5s bars")
                        else:
                            self.logger.info(f"  ⚠️  {symbol}: No 5s bars available for prefill")
                    except Exception as e:
                        self.logger.info(f"  ❌ {symbol}: Prefill error: {e}")

        # Connect to IBKR if not dry run
        if not self.dry_run:
            await self.ibkr_executor.connect()

        # Health check LLM
        self.logger.info("Checking LLM provider health...")
        if await self.llm_provider.health_check():
            self.logger.info("✓ LLM provider is healthy\n")
        else:
            self.logger.info("✗ LLM provider health check failed\n")
            return

        self.running = True
        self.logger.info("Bot started. Press Ctrl+C to stop.\n")

    async def stop(self):
        """Stop the trading bot"""
        self.logger.info("\nStopping bot...")
        self.running = False

        # Close connections
        await self.data_fetcher.close()

        if not self.dry_run and self.ibkr_executor:
            await self.ibkr_executor.disconnect()

        # Save final state
        # Use ET time for consistency with logs
        et_now = now_ny()
        timestamp = et_now.strftime("%Y%m%d_%H%M%S")
        filepath = f"results/{self.provider_name}_final_{timestamp}.json"
        self.portfolio.save_state(filepath)

        self.logger.info(f"\nFinal Portfolio Summary:")
        self.logger.info(f"{'='*80}")
        state = self.portfolio.get_detailed_state()
        self.logger.info(f"Total Equity: ${state['total_equity']:,.2f}")
        self.logger.info(f"Total P&L: ${state['total_pnl']:,.2f} ({state['total_pnl_pct']:.2f}%)")
        self.logger.info(f"Cash: ${state['cash']:,.2f}")
        self.logger.info(f"Positions: {state['num_positions']}")
        self.logger.info(f"{'='*80}\n")

        # V2.3: Close session
        if hasattr(self, 'ledger') and self.session_id:
            self.ledger.close_session(
                session_id=self.session_id,
                status="CLOSED",
                reason="Normal shutdown",
            )

    def _check_data_health(self, market_data_all: Dict[str, Dict]) -> bool:
        """Check data health status (V2.12 Enhanced - 6 Layer Validation)

        Args:
            market_data_all: Market data for all symbols

        Returns:
            True if data is healthy enough for trading, False otherwise

        Validation layers:
        1. Timestamp exists and parseable
        2. Timestamp is fresh (< 10s old)
        3. Timestamp is advancing (not cached old data)
        4. Price is valid (> 0)
        5. Sufficient data bars (>= 5 for 1s bars)
        6. Volume present (warn only, not blocking)
        """
        from datetime import datetime, timezone

        # Always use UTC for time calculations to avoid timezone issues
        now_utc = datetime.now(timezone.utc)

        stale_count = 0
        fresh_count = 0
        stale_symbols = []

        for symbol in self.symbols:
            data = market_data_all.get(symbol, {})
            is_healthy = True
            failure_reason = None

            # === Layer 1: Timestamp exists ===
            data_timestamp_str = data.get("timestamp")
            if not data_timestamp_str:
                is_healthy = False
                failure_reason = "No timestamp in data"
                self.logger.warning(f"[{symbol}] ⚠️  {failure_reason}")
                stale_count += 1
                stale_symbols.append(symbol)
                continue

            try:
                # Parse ISO timestamp (may be in NY timezone from fetcher)
                data_timestamp = datetime.fromisoformat(data_timestamp_str)

                # Convert to UTC for consistent comparison
                if data_timestamp.tzinfo is None:
                    data_timestamp = data_timestamp.replace(tzinfo=timezone.utc)
                else:
                    data_timestamp = data_timestamp.astimezone(timezone.utc)

                # === Layer 2: Timestamp freshness ===
                data_age_sec = (now_utc - data_timestamp).total_seconds()

                # V2.13: Dynamic threshold based on trading session
                session_phase = self.time_manager.session_phase(datetime.now(NY_TZ))
                if session_phase == "OVERNIGHT":
                    threshold = self.data_health_config["stale_threshold_sec_overnight"]
                elif session_phase == "PRE":
                    threshold = self.data_health_config["stale_threshold_sec_pre"]
                elif session_phase == "AFTERHOURS":
                    threshold = self.data_health_config["stale_threshold_sec_afterhours"]
                else:
                    threshold = self.data_health_config["stale_threshold_sec"]

                if data_age_sec > threshold:
                    is_healthy = False
                    failure_reason = f"Stale: {data_age_sec:.1f}s old (threshold: {threshold}s for {session_phase})"
                    self.logger.warning(f"[{symbol}] ⚠️  {failure_reason}")

                # === Layer 3: Timestamp advancing check (防止缓存旧数据) ===
                last_timestamp_key = f"last_timestamp_{symbol}"
                last_timestamp = self.data_health.get(last_timestamp_key)

                if last_timestamp and is_healthy:
                    # Ensure last_timestamp is UTC
                    if isinstance(last_timestamp, datetime):
                        if last_timestamp.tzinfo is None:
                            last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)
                        else:
                            last_timestamp = last_timestamp.astimezone(timezone.utc)

                        # Check if timestamp is advancing
                        if data_timestamp <= last_timestamp:
                            is_healthy = False
                            failure_reason = f"Timestamp not advancing: {data_timestamp.isoformat()} (last: {last_timestamp.isoformat()})"
                            self.logger.warning(f"[{symbol}] ⚠️  {failure_reason} - Cached data!")

                # Update last timestamp for next check (even if unhealthy)
                self.data_health[last_timestamp_key] = data_timestamp

                # === Layer 4: Price validity ===
                current_price = data.get("current_price", 0.0)
                if current_price <= 0 and is_healthy:
                    is_healthy = False
                    failure_reason = f"Invalid price: ${current_price}"
                    self.logger.warning(f"[{symbol}] ⚠️  {failure_reason}")

                # === Layer 5: Data bars count ===
                bars_1s = data.get("bars_1s", [])
                min_bars = self.data_health_config.get("min_bars_required", 5)

                if len(bars_1s) < min_bars and is_healthy:
                    is_healthy = False
                    failure_reason = f"Insufficient bars: {len(bars_1s)} (need {min_bars})"
                    self.logger.warning(f"[{symbol}] ⚠️  {failure_reason}")

                # === Layer 6: Volume check (warn only) ===
                if bars_1s and len(bars_1s) > 0:
                    latest_bar = bars_1s[0]
                    volume = latest_bar.get("volume", 0)
                    if volume <= 0:
                        self.logger.debug(f"[{symbol}] No volume in latest bar (may be normal for extended hours)")

                # === Final verdict ===
                if is_healthy:
                    fresh_count += 1
                    self.logger.debug(f"[{symbol}] ✓ Healthy: age={data_age_sec:.1f}s, price=${current_price:.2f}, bars={len(bars_1s)}")
                else:
                    stale_count += 1
                    stale_symbols.append(symbol)

            except (ValueError, TypeError) as e:
                is_healthy = False
                self.logger.warning(f"[{symbol}] ⚠️  Timestamp parse error: {data_timestamp_str}, {e}")
                stale_count += 1
                stale_symbols.append(symbol)

        # === Overall health assessment ===
        if stale_count == 0:
            self._update_health_status("GOOD", stale_symbols)
            return True
        elif stale_count == len(self.symbols):
            self._update_health_status("BAD", stale_symbols)
            return False
        else:
            self.logger.warning(f"⚠️  Partial data: {fresh_count} fresh, {stale_count} stale")
            self._update_health_status("PARTIAL", stale_symbols)
            return True

    def _update_health_status(self, result: str, stale_symbols: List[str]):
        """Update data health status based on check result (V2.12)

        Args:
            result: "GOOD" / "BAD" / "PARTIAL"
            stale_symbols: List of symbols with stale data
        """
        from datetime import datetime, timezone

        if result == "GOOD":
            # Data is good
            if self.data_health["status"] in ["UNHEALTHY", "RECOVERING"]:
                # Was unhealthy, now recovering
                self.data_health["recovery_checks_passed"] += 1

                if self.data_health["recovery_checks_passed"] >= self.data_health_config["recovery_checks_needed"]:
                    # Full recovery confirmed
                    self.logger.info("✅ Data health RECOVERED - resuming normal trading")
                    self.data_health["status"] = "HEALTHY"
                    self.data_health["consecutive_failures"] = 0
                    self.data_health["recovery_checks_passed"] = 0
                else:
                    self.data_health["status"] = "RECOVERING"
                    self.logger.info(f"🔄 Data health recovering ({self.data_health['recovery_checks_passed']}/{self.data_health_config['recovery_checks_needed']})")
            else:
                # Was already healthy
                self.data_health["status"] = "HEALTHY"
                self.data_health["consecutive_failures"] = 0

            self.data_health["last_good_data_time"] = datetime.now(timezone.utc)

        elif result == "BAD":
            # Data is bad - all symbols stale
            self.data_health["consecutive_failures"] += 1
            self.data_health["failure_count"] += 1
            self.data_health["recovery_checks_passed"] = 0  # Reset recovery counter

            if self.data_health["consecutive_failures"] >= self.data_health_config["unhealthy_after_failures"]:
                if self.data_health["status"] != "UNHEALTHY":
                    self.logger.error(f"❌ Data health UNHEALTHY (failed {self.data_health['consecutive_failures']} times)")
                    self.logger.error("   → Stopping LLM calls to save tokens")
                    self.logger.error(f"   → Stale symbols: {', '.join(stale_symbols)}")
                self.data_health["status"] = "UNHEALTHY"
            elif self.data_health["consecutive_failures"] >= self.data_health_config["degraded_after_failures"]:
                if self.data_health["status"] == "HEALTHY":
                    self.logger.warning(f"⚠️  Data health DEGRADED (failed {self.data_health['consecutive_failures']} times)")
                self.data_health["status"] = "DEGRADED"

        elif result == "PARTIAL":
            # Partial failure - log but don't change status drastically
            pass

    def _handle_unhealthy_data(self):
        """Handle unhealthy data state - skip this cycle (V2.12)"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        time_since_good = (now - self.data_health["last_good_data_time"]).total_seconds()

        self.logger.warning(f"⏸️  Skipping cycle #{self.cycle_count} due to unhealthy data")
        self.logger.warning(f"   Last good data: {time_since_good:.0f}s ago")
        self.logger.warning(f"   Consecutive failures: {self.data_health['consecutive_failures']}")
        self.logger.warning(f"   Status: {self.data_health['status']}")
        self.logger.warning(f"   Will retry in {getattr(self, 'cycle_interval_sec', 60)}s...")

        # Check if we've been waiting too long
        if time_since_good > self.data_health_config["max_wait_time_sec"]:
            self.logger.error(f"🚨 CRITICAL: No good data for {time_since_good/60:.1f} minutes!")
            self.logger.error(f"   Check data pipeline:")
            self.logger.error(f"   - Redis: redis-cli LINDEX sfs:1s:AAPL 0")
            self.logger.error(f"   - ClickHouse: SELECT * FROM sfs.sfs_5s WHERE symbol='AAPL' ORDER BY ts DESC LIMIT 1")
            self.logger.error(f"   - Kafka consumer: check if market data is flowing")

    async def execute_trade(self, decision: LLMResponse, current_price: float) -> bool:
        """Execute a trading decision (V2.10: Extended hours use limit orders)

        Args:
            decision: LLM decision
            current_price: Current market price

        Returns:
            True if trade was executed
        """
        symbol = decision.symbol
        action = decision.decision
        quantity = decision.quantity or 0

        if action == "HOLD" or quantity == 0:
            return False

        self.logger.info(f"\n[{symbol}] LLM Decision: {action} x {quantity} @ ${current_price:.2f}")
        self.logger.info(f"Confidence: {decision.confidence:.2f}")
        self.logger.info(f"Reasoning: {decision.reasoning}")

        # V2.10+: Determine order type
        # Extended hours require limit orders; also use limit if LLM suggested a planned price
        now_local = now_ny()
        session_phase = "RTH"  # Default
        use_limit_order = False

        if self.time_manager:
            session_phase = self.time_manager.session_phase(now_local)
            # Use limit orders in extended hours
            use_limit_order = session_phase in ("PRE", "AFTERHOURS", "OVERNIGHT", "OFF")

        order_type = "LMT" if use_limit_order else "MKT"

        # Calculate/apply limit price
        limit_price = None
        # If LLM provided planned limit price, honor it (intraday anticipatory entry)
        if getattr(decision, 'limit_price', None):
            try:
                limit_price = float(decision.limit_price)
                use_limit_order = True
                order_type = "LMT"
            except Exception:
                limit_price = None
        # Else if session dictates limit orders, synthesize a conservative limit
        elif use_limit_order:
            if action == "BUY":
                limit_price = current_price * 1.001
            else:
                limit_price = current_price * 0.999

        # DEBUG: Show execution mode
        mode = "🎮 DRY RUN (simulation)" if self.dry_run else "📡 LIVE TRADING (real orders to IBKR)"
        self.logger.info(f"[DEBUG] Mode: {mode}")
        self.logger.info(f"[DEBUG] dry_run={self.dry_run}, ibkr_executor={'None' if self.ibkr_executor is None else 'initialized'}")
        self.logger.info(f"[DEBUG] Session: {session_phase}, Order Type: {order_type}")
        if limit_price:
            self.logger.info(f"[DEBUG] Limit Price: ${limit_price:.2f}")

        # If LLM suggested a planned limit and we are in dry-run mode, schedule locally instead of immediate fill
        try:
            if self.dry_run and getattr(decision, 'limit_price', None) and quantity > 0:
                import time as _t
                ttl = int(getattr(decision, 'time_window_s', 0) or 900)
                expiry = _t.time() + ttl
                side = action
                plan = {"side": side, "qty": int(quantity), "px": float(decision.limit_price), "expire_ts": expiry}
                arr = self.pending_orders.get(symbol, [])
                arr.append(plan)
                self.pending_orders[symbol] = arr
                self.logger.info(f"[{symbol}] 🗓️ Scheduled planned {side} limit ${float(decision.limit_price):.2f} (valid {ttl//60}m)")
                return False
        except Exception:
            pass

        # Execute based on action
        if action == "BUY":
            # PRE-CHECK: Verify portfolio limits BEFORE placing order
            can_open, reason = self.portfolio.can_open_position(symbol, current_price, quantity)
            if not can_open:
                self.logger.warning(f"⚠️  Cannot open position: {reason}")
                self.logger.warning(f"   Portfolio: Cash=${self.portfolio.cash:.2f}, Equity=${self.portfolio.total_equity:.2f}")
                return False

            # V2.3: Log order creation (V2.10: dynamic order type)
            if hasattr(self, 'ledger') and self.session_id:
                import time
                order_id_internal = f"{symbol}-{int(time.time()*1000)}"

                self.ledger.log_order_event(
                    session_id=self.session_id,
                    order_id_internal=order_id_internal,
                    stage="created",
                    symbol=symbol,
                    side="BUY",
                    qty=quantity,
                    order_type=order_type,  # V2.10: MKT or LMT based on session
                    limit_price=limit_price,  # V2.10: set for limit orders
                    tif="DAY",
                    meta={
                        "confidence": decision.confidence if hasattr(decision, 'confidence') else 0.0,
                        "dry_run": self.dry_run,
                        "session_phase": session_phase,  # V2.10: track session
                    },
                )

            if self.dry_run:
                # Simulate
                self.logger.info(f"[DEBUG] Simulating BUY (not sending to IBKR)")
                success = self.portfolio.open_position(symbol, quantity, current_price)
                if success:
                    self.logger.info(f"✓ Simulated position opened: {symbol} x {quantity} @ ${current_price:.2f}")

                    # V2.3: Log fill
                    if hasattr(self, 'ledger') and self.session_id:
                        from .utils.commission import calculate_commission
                        fee = calculate_commission(quantity * current_price)

                        self.ledger.log_fill(
                            session_id=self.session_id,
                            order_id_internal=order_id_internal,
                            fill_qty=quantity,
                            fill_price=current_price,
                            symbol=symbol,
                            fee=fee,
                            source="SIM",
                        )

                        # Also log order filled stage (V2.10: dynamic order type)
                        self.ledger.log_order_event(
                            session_id=self.session_id,
                            order_id_internal=order_id_internal,
                            stage="filled",
                            symbol=symbol,
                            side="BUY",
                            qty=quantity,
                            order_type=order_type,  # V2.10
                            limit_price=limit_price,  # V2.10
                            tif="DAY",
                            meta={"avg_fill_price": current_price, "session_phase": session_phase},
                        )
            else:
                # Real trade - PRE-CHECK PASSED, now place order (V2.10: market or limit)
                self.logger.info(f"[DEBUG] Sending REAL BUY order to IBKR ({order_type})...")
                if use_limit_order:
                    trade = await self.ibkr_executor.place_limit_order(
                        symbol, OrderAction.BUY, quantity, limit_price
                    )
                else:
                    trade = await self.ibkr_executor.place_market_order(
                        symbol, OrderAction.BUY, quantity
                    )
                if trade:
                    self.logger.info(f"[DEBUG] Order submitted to IBKR, waiting for fill...")
                    # Wait for fill and update portfolio
                    filled = await self.ibkr_executor.wait_for_fill(trade, timeout=30)
                    if filled:
                        fill_price = trade.orderStatus.avgFillPrice
                        # Update portfolio (should succeed since we pre-checked)
                        success = self.portfolio.open_position(symbol, quantity, fill_price)
                        if success:
                            self.logger.info(f"✓ REAL ORDER FILLED: {symbol} x {quantity} @ ${fill_price:.2f}")

                            # V2.3: Log fill
                            if hasattr(self, 'ledger') and self.session_id:
                                from .utils.commission import calculate_commission
                                fee = calculate_commission(quantity * fill_price)

                                self.ledger.log_fill(
                                    session_id=self.session_id,
                                    order_id_internal=order_id_internal,
                                    fill_qty=quantity,
                                    fill_price=fill_price,
                                    symbol=symbol,
                                    fee=fee,
                                    source="IBKR",
                                )

                                # Also log order filled stage
                                self.ledger.log_order_event(
                                    session_id=self.session_id,
                                    order_id_internal=order_id_internal,
                                    stage="filled",
                                    symbol=symbol,
                                    side="BUY",
                                    qty=quantity,
                                    order_type=order_type,  # V2.10: Dynamic order type
                                    limit_price=limit_price,  # V2.10: Set for limit orders
                                    tif="DAY",
                                    meta={"avg_fill_price": fill_price, "session_phase": session_phase},  # V2.10: Track session
                                )
                        else:
                            self.logger.error(f"✗ Portfolio update failed after fill (this should not happen!)")
                    else:
                        success = False
                        self.logger.error(f"✗ Order NOT filled (timeout)")
                else:
                    success = False
                    self.logger.error(f"✗ Failed to submit order to IBKR")

            return success

        elif action == "SELL":
            # V2.2: Check if this is closing a long position or opening a short position
            has_position = symbol in self.portfolio.positions

            if has_position:
                # Closing existing long position
                can_close, reason = self.portfolio.can_close_position(symbol, quantity)
                if not can_close:
                    self.logger.warning(f"⚠️  Cannot close position: {reason}")
                    return False

                # V2.3: Log order creation (closing long)
                if hasattr(self, 'ledger') and self.session_id:
                    import time
                    order_id_internal = f"{symbol}-{int(time.time()*1000)}"

                    self.ledger.log_order_event(
                        session_id=self.session_id,
                        order_id_internal=order_id_internal,
                        stage="created",
                        symbol=symbol,
                        side="SELL",
                        qty=quantity,
                        order_type=order_type,  # V2.10: Dynamic order type
                        limit_price=limit_price,  # V2.10: Set for limit orders
                        tif="DAY",
                        meta={
                            "confidence": decision.confidence if hasattr(decision, 'confidence') else 0.0,
                            "dry_run": self.dry_run,
                            "action_type": "close_long",
                            "session_phase": session_phase,  # V2.10: Track session
                        },
                    )

                if self.dry_run:
                    # Simulate
                    self.logger.info(f"[DEBUG] Simulating SELL (closing long, not sending to IBKR)")
                    success = self.portfolio.close_position(symbol, quantity, current_price)
                    if success:
                        self.logger.info(f"✓ Simulated position closed: {symbol} x {quantity} @ ${current_price:.2f}")

                        # V2.3: Log fill
                        if hasattr(self, 'ledger') and self.session_id:
                            from .utils.commission import calculate_commission
                            fee = calculate_commission(quantity * current_price)

                            self.ledger.log_fill(
                                session_id=self.session_id,
                                order_id_internal=order_id_internal,
                                fill_qty=quantity,
                                fill_price=current_price,
                                symbol=symbol,
                                fee=fee,
                                source="SIM",
                            )

                            # Also log order filled stage
                            self.ledger.log_order_event(
                                session_id=self.session_id,
                                order_id_internal=order_id_internal,
                                stage="filled",
                                symbol=symbol,
                                side="SELL",
                                qty=quantity,
                                order_type=order_type,  # V2.10: Dynamic order type
                                limit_price=limit_price,  # V2.10: Set for limit orders
                                tif="DAY",
                                meta={"avg_fill_price": current_price, "action_type": "close_long", "session_phase": session_phase},
                            )
                else:
                    # Real trade - PRE-CHECK PASSED, now place order (V2.10: market or limit)
                    self.logger.info(f"[DEBUG] Sending REAL SELL order to IBKR (closing long, {order_type})...")
                    if use_limit_order:
                        trade = await self.ibkr_executor.place_limit_order(
                            symbol, OrderAction.SELL, quantity, limit_price
                        )
                    else:
                        trade = await self.ibkr_executor.place_market_order(
                            symbol, OrderAction.SELL, quantity
                        )
                    if trade:
                        self.logger.info(f"[DEBUG] Order submitted to IBKR, waiting for fill...")
                        filled = await self.ibkr_executor.wait_for_fill(trade, timeout=30)
                        if filled:
                            fill_price = trade.orderStatus.avgFillPrice
                            # Update portfolio (should succeed since we pre-checked)
                            success = self.portfolio.close_position(symbol, quantity, fill_price)
                            if success:
                                self.logger.info(f"✓ REAL ORDER FILLED (closed long): {symbol} x {quantity} @ ${fill_price:.2f}")

                                # V2.3: Log fill
                                if hasattr(self, 'ledger') and self.session_id:
                                    from .utils.commission import calculate_commission
                                    fee = calculate_commission(quantity * fill_price)

                                    self.ledger.log_fill(
                                        session_id=self.session_id,
                                        order_id_internal=order_id_internal,
                                        fill_qty=quantity,
                                        fill_price=fill_price,
                                        symbol=symbol,
                                        fee=fee,
                                        source="IBKR",
                                    )

                                    # Also log order filled stage
                                    self.ledger.log_order_event(
                                        session_id=self.session_id,
                                        order_id_internal=order_id_internal,
                                        stage="filled",
                                        symbol=symbol,
                                        side="SELL",
                                        qty=quantity,
                                        order_type=order_type,  # V2.10: Dynamic order type
                                        limit_price=limit_price,  # V2.10: Set for limit orders
                                        tif="DAY",
                                        meta={"avg_fill_price": fill_price, "action_type": "close_long", "session_phase": session_phase},
                                    )
                            else:
                                self.logger.error(f"✗ Portfolio update failed after fill (this should not happen!)")
                        else:
                            success = False
                            self.logger.error(f"✗ Order NOT filled (timeout)")
                    else:
                        success = False
                        self.logger.error(f"✗ Failed to submit order to IBKR")
            else:
                # V2.2: Opening short position (if allowed)
                if not self.portfolio.allow_short_selling:
                    self.logger.warning(f"⚠️  Cannot open short position: Short selling is disabled")
                    self.logger.info(f"   Enable with --allow-short-selling flag")
                    return False

                # PRE-CHECK: Verify portfolio limits BEFORE placing order
                can_open, reason = self.portfolio.can_open_position(symbol, current_price, quantity)
                if not can_open:
                    self.logger.warning(f"⚠️  Cannot open short position: {reason}")
                    return False

                # V2.3: Log order creation (opening short)
                if hasattr(self, 'ledger') and self.session_id:
                    import time
                    order_id_internal = f"{symbol}-{int(time.time()*1000)}"

                    self.ledger.log_order_event(
                        session_id=self.session_id,
                        order_id_internal=order_id_internal,
                        stage="created",
                        symbol=symbol,
                        side="SELL",
                        qty=quantity,
                        order_type=order_type,  # V2.10: Dynamic order type
                        limit_price=limit_price,  # V2.10: Set for limit orders
                        tif="DAY",
                        meta={
                            "confidence": decision.confidence if hasattr(decision, 'confidence') else 0.0,
                            "dry_run": self.dry_run,
                            "action_type": "open_short",
                            "session_phase": session_phase,  # V2.10: Track session
                        },
                    )

                if self.dry_run:
                    # Simulate short
                    self.logger.info(f"[DEBUG] Simulating SHORT (opening short, not sending to IBKR)")
                    success = self.portfolio.open_position(symbol, quantity, current_price, is_short=True)
                    if success:
                        self.logger.info(f"✓ Simulated short position opened: {symbol} x {quantity} @ ${current_price:.2f}")

                        # V2.3: Log fill
                        if hasattr(self, 'ledger') and self.session_id:
                            from .utils.commission import calculate_commission
                            fee = calculate_commission(quantity * current_price)

                            self.ledger.log_fill(
                                session_id=self.session_id,
                                order_id_internal=order_id_internal,
                                fill_qty=quantity,
                                fill_price=current_price,
                                symbol=symbol,
                                fee=fee,
                                source="SIM",
                            )

                            # Also log order filled stage
                            self.ledger.log_order_event(
                                session_id=self.session_id,
                                order_id_internal=order_id_internal,
                                stage="filled",
                                symbol=symbol,
                                side="SELL",
                                qty=quantity,
                                order_type=order_type,  # V2.10: Dynamic order type
                                limit_price=limit_price,  # V2.10: Set for limit orders
                                tif="DAY",
                                meta={"avg_fill_price": current_price, "action_type": "open_short", "session_phase": session_phase},
                            )
                else:
                    # Real trade - PRE-CHECK PASSED, now place order (V2.10: market or limit)
                    self.logger.info(f"[DEBUG] Sending REAL SHORT SELL order to IBKR ({order_type})...")
                    if use_limit_order:
                        trade = await self.ibkr_executor.place_limit_order(
                            symbol, OrderAction.SELL, quantity, limit_price
                        )
                    else:
                        trade = await self.ibkr_executor.place_market_order(
                            symbol, OrderAction.SELL, quantity
                        )
                    if trade:
                        self.logger.info(f"[DEBUG] Order submitted to IBKR, waiting for fill...")
                        filled = await self.ibkr_executor.wait_for_fill(trade, timeout=30)
                        if filled:
                            fill_price = trade.orderStatus.avgFillPrice
                            # Update portfolio (should succeed since we pre-checked)
                            success = self.portfolio.open_position(symbol, quantity, fill_price, is_short=True)
                            if success:
                                self.logger.info(f"✓ REAL SHORT ORDER FILLED: {symbol} x {quantity} @ ${fill_price:.2f}")

                                # V2.3: Log fill
                                if hasattr(self, 'ledger') and self.session_id:
                                    from .utils.commission import calculate_commission
                                    fee = calculate_commission(quantity * fill_price)

                                    self.ledger.log_fill(
                                        session_id=self.session_id,
                                        order_id_internal=order_id_internal,
                                        fill_qty=quantity,
                                        fill_price=fill_price,
                                        symbol=symbol,
                                        fee=fee,
                                        source="IBKR",
                                    )

                                    # Also log order filled stage
                                    self.ledger.log_order_event(
                                        session_id=self.session_id,
                                        order_id_internal=order_id_internal,
                                        stage="filled",
                                        symbol=symbol,
                                        side="SELL",
                                        qty=quantity,
                                        order_type=order_type,  # V2.10: Dynamic order type
                                        limit_price=limit_price,  # V2.10: Set for limit orders
                                        tif="DAY",
                                        meta={"avg_fill_price": fill_price, "action_type": "open_short", "session_phase": session_phase},
                                    )
                            else:
                                self.logger.error(f"✗ Portfolio update failed after fill (this should not happen!)")
                        else:
                            success = False
                            self.logger.error(f"✗ Order NOT filled (timeout)")
                    else:
                        success = False
                        self.logger.error(f"✗ Failed to submit order to IBKR")

            return success

        return False

    async def run_trading_cycle(self):
        """Run one trading cycle across all symbols"""
        self.cycle_count += 1
        from datetime import datetime as dt
        # Get current ET time directly for consistency
        current_time_et = now_ny()

        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"Trading Cycle #{self.cycle_count} - {current_time_et.strftime('%Y-%m-%d %H:%M:%S ET')}")

        # V2.10: Check trading session (skip only during maintenance window)
        if self.time_manager:
            session_phase = self.time_manager.session_phase(now_ny())

            # Only skip during OFF session (03:50-04:00 ET maintenance window)
            if session_phase == "OFF":
                self.logger.info(f"⏰ System maintenance window (current: {current_time_et.strftime('%H:%M')} ET)")
                self.logger.info(f"   Maintenance: 03:50-04:00 ET")
                self.logger.info(f"   Skipping this cycle")
                self.logger.info(f"{'='*80}")
                return

            # Log current session for visibility
            self.logger.info(f"📊 Trading Session: {session_phase}")

        # V2: Show market phase if time awareness enabled
        if self.enable_time_awareness and self.time_awareness:
            from .utils.trading_helpers import get_market_phase_description
            phase = self.time_awareness.get_market_phase()
            phase_desc = get_market_phase_description(phase.value)
            self.logger.info(f"Market Phase: {phase.value} - {phase_desc}")

        self.logger.info(f"{'='*80}")

        # Refresh dynamic skills (best-effort, non-blocking)
        self._maybe_refresh_skill_runtime()

        # V2.5: Refresh higher TF context periodically
        import time
        if self.use_aggregator and self.higher_loader:
            now = time.time()
            if now - self.last_higher_refresh >= self.higher_tf_refresh_sec:
                self.logger.info("\n[Higher TF] Refreshing 1h/1d context...")
                try:
                    self.higher_cache = self.higher_loader.load(
                        self.symbols, one_hour_limit=120
                    )
                    self.logger.info(f"[Higher TF] ✅ Refreshed for {len(self.higher_cache)} symbols")
                except Exception as e:
                    self.logger.info(f"[Higher TF] ⚠️  Refresh failed: {e}")
                self.last_higher_refresh = now

        # V2.5: Get time context
        time_ctx = None
        if self.use_aggregator and self.time_manager:
            now_local = now_ny()
            # V2.1: Pass api_interval_sec for decisions_left calculation
            # Use actual cycle interval (or 60 as fallback)
            api_interval = getattr(self, 'cycle_interval_sec', 60)
            time_ctx = self.time_manager.build_time_ctx(now_local, api_interval_sec=api_interval)
            self.logger.info(f"\n[Time] Phase: {time_ctx['phase']}, "
                  f"Allow New: {time_ctx['allow_new_position']}, "
                  f"Should Flatten: {time_ctx['should_flatten']}")

            # EOD flatten check
            if time_ctx['should_flatten']:
                self.logger.info("\n⚠️  EOD Flatten triggered - closing all positions")
                for symbol in self.symbols:
                    # Fixed: use positions dict directly
                    if symbol in self.portfolio.positions:
                        pos = self.portfolio.positions[symbol]
                        pos_size = pos.quantity
                        current_price = pos.current_price
                        self.logger.info(f"  Flattening {symbol}: {pos_size} shares @ ${current_price:.2f}")
                        self.portfolio.close_position(symbol, pos_size, current_price)
                # Continue cycle after flatten (don't break)

        # Get portfolio state
        portfolio_state = self.portfolio.get_position_state()
        self.logger.info(f"Portfolio: Equity=${portfolio_state['total_equity']:,.2f}, "
              f"Cash=${portfolio_state['cash']:,.2f}, "
              f"P&L=${portfolio_state['total_pnl']:,.2f} ({portfolio_state['total_pnl_pct']:.2f}%)")

        # V2: Check for reflection trigger
        if self.enable_memory and self.time_awareness and self.memory_system:
            from llm_trader.utils.trading_helpers import should_execute_reflection, calculate_reflection_stats

            phase = self.time_awareness.get_market_phase()
            if should_execute_reflection(self.last_reflection_time, current_time_et, phase.value):
                self.logger.info(f"\n[V2] Triggering reflection for {phase.value} phase...")
                stats = calculate_reflection_stats(self.portfolio.trade_history)

                if self.prompt_builder:
                    # Build recent trades summary
                    recent_trades = self.portfolio.trade_history[-10:]
                    trades_summary = "\n".join([
                        f"- {t['action']} {t['symbol']} x{t['quantity']} @ ${t['price']:.2f} "
                        f"(P&L: ${t.get('realized_pnl', 0):.2f})"
                        for t in recent_trades
                    ])

                    reflection_prompt = self.prompt_builder.build_reflection_prompt(
                        period=phase.value,
                        total_trades=stats['total_trades'],
                        winning_trades=stats['winning_trades'],
                        losing_trades=stats['losing_trades'],
                        total_pnl=stats['total_pnl'],
                        recent_trades_summary=trades_summary,
                    )

                    # Get reflection from LLM
                    reflection_response = await self.llm_provider.generate_response(reflection_prompt)

                    # Store reflection
                    self.memory_system.add_reflection(
                        period=phase.value,
                        insights=reflection_response,
                        performance_summary=stats,
                    )

                    self.logger.info(f"[V2] Reflection stored: {reflection_response[:100]}...")

                self.last_reflection_time = current_time

        # Fetch market data for all symbols
        self.logger.info(f"\nFetching market data for {len(self.symbols)} symbols...")
        market_data_all = await self.data_fetcher.get_multiple_symbols_data(self.symbols)

        # V2.12: Data health check - stop LLM calls if data is stale
        data_health_ok = self._check_data_health(market_data_all)

        if not data_health_ok:
            # Data is unhealthy, skip LLM calls this cycle to save tokens
            self._handle_unhealthy_data()
            # Still update portfolio prices with available data (even if stale)
            prices = {
                symbol: data.get("current_price", 0.0)
                for symbol, data in market_data_all.items()
            }
            self.portfolio.update_position_prices(prices)
            return  # Skip rest of cycle

        # Update portfolio prices
        prices = {
            symbol: data.get("current_price", 0.0)
            for symbol, data in market_data_all.items()
        }
        self.portfolio.update_position_prices(prices)

        # Analyze each symbol with LLM
        for symbol in self.symbols:
            try:
                # V2.5: Update aggregator with latest 1s bars
                has_realtime_data = False
                if self.use_aggregator:
                    try:
                        bars_1s = await self.data_fetcher.get_1s_bars_from_redis(symbol, n=60)
                        if bars_1s:
                            # 检查数据新鲜度（最新bar不超过10秒）
                            import time
                            latest_bar = bars_1s[0]  # Redis返回的第一条是最新的
                            latest_ts_ms = latest_bar.get("ts", 0)
                            now_ms = int(time.time() * 1000)
                            data_age_sec = (now_ms - latest_ts_ms) / 1000.0

                            if data_age_sec >= 10:
                                self.logger.info(f"[{symbol}] ⚠️  Data too old ({data_age_sec:.1f}s), treating as no realtime data")
                                has_realtime_data = False
                            else:
                                for bar in bars_1s:
                                    self.aggregator.update_from_1s(symbol, bar)
                                self.logger.info(f"[{symbol}] Updated aggregator with {len(bars_1s)} x 1s bars (latest: {data_age_sec:.1f}s ago)")
                                has_realtime_data = True
                        else:
                            self.logger.info(f"[{symbol}] ⚠️  No 1s bars from Redis")
                            has_realtime_data = False
                    except Exception as e:
                        self.logger.info(f"[{symbol}] ❌ Aggregator update failed: {e}")
                        has_realtime_data = False

                market_data = market_data_all.get(symbol, {})
                current_price = market_data.get("current_price", 0.0)

                if current_price == 0.0:
                    self.logger.info(f"[{symbol}] No price data, skipping")
                    continue

                self.logger.info(f"\n[{symbol}] Analyzing... (price: ${current_price:.2f})")

                # Handle pending planned orders (dry-run/local scheduler)
                try:
                    import time as _t
                    plans = self.pending_orders.get(symbol, [])
                    still_pending = []
                    for plan in plans:
                        if _t.time() > plan.get("expire_ts", 0):
                            self.logger.info(f"[{symbol}] ⏱️ Pending {plan['side']} expired; dropping plan")
                            continue
                        # Trigger conditions
                        mode = str(plan.get("mode") or "limit").lower()
                        # at-open triggers
                        if mode in ("sell_at_open", "buy_on_open"):
                            try:
                                now_local = now_ny()
                                phase = self.time_manager.session_phase(now_local) if self.time_manager else "RTH"
                                # within first 2 minutes after open
                                open_dt = now_local.replace(hour=9, minute=30, second=0, microsecond=0)
                                since_open_sec = (now_local - open_dt).total_seconds()
                                in_open_window = (phase == "RTH") and (0 <= since_open_sec <= 120)
                            except Exception:
                                in_open_window = False
                            if in_open_window:
                                side = plan["side"]
                                qty = int(plan.get("qty") or 0)
                                self.logger.info(f"[{symbol}] 🚀 Open-trigger {mode} executing {side} x {qty}")
                                from .providers import LLMResponse
                                await self.execute_trade(
                                    LLMResponse(symbol=symbol, decision=side, quantity=qty, confidence=1.0,
                                                reasoning=mode, limit_price=None),
                                    current_price
                                )
                                continue
                            # not yet at open window, keep waiting
                            still_pending.append(plan)
                            continue

                        # price limit triggers
                        if plan["side"] == "BUY" and plan.get("px") is not None and current_price <= float(plan["px"]):
                            self.logger.info(f"[{symbol}] ✅ Planned BUY trigger met @ ${current_price:.2f} ≤ ${plan['px']:.2f}; executing")
                            from .providers import LLMResponse
                            await self.execute_trade(
                                LLMResponse(symbol=symbol, decision="BUY", quantity=int(plan["qty"]), confidence=1.0,
                                            reasoning="planned_limit_fill", limit_price=float(plan["px"])) ,
                                current_price
                            )
                            continue
                        elif plan["side"] == "SELL" and plan.get("px") is not None and current_price >= float(plan["px"]):
                            self.logger.info(f"[{symbol}] ✅ Planned SELL trigger met @ ${current_price:.2f} ≥ ${plan['px']:.2f}; executing")
                            from .providers import LLMResponse
                            await self.execute_trade(
                                LLMResponse(symbol=symbol, decision="SELL", quantity=int(plan["qty"]), confidence=1.0,
                                            reasoning="planned_limit_fill", limit_price=float(plan["px"])) ,
                                current_price
                            )
                            continue
                        # Keep waiting
                        still_pending.append(plan)
                    if still_pending:
                        self.pending_orders[symbol] = still_pending
                    elif symbol in self.pending_orders:
                        del self.pending_orders[symbol]
                except Exception as _e:
                    self.logger.info(f"[{symbol}] Pending plan check failed: {_e}")

                # V2.5: 硬性检查 - 必须有实时数据才能交易
                if self.use_aggregator and not has_realtime_data:
                    self.logger.info(f"  🚫 No realtime data from Redis - skipping API call (safety gate)")
                    self.logger.info(f"  → Decision: HOLD (no real-time market data)")
                    continue

                # V2.6: Build prompt using CompactPromptBuilder if enabled
                if self.use_compact_prompt and self.compact_prompt_builder and time_ctx:
                    # Get multi-timeframe snapshot
                    mtf_snapshot = self.aggregator.get_snapshot(symbol)

                    # Get VIX
                    vix_value = await self.data_fetcher.get_latest_vix()

                    # Get portfolio state for this symbol
                    if symbol in self.portfolio.positions:
                        pos = self.portfolio.positions[symbol]
                        position_size = pos.quantity
                        avg_cost = pos.avg_cost
                    else:
                        position_size = 0
                        avg_cost = 0.0

                    # V2.8: Calculate daily PnL for risk control
                    current_equity = portfolio_state['total_equity']
                    daily_pnl = current_equity - self.session_start_equity

                    portfolio_for_prompt = {
                        "position_size": position_size,
                        "avg_cost": avg_cost,
                        "current_price": current_price,
                        "cash": portfolio_state['cash'],
                        "equity": current_equity,
                        "daily_pnl": daily_pnl,  # V2.8: 每日盈亏
                        "max_position_size": 5000.0
                    }

                    # Market context
                    market_ctx = {
                        "vix": vix_value or 18.0,
                        "d1_atr_bps": 150,
                        "d1_trend": "up"
                    }

                    # V2.7: Add sentiment analysis (with 5-minute cache)
                    if self.sentiment_engine is not None:
                        current_time = time.time()
                        if (self.sentiment_cache is None or
                            (current_time - self.sentiment_cache_timestamp) > self.sentiment_refresh_interval):
                            try:
                                # Refresh sentiment cache
                                self.sentiment_cache = self.sentiment_engine.get_summary_dict([symbol])
                                self.sentiment_cache_timestamp = current_time
                                self.logger.debug(f"[{symbol}] Sentiment refreshed: composite={self.sentiment_cache['composite_score']:.3f}")
                            except Exception as e:
                                self.logger.warning(f"[{symbol}] Sentiment fetch failed: {e}")
                                self.sentiment_cache = None

                        # Add to market_ctx
                        if self.sentiment_cache:
                            market_ctx.update({
                                "sentiment_composite": round(self.sentiment_cache['composite_score'], 3),
                                "sentiment_label": self.sentiment_cache['composite_label'],
                                "fear_score": round(self.sentiment_cache['fear_score'], 3),
                                "flow_score": round(self.sentiment_cache['flow_score'], 3),
                                "narrative_score": round(self.sentiment_cache['narrative_score'], 3)
                            })

                    # Trade memory (from portfolio)
                    total_trades = len(self.portfolio.trade_history)
                    winning_trades = sum(1 for t in self.portfolio.trade_history if t.get('realized_pnl', 0) > 0)
                    avg_win_bps = 45 if winning_trades > 0 else 0
                    avg_loss_bps = -30
                    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

                    trade_memory = {
                        "total_trades": total_trades,
                        "winning_trades": winning_trades,
                        "avg_win_bps": avg_win_bps,
                        "avg_loss_bps": avg_loss_bps,
                        "risk_reward": abs(avg_win_bps / avg_loss_bps) if avg_loss_bps != 0 else 1.5,
                        "max_drawdown_pct": -2.5,
                        "median_hold_seconds": 180,
                        "last3": [35, -25, 50],
                        "current_plan": "momentum"
                    }

                    # Build compact execution prompt
                    messages, provider_kwargs = self.compact_prompt_builder.build_execution_prompt(
                        provider=self.provider_name,
                        symbol=symbol,
                        mtf_snapshot=mtf_snapshot,
                        portfolio=portfolio_for_prompt,
                        time_ctx=time_ctx,
                        market_ctx=market_ctx,
                        trade_memory=trade_memory,
                        base_price=current_price,
                        language="en"
                    )

                    self.logger.info(f"[{symbol}] Using compact execution prompt (~600 tokens, 83% reduction)")

                    # Call provider with messages
                    decision = await self._call_provider_with_messages(messages, provider_kwargs)

                    if decision:
                        # chat_completion() already returns normalized compact dict: {"a":"B","q":19,"conf":0.65}
                        if isinstance(decision, dict):
                            # Map compact fields to action names
                            action_map = {"B": "BUY", "S": "SELL", "H": "HOLD"}
                            action = action_map.get(decision.get("a", "H"), "HOLD")
                            confidence = decision.get("conf", 0.0)
                            shares = decision.get("q", 0)
                            # DEBUG: Log extracted values
                            self.logger.info(f"[DEBUG] Extracted from decision: action={action}, conf={confidence}, shares={shares}")
                        else:
                            # Fallback for unexpected string responses
                            self.logger.info(f"[{symbol}] Unexpected string response, defaulting to HOLD")
                            action = "HOLD"
                            confidence = 0.0
                            shares = 0

                        # Hard gates from time manager
                        if time_ctx['should_flatten'] and position_size > 0:
                            action = "SELL"
                            shares = position_size
                            self.logger.info(f"  🔒 Hard gate: EOD flatten override")

                        if not time_ctx['allow_new_position'] and position_size == 0 and action == "BUY":
                            action = "HOLD"
                            shares = 0
                            self.logger.info(f"  🔒 Hard gate: No new positions allowed")

                        # Create LLMResponse object for execute_trade (carry planning fields if present)
                        from .providers import LLMResponse
                        limit_px = None
                        take_profit_bps = None
                        stop_loss_bps = None
                        time_window_s = None
                        if isinstance(decision, dict):
                            limit_px = decision.get("px")
                            take_profit_bps = decision.get("tp")
                            stop_loss_bps = decision.get("sl")
                            time_window_s = decision.get("tw_s")

                        llm_response = LLMResponse(
                            symbol=symbol,
                            decision=action,
                            confidence=confidence,
                            quantity=shares,
                            reasoning=decision.get("rationale", "") if isinstance(decision, dict) else "",
                            limit_price=limit_px,
                            take_profit_bps=take_profit_bps,
                            stop_loss_bps=stop_loss_bps,
                            time_window_s=time_window_s,
                        )

                        # V2.3: Log decision to ledger
                        if hasattr(self, 'ledger') and self.session_id:
                            self.ledger.log_decision(
                                session_id=self.session_id,
                                symbol=symbol,
                                decision_json={
                                    "action": action,
                                    "confidence": confidence,
                                    "shares": shares,
                                    "rationale": decision.get("rationale", "") if isinstance(decision, dict) else "",
                                },
                                prompt_digest=f"compact_v2.6",
                                raw_text=str(decision)[:4000],
                            )

                        # Execute if confidence is high enough
                        if confidence >= 0.5:
                            await self.execute_trade(llm_response, current_price)
                        else:
                            self.logger.info(f"[{symbol}] Decision: HOLD (LLM suggested {action} but confidence too low: {confidence:.2f}, need ≥0.50)")
                    else:
                        # V2.11: Log failed LLM call as HOLD decision for completeness
                        self.logger.info(f"[{symbol}] No decision from provider (LLM call failed or returned None)")

                        if hasattr(self, 'ledger') and self.session_id:
                            self.ledger.log_decision(
                                session_id=self.session_id,
                                symbol=symbol,
                                decision_json={
                                    "action": "HOLD",
                                    "confidence": 0.0,
                                    "shares": 0,
                                    "rationale": "LLM call failed or returned None",
                                    "error": True,
                                },
                                prompt_digest=f"compact_v2.6_failed",
                                raw_text="No response from LLM provider",
                            )

                    # Skip other prompt builders
                    continue

                # V2.5: Build prompt using UniversalPromptBuilder if aggregator enabled
                elif self.use_aggregator and self.universal_prompt_builder and time_ctx:
                    # Get multi-timeframe snapshot
                    mtf_snapshot = self.aggregator.get_snapshot(symbol)

                    # Get VIX
                    vix_value = await self.data_fetcher.get_latest_vix()

                    # Get portfolio state for this symbol
                    # Fixed: use positions dict directly
                    if symbol in self.portfolio.positions:
                        pos = self.portfolio.positions[symbol]
                        position_size = pos.quantity
                        avg_cost = pos.avg_cost
                    else:
                        position_size = 0
                        avg_cost = 0.0

                    # V2.8: Calculate daily PnL for risk control
                    current_equity = portfolio_state['total_equity']
                    daily_pnl = current_equity - self.session_start_equity

                    portfolio_for_prompt = {
                        "position_size": position_size,
                        "avg_cost": avg_cost,
                        "cash": portfolio_state['cash'],
                        "equity": current_equity,
                        "daily_pnl": daily_pnl,  # V2.8: 每日盈亏
                        "pnl": (position_size * current_price - avg_cost * position_size) if position_size > 0 else 0,
                        "available_buying_power": portfolio_state['available_buying_power'],
                        "max_position_size": portfolio_state['max_position_size'],
                    }

                    # Build prompt
                    messages, provider_kwargs, extra_info = self.universal_prompt_builder.build(
                        provider=self.provider_name,
                        symbol=symbol,
                        mtf_snapshot=mtf_snapshot,
                        portfolio=portfolio_for_prompt,
                        time_ctx=time_ctx,
                        cash_limit=self.initial_cash,
                        allow_new_position=time_ctx['allow_new_position'],
                        vix_value=vix_value,
                        higher_tf_context=self.higher_cache.get(symbol),
                        language="en",
                        # V2.5: 缺数降级配置
                        min_bars_requirements={
                            "1s": 30,
                            "5s": 12,
                            "1m": 10,
                            "5m": 3,
                            "15m": 1,
                        },
                        enable_degrade=True,
                    )

                    # V2.5: 提取缺数信息
                    missing_frames = extra_info.get("missing_frames", [])

                    # 打印缺数警告
                    if missing_frames:
                        self.logger.info(f"  ⚠️  Data incomplete: {', '.join(missing_frames)}")

                    # Call provider with messages
                    decision = await self._call_provider_with_messages(messages, provider_kwargs)

                    if decision:
                        # Support both verbose and compact response formats
                        # Verbose: {"action": "BUY", "confidence": 0.65, "shares": 100}
                        # Compact: {"a": "B", "conf": 0.65, "q": 100}
                        action_map_compact = {"B": "BUY", "S": "SELL", "H": "HOLD"}

                        if "a" in decision:
                            # Compact format (from normalize_to_compact)
                            action = action_map_compact.get(decision.get("a", "H"), "HOLD")
                            confidence = decision.get("conf", 0.0)
                            shares = decision.get("q", 0)
                        else:
                            # Verbose format (standard JSON schema)
                            action = decision.get("action", "HOLD")
                            confidence = decision.get("confidence", 0.0)
                            shares = decision.get("shares", 0)

                        # Auto-calculate shares if LLM returned 0 for BUY/SELL
                        if (action in ["BUY", "SELL"]) and (shares == 0 or shares is None):
                            # Calculate default position: 10% of available cash
                            max_notional = min(self.initial_cash, self.portfolio.cash) * 0.10
                            shares = int(max_notional / current_price)
                            self.logger.info(f"  💡 LLM didn't specify shares, auto-calculated: {shares} shares (10% of cash = ${max_notional:.2f})")

                        # V2.8: 硬规则 1 & 2 - 每日 PnL 风控 (最高优先级)
                        daily_pnl_pct = daily_pnl / current_equity if current_equity > 0 else 0.0
                        loss_limit_hit = daily_pnl_pct <= -0.01
                        profit_limit_hit = daily_pnl_pct >= 0.01

                        if loss_limit_hit:
                            if position_size > 0:
                                action = "SELL"
                                shares = position_size
                                self.logger.info(f"  🔒 Hard gate: Daily loss limit hit ({daily_pnl_pct*100:.2f}%) - FLATTEN")
                            else:
                                action = "HOLD"
                                shares = 0
                                self.logger.info(f"  🔒 Hard gate: Daily loss limit hit ({daily_pnl_pct*100:.2f}%) - NO NEW POSITIONS")

                        elif profit_limit_hit:
                            if position_size > 0:
                                action = "SELL"
                                shares = position_size
                                self.logger.info(f"  🔒 Hard gate: Daily profit limit hit ({daily_pnl_pct*100:.2f}%) - FLATTEN")
                            else:
                                action = "HOLD"
                                shares = 0
                                self.logger.info(f"  🔒 Hard gate: Daily profit limit hit ({daily_pnl_pct*100:.2f}%) - NO NEW POSITIONS")

                        # Hard gates from time manager
                        elif time_ctx['should_flatten'] and position_size > 0:
                            action = "SELL"
                            shares = position_size
                            self.logger.info(f"  🔒 Hard gate: EOD flatten override")

                        elif not time_ctx['allow_new_position'] and position_size == 0 and action == "BUY":
                            action = "HOLD"
                            shares = 0
                            self.logger.info(f"  🔒 Hard gate: No new positions allowed")

                        # V2.5: 硬规则 3 - 缺数降级
                        elif missing_frames and position_size == 0 and action == "BUY":
                            # 关键周期缺数时禁止开仓
                            critical_missing = any(tf.startswith(("1m", "5m")) for tf in missing_frames)
                            if critical_missing:
                                action = "HOLD"
                                shares = 0
                                self.logger.info(f"  🔒 Hard gate: Data incomplete - no new positions")
                                self.logger.info(f"      Missing critical frames: {', '.join([tf for tf in missing_frames if tf.startswith(('1m', '5m'))])}")

                        # Create LLMResponse object for execute_trade
                        from .providers import LLMResponse
                        llm_response = LLMResponse(
                            symbol=symbol,
                            decision=action,
                            confidence=confidence,
                            quantity=shares,
                            reasoning=decision.get("rationale", "")
                        )

                        # V2.3: Log decision to ledger
                        if hasattr(self, 'ledger') and self.session_id:
                            self.ledger.log_decision(
                                session_id=self.session_id,
                                symbol=symbol,
                                decision_json={
                                    "action": action,
                                    "confidence": confidence,
                                    "shares": shares,
                                    "rationale": decision.get("rationale", ""),
                                },
                                prompt_digest=f"universal_v2.5",
                                raw_text=str(decision)[:4000],
                            )

                        # Execute if confidence is high enough
                        # Moderate threshold for quality trades (0.5 = 50% confidence)
                        if confidence >= 0.5:
                            await self.execute_trade(llm_response, current_price)
                        else:
                            self.logger.info(f"[{symbol}] Decision: HOLD (LLM suggested {action} but confidence too low: {confidence:.2f}, need ≥0.50)")
                    else:
                        # V2.11: Log failed LLM call as HOLD decision for completeness
                        self.logger.info(f"[{symbol}] No decision from provider (LLM call failed or returned None)")

                        if hasattr(self, 'ledger') and self.session_id:
                            self.ledger.log_decision(
                                session_id=self.session_id,
                                symbol=symbol,
                                decision_json={
                                    "action": "HOLD",
                                    "confidence": 0.0,
                                    "shares": 0,
                                    "rationale": "LLM call failed or returned None",
                                    "error": True,
                                },
                                prompt_digest=f"universal_v2.5_failed",
                                raw_text="No response from LLM provider",
                            )

                    # V2.5: Skip old code path, continue to next symbol
                    continue

                # V2: Use PromptBuilder if enabled (fallback when aggregator not used)
                elif self.enable_memory and self.prompt_builder:
                    # R1PromptBuilder has different signature (no include_memory param)
                    if isinstance(self.prompt_builder, R1PromptBuilder):
                        decision_prompt = self.prompt_builder.build_decision_prompt(
                            symbol=symbol,
                            market_data=market_data,
                            portfolio_state=portfolio_state,
                        )
                    else:
                        decision_prompt = self.prompt_builder.build_decision_prompt(
                            symbol=symbol,
                            market_data=market_data,
                            portfolio_state=portfolio_state,
                            include_memory=True,
                        )
                    # Get LLM decision with custom prompt
                    decision = await self.llm_provider.analyze_market_with_prompt(
                        symbol, decision_prompt, portfolio_state={}
                    )
                else:
                    # V1: Use default provider method
                    decision = await self.llm_provider.analyze_market(
                        symbol, market_data, portfolio_state
                    )

                # Allow dynamic skills to enrich or filter decisions before gates
                decision = self._apply_skill_decision_hooks(
                    decision,
                    symbol=symbol,
                    market_data=market_data,
                    portfolio_state=portfolio_state,
                    time_ctx=time_ctx or {},
                )

                # Execute if confidence is high enough
                # High threshold ensures quality trades
                if decision.confidence and decision.confidence >= 0.5:
                    # Risk hooks can downsize/disable decisions pre-execution
                    decision = self._apply_skill_risk_hooks(
                        decision,
                        symbol=symbol,
                        portfolio_state=portfolio_state,
                        time_ctx=time_ctx or {},
                    )
                    success = await self.execute_trade(decision, current_price)

                    # V2: Record decision to memory
                    if success and self.enable_memory and self.memory_system and self.time_awareness:
                        from llm_trader.utils.trading_helpers import compress_market_features, format_decision_for_memory

                        phase = self.time_awareness.get_market_phase()
                        compressed_features = compress_market_features(market_data)

                        # Add episode to memory
                        self.memory_system.add_episode(
                            symbol=symbol,
                            action=decision.decision,
                            quantity=decision.quantity or 0,
                            price=current_price,
                            confidence=decision.confidence or 0.0,
                            reasoning=decision.reasoning or "",
                            market_phase=phase.value,
                            market_features=compressed_features,
                        )
                        self.logger.info(f"[V2] Decision recorded to episodic memory")
                else:
                    self.logger.info(f"[{symbol}] Decision: {decision.decision} (confidence too low: {decision.confidence:.2f})")

            except Exception as e:
                self.logger.info(f"[{symbol}] Error: {e}")
                traceback.print_exc()

        # V2.3: Take snapshots
        if hasattr(self, 'ledger') and self.session_id:
            # Position snapshot
            positions = []
            for sym, pos in self.portfolio.positions.items():
                positions.append({
                    "symbol": sym,
                    "position": pos.quantity,  # Can be negative for short
                    "avg_cost": pos.avg_cost,
                    "market_price": pos.current_price,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "realized_pnl_to_date": 0.0,  # Track separately if needed
                })
            self.ledger.snapshot_position(session_id=self.session_id, positions=positions)

            # Equity snapshot
            portfolio_state = self.portfolio.get_position_state()
            self.ledger.snapshot_equity(
                session_id=self.session_id,
                equity=portfolio_state['total_equity'],
                cash=portfolio_state['cash'],
                gross_exposure=abs(self.portfolio.total_market_value),
                net_exposure=self.portfolio.total_market_value,
                drawdown_to_date=0.0,  # Calculate if needed
            )

        # Print summary
        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"Cycle #{self.cycle_count} Complete")
        self.logger.info(f"{'='*80}\n")

    async def _call_provider_with_messages(self, messages: List[Dict], provider_kwargs: Dict) -> Optional[Dict]:
        """Call LLM provider with messages format

        This method adapts the universal prompt format to your provider's interface.
        """
        try:
            # Extract system and user messages
            system_msg = next((m['content'] for m in messages if m['role'] == 'system'), "")
            user_msg = next((m['content'] for m in messages if m['role'] == 'user'), "")

            # === Log API Request to detailed file (write directly) ===
            import json
            from datetime import datetime

            log_content = []
            log_content.append("="*80)
            log_content.append(f"API REQUEST - Cycle #{self.cycle_count}")
            log_content.append(f"Timestamp: {now_ny().strftime('%Y-%m-%d %H:%M:%S')}")
            log_content.append("="*80)
            log_content.append(f"Provider: {self.provider_name}")
            log_content.append(f"Model: {self.llm_provider.model if hasattr(self.llm_provider, 'model') else 'unknown'}")
            log_content.append("\nProvider kwargs:")
            log_content.append(json.dumps(provider_kwargs, indent=2, ensure_ascii=False))
            log_content.append("\n" + "-"*80)
            log_content.append("SYSTEM MESSAGE")
            log_content.append("-"*80)
            log_content.append(system_msg)
            log_content.append("\n" + "-"*80)
            log_content.append("USER MESSAGE")
            log_content.append("-"*80)

            # Format user message: extract and prettify JSON data
            if "DATA:" in user_msg:
                parts = user_msg.split("DATA:", 1)
                instruction_part = parts[0].strip()
                data_part = parts[1].strip() if len(parts) > 1 else ""

                log_content.append(instruction_part)
                log_content.append("\n" + "-"*80)
                log_content.append("DATA (formatted):")
                log_content.append("-"*80)

                # Try to format JSON data
                try:
                    data_json = json.loads(data_part)
                    formatted_json = json.dumps(data_json, indent=2, ensure_ascii=False)
                    log_content.append(formatted_json)
                except:
                    # If parsing fails, log as-is
                    log_content.append(data_part)
            else:
                log_content.append(user_msg)

            log_content.append("\n" + "="*80 + "\n")

            # Write to file directly
            with open(self.api_log_file, 'a', encoding='utf-8') as f:
                f.write('\n'.join(log_content) + '\n')

            # Call provider based on type
            if hasattr(self.llm_provider, 'chat_completion'):
                # Modern provider with messages support
                response = await self.llm_provider.chat_completion(messages=messages, **provider_kwargs)
            else:
                # Legacy provider - use analyze_market_with_prompt if available
                if hasattr(self.llm_provider, 'analyze_market_with_prompt'):
                    prompt = system_msg + "\n\n" + user_msg
                    response = await self.llm_provider.analyze_market_with_prompt("", prompt)
                else:
                    # Fallback to generate_response
                    prompt = system_msg + "\n\n" + user_msg
                    response = await self.llm_provider.generate_response(prompt)

            # === Log API Response to detailed file (write directly) ===
            response_content = []
            response_content.append("="*80)
            response_content.append(f"API RESPONSE - Cycle #{self.cycle_count}")
            response_content.append(f"Timestamp: {now_ny().strftime('%Y-%m-%d %H:%M:%S')}")
            response_content.append("="*80)
            response_content.append(f"Response type: {type(response).__name__}")

            if isinstance(response, dict):
                response_content.append("\nRaw response (dict):")
                response_content.append(json.dumps(response, indent=2, ensure_ascii=False))

                # Log reasoning separately if present (DeepSeek R1)
                if "_reasoning" in response and response["_reasoning"]:
                    response_content.append("\n" + "-"*80)
                    response_content.append("REASONING PROCESS (DeepSeek R1):")
                    response_content.append("-"*80)
                    response_content.append(response["_reasoning"])
                    response_content.append("-"*80)
            elif isinstance(response, LLMResponse):
                response_content.append("\nLLMResponse object:")
                response_content.append(f"  Decision: {response.decision}")
                response_content.append(f"  Confidence: {response.confidence}")
                response_content.append(f"  Quantity: {getattr(response, 'quantity', 0)}")
                response_content.append(f"  Reasoning: {response.reasoning}")
            else:
                response_content.append(f"\nRaw response: {response}")

            response_content.append("\n" + "="*80 + "\n\n")

            # Write to file directly
            with open(self.api_log_file, 'a', encoding='utf-8') as f:
                f.write('\n'.join(response_content) + '\n')

            # Parse response
            if isinstance(response, dict):
                return response
            elif isinstance(response, LLMResponse):
                return {
                    "action": response.decision,
                    "confidence": response.confidence,
                    "shares": getattr(response, 'quantity', 0),
                    "rationale": response.reasoning
                }
            else:
                # Try to parse as JSON
                import json
                if isinstance(response, str):
                    # Try to find JSON in the response
                    try:
                        parsed = json.loads(response)
                        return parsed
                    except json.JSONDecodeError:
                        # Try to extract JSON from markdown code blocks
                        import re
                        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
                        if json_match:
                            parsed = json.loads(json_match.group(1))
                            return parsed
                        # Last attempt: find any JSON object
                        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', response, re.DOTALL)
                        if json_match:
                            parsed = json.loads(json_match.group(0))
                            return parsed
                return None

        except Exception as e:
            self.logger.info(f"❌ Provider call error: {e}")
            # Log exception to API file
            with open(self.api_log_file, 'a', encoding='utf-8') as f:
                f.write(f"\n❌ Exception occurred: {e}\n")
                f.write(traceback.format_exc() + "\n")
            traceback.print_exc()
            return None

    async def run(self, interval: int = 60, max_cycles: Optional[int] = None, wait_for_market: bool = False, wait_for_premarket: bool = False, wait_for_afterhours: bool = False, wait_for_overnight: bool = False, heartbeat_file: Optional[str] = None):
        """Run the trading bot (V2.9: Support all trading sessions)

        Args:
            interval: Seconds between trading cycles (used as base interval if adaptive disabled)
            max_cycles: Max number of cycles (None = run forever)
            wait_for_market: If True, wait until RTH market opens (9:30 ET) before starting
            wait_for_premarket: If True, wait until premarket opens (4:00 ET) before starting
            wait_for_afterhours: If True, wait until after-hours opens (16:00 ET) before starting
            wait_for_overnight: If True, wait until overnight trading opens (20:00 ET) before starting
            heartbeat_file: Optional path to write heartbeat JSON periodically

        Note: Priority order if multiple flags specified:
            1. wait_for_overnight (20:00)
            2. wait_for_afterhours (16:00)
            3. wait_for_premarket (4:00)
            4. wait_for_market (9:30)
        """
        # Store interval for use in run_trading_cycle()
        self.cycle_interval_sec = interval

        # Setup heartbeat file if provided
        hb_path: Optional[Path] = Path(heartbeat_file) if heartbeat_file else None

        await self.start()

        # V2.9: Wait for session start (priority: overnight > afterhours > premarket > market)
        from datetime import datetime, timezone, timedelta

        # Wait for overnight open if requested (highest priority: 20:00 ET)
        if wait_for_overnight and self.time_manager:
            now_local = now_ny()
            seconds_to_wait = self.time_manager.seconds_until_overnight_open(now_local)

            if seconds_to_wait and seconds_to_wait > 0:
                hours = seconds_to_wait // 3600
                minutes = (seconds_to_wait % 3600) // 60
                seconds = seconds_to_wait % 60

                now_et = now_local
                target_et = now_et + timedelta(seconds=seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("⏰ WAITING FOR OVERNIGHT TRADING OPEN")
                self.logger.info("=" * 80)
                self.logger.info(f"Current time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info(f"Overnight opens:   {target_et.strftime('%Y-%m-%d %H:%M:%S')} ET (20:00)")
                self.logger.info(f"Time to wait:      {hours}h {minutes}m {seconds}s")
                self.logger.info("=" * 80)
                self.logger.info("\nBot will automatically start trading at overnight session open...")
                self.logger.info("Press Ctrl+C to cancel.\n")

                await asyncio.sleep(seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("✅ OVERNIGHT TRADING IS NOW OPEN - Starting trading bot")
                self.logger.info("=" * 80)
                self.logger.info("")

        # Wait for after-hours open if requested
        elif wait_for_afterhours and self.time_manager:
            now_local = now_ny()
            seconds_to_wait = self.time_manager.seconds_until_afterhours_open(now_local)

            if seconds_to_wait and seconds_to_wait > 0:
                hours = seconds_to_wait // 3600
                minutes = (seconds_to_wait % 3600) // 60
                seconds = seconds_to_wait % 60

                now_et = now_local
                target_et = now_et + timedelta(seconds=seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("⏰ WAITING FOR AFTER-HOURS TRADING OPEN")
                self.logger.info("=" * 80)
                self.logger.info(f"Current time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info(f"After-hours opens: {target_et.strftime('%Y-%m-%d %H:%M:%S')} ET (16:00)")
                self.logger.info(f"Time to wait:      {hours}h {minutes}m {seconds}s")
                self.logger.info("=" * 80)
                self.logger.info("\nBot will automatically start trading at after-hours open...")
                self.logger.info("Press Ctrl+C to cancel.\n")

                await asyncio.sleep(seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("✅ AFTER-HOURS TRADING IS NOW OPEN - Starting trading bot")
                self.logger.info("=" * 80)
                self.logger.info("")

        # Wait for premarket open if requested (takes priority over wait_for_market)
        elif wait_for_premarket and self.time_manager:
            now_local = now_ny()
            seconds_to_wait = self.time_manager.seconds_until_premarket_open(now_local)

            if seconds_to_wait and seconds_to_wait > 0:
                hours = seconds_to_wait // 3600
                minutes = (seconds_to_wait % 3600) // 60
                seconds = seconds_to_wait % 60

                now_et = now_local
                target_et = now_et + timedelta(seconds=seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("⏰ WAITING FOR PREMARKET OPEN")
                self.logger.info("=" * 80)
                self.logger.info(f"Current time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info(f"Premarket opens:   {target_et.strftime('%Y-%m-%d %H:%M:%S')} ET (4:00)")
                self.logger.info(f"Time to wait:      {hours}h {minutes}m {seconds}s")
                self.logger.info("=" * 80)
                self.logger.info("\nBot will automatically start trading at premarket open...")
                self.logger.info("Press Ctrl+C to cancel.\n")

                # Sleep until premarket open
                await asyncio.sleep(seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("✅ PREMARKET IS NOW OPEN - Starting trading bot")
                self.logger.info("=" * 80)
                self.logger.info("")

        # Wait for market open if requested (only if other wait flags not used)
        elif wait_for_market and self.time_manager:
            now_local = now_ny()
            seconds_to_wait = self.time_manager.seconds_until_market_open(now_local)

            if seconds_to_wait and seconds_to_wait > 0:
                hours = seconds_to_wait // 3600
                minutes = (seconds_to_wait % 3600) // 60
                seconds = seconds_to_wait % 60

                now_et = now_local
                target_et = now_et + timedelta(seconds=seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("⏰ WAITING FOR MARKET OPEN")
                self.logger.info("=" * 80)
                self.logger.info(f"Current time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info(f"Market opens at:   {target_et.strftime('%Y-%m-%d %H:%M:%S')} ET (9:30)")
                self.logger.info(f"Time to wait:      {hours}h {minutes}m {seconds}s")
                self.logger.info("=" * 80)
                self.logger.info("\nBot will automatically start trading at market open...")
                self.logger.info("Press Ctrl+C to cancel.\n")

                # Sleep until market open
                await asyncio.sleep(seconds_to_wait)

                self.logger.info("\n" + "=" * 80)
                self.logger.info("✅ MARKET IS NOW OPEN - Starting trading bot")
                self.logger.info("=" * 80)
                self.logger.info("")

        try:
            while self.running:
                await self.run_trading_cycle()

                if max_cycles and self.cycle_count >= max_cycles:
                    self.logger.info(f"\nReached max cycles ({max_cycles}), stopping...")
                    break

                # Check if we should stop trading (after close_at time)
                if self.time_manager:
                    # If it's past close_at time and we have no positions, stop the bot
                    now_local = now_ny()
                    if self.time_manager.should_flatten_now(now_local):
                        if len(self.portfolio.positions) == 0:
                            self.logger.info(f"\n{'='*80}")
                            self.logger.info(f"⏰ Trading hours ended (close_at: {self.time_manager.close_at})")
                            self.logger.info(f"   All positions closed. Stopping bot.")
                            self.logger.info(f"{'='*80}")
                            break
                        else:
                            self.logger.info(f"⚠️  Past close time but still have {len(self.portfolio.positions)} open positions")

                # Write heartbeat file (best-effort)
                if hb_path is not None:
                    try:
                        hb_path.parent.mkdir(parents=True, exist_ok=True)
                        import json as _J
                        from zoneinfo import ZoneInfo as _ZI
                        from datetime import timezone as _TZ

                        # V2.12: Calculate time since last good data
                        time_since_good_data_sec = None
                        if self.data_health["last_good_data_time"]:
                            time_since_good_data_sec = (datetime.now(_TZ.utc) - self.data_health["last_good_data_time"]).total_seconds()

                        payload = {
                            "cycle_count": self.cycle_count,
                            "timestamp_et": now_ny().isoformat(),
                            "status": self.data_health["status"].lower(),  # V2.12: Use data health status
                            "data_health": {  # V2.12: Detailed health info
                                "status": self.data_health["status"],
                                "consecutive_failures": self.data_health["consecutive_failures"],
                                "total_failures": self.data_health["failure_count"],
                                "last_good_data_time_utc": self.data_health["last_good_data_time"].isoformat() if self.data_health["last_good_data_time"] else None,
                                "time_since_good_data_sec": time_since_good_data_sec,
                                "recovery_progress": f"{self.data_health['recovery_checks_passed']}/{self.data_health_config['recovery_checks_needed']}" if self.data_health["status"] == "RECOVERING" else None,
                            },
                            "positions": len(self.portfolio.positions),
                            "provider": self.provider_name,
                            "session_id": self.session_id if hasattr(self, 'session_id') else None,
                        }
                        hb_path.write_text(_J.dumps(payload, indent=2), encoding="utf-8")
                    except Exception:
                        pass  # Non-critical, ignore errors

                # V2.12: Dynamic interval based on data health status
                if self.data_health["status"] == "UNHEALTHY":
                    # Unhealthy: check every 10s for quick recovery detection
                    wait_interval = 10
                    self.logger.info(f"⏱️  Data UNHEALTHY - checking every {wait_interval}s for recovery")
                elif self.data_health["status"] == "RECOVERING":
                    # Recovering: check every 30s for validation
                    wait_interval = 30
                    self.logger.info(f"⏱️  Data RECOVERING - validating every {wait_interval}s")
                elif self.enable_adaptive_frequency and self.time_awareness:
                    # V2: Use adaptive interval based on market phase
                    has_positions = len(self.portfolio.positions) > 0
                    wait_interval = self.time_awareness.get_adaptive_interval(has_positions)
                    phase = self.time_awareness.get_market_phase()
                    self.logger.info(f"[V2] Adaptive Frequency: {wait_interval}s "
                          f"(Phase: {phase.value}, Positions: {len(self.portfolio.positions)})")
                else:
                    # V1: Use fixed interval
                    wait_interval = interval

                self.logger.info(f"Waiting {wait_interval}s for next cycle...")
                await asyncio.sleep(wait_interval)

        except KeyboardInterrupt:
            self.logger.info("\nReceived interrupt signal")
        finally:
            await self.stop()


async def main():
    """Main entry point for testing"""
    config = Config()

    # Test with OpenAI
    bot = LLMTraderBot(
        provider_name="openai",
        symbols=["AAPL", "TSLA", "NVDA"],
        config=config,
        initial_cash=50000.0,
        dry_run=True,
    )

    await bot.run(interval=60, max_cycles=5)


if __name__ == "__main__":
    asyncio.run(main())
