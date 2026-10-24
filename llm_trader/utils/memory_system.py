"""Three-Layer Memory System for Autonomous LLM Trading Agent

Episodic Memory: Specific trade instances (what happened)
Policy Memory: Strategy parameters and preferences (how to trade)
Reflection Memory: Self-assessment and learning (why it worked/failed)
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from collections import deque
import json

NY_TZ = ZoneInfo("America/New_York")


@dataclass
class EpisodicMemory:
    """Records a specific trading episode"""
    timestamp: str
    symbol: str
    action: str  # BUY, SELL, HOLD
    quantity: int
    price: float
    confidence: float
    reasoning: str
    market_phase: str
    # Outcome (filled after trade execution)
    realized_pnl: Optional[float] = None
    hold_duration_minutes: Optional[int] = None
    outcome_verified: bool = False
    # Market context snapshot
    market_features: Dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> str:
        """Compress to single-line summary for prompt"""
        outcome = f"P&L=${self.realized_pnl:.2f}" if self.realized_pnl else "Pending"
        return f"[{self.timestamp}] {self.action} {self.symbol} x{self.quantity} @${self.price:.2f} ({self.market_phase}) → {outcome}"


@dataclass
class PolicyMemory:
    """Stores strategic parameters and preferences"""
    # Risk parameters
    preferred_position_size_pct: float = 0.10  # % of portfolio per position
    confidence_threshold: float = 0.6
    max_hold_duration_minutes: int = 240  # 4 hours

    # Trading preferences (learned over time)
    preferred_symbols: List[str] = field(default_factory=list)
    avoid_symbols: List[str] = field(default_factory=list)
    preferred_market_phases: List[str] = field(default_factory=list)

    # Performance tracking
    win_rate: float = 0.0
    avg_pnl_per_trade: float = 0.0
    best_performing_strategy: Optional[str] = None

    # Meta parameters
    learning_rate: float = 0.1  # How quickly to update策略
    exploration_rate: float = 0.2  # % of trades for exploration

    last_updated: str = field(default_factory=lambda: datetime.now(NY_TZ).isoformat())

    def update_from_reflection(self, reflection: Dict[str, Any]):
        """Update policy based on reflection insights"""
        self.last_updated = datetime.now(NY_TZ).isoformat()

        # Update win rate if provided
        if "win_rate" in reflection:
            self.win_rate = reflection["win_rate"]

        # Update preferred symbols
        if "top_symbols" in reflection:
            self.preferred_symbols = reflection["top_symbols"][:5]

        # Update best strategy
        if "best_strategy" in reflection:
            self.best_performing_strategy = reflection["best_strategy"]

    def to_prompt_summary(self) -> str:
        """Format for LLM prompt"""
        return f"""STRATEGY POLICY:
- Position Size: {self.preferred_position_size_pct*100:.0f}% per trade
- Confidence Threshold: {self.confidence_threshold:.1f}
- Win Rate: {self.win_rate*100:.1f}%
- Avg P&L: ${self.avg_pnl_per_trade:.2f}
- Preferred Symbols: {', '.join(self.preferred_symbols[:3]) if self.preferred_symbols else 'None yet'}
- Best Strategy: {self.best_performing_strategy or 'Exploring'}
"""


@dataclass
class ReflectionMemory:
    """Stores self-assessment and learnings"""
    timestamp: str
    period: str  # "midday", "end_of_day", "weekly"
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    insights: List[str] = field(default_factory=list)
    mistakes_identified: List[str] = field(default_factory=list)
    improvements_planned: List[str] = field(default_factory=list)

    # Meta insights
    best_performing_conditions: Optional[str] = None
    worst_performing_conditions: Optional[str] = None

    def to_summary(self) -> str:
        """Compress reflection to summary"""
        win_rate = self.winning_trades / max(self.total_trades, 1) * 100
        return f"""[{self.period.upper()} REFLECTION - {self.timestamp}]
Trades: {self.total_trades} (W:{self.winning_trades}, L:{self.losing_trades}, WR:{win_rate:.1f}%)
P&L: ${self.total_pnl:.2f}
Key Insights: {'; '.join(self.insights[:2])}
Planned Improvements: {'; '.join(self.improvements_planned[:2])}
"""


class MemorySystem:
    """Manages three-layer memory for autonomous trading agent"""

    def __init__(self, max_episodic_memories: int = 50):
        """Initialize memory system

        Args:
            max_episodic_memories: Max number of episodic memories to keep
        """
        self.episodic_memories: deque = deque(maxlen=max_episodic_memories)
        self.policy_memory = PolicyMemory()
        self.reflection_memories: List[ReflectionMemory] = []

        self.total_trades = 0
        self.total_winning_trades = 0
        self.total_pnl = 0.0

    def add_episode(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        confidence: float,
        reasoning: str,
        market_phase: str,
        market_features: Dict[str, Any] = None,
    ) -> EpisodicMemory:
        """Record a new trading episode

        Args:
            symbol: Stock symbol
            action: BUY, SELL, HOLD
            quantity: Number of shares
            price: Execution price
            confidence: LLM confidence score
            reasoning: LLM reasoning
            market_phase: Market phase at decision time
            market_features: Market context snapshot

        Returns:
            Created EpisodicMemory object
        """
        episode = EpisodicMemory(
            timestamp=datetime.now(NY_TZ).isoformat(),
            symbol=symbol,
            action=action,
            quantity=quantity,
            price=price,
            confidence=confidence,
            reasoning=reasoning,
            market_phase=market_phase,
            market_features=market_features or {},
        )

        self.episodic_memories.append(episode)
        if action != "HOLD":
            self.total_trades += 1

        return episode

    def update_episode_outcome(
        self,
        episode: EpisodicMemory,
        realized_pnl: float,
        hold_duration_minutes: int = None,
    ):
        """Update episode with outcome after trade completes

        Args:
            episode: Episode to update
            realized_pnl: Realized profit/loss
            hold_duration_minutes: How long position was held
        """
        episode.realized_pnl = realized_pnl
        episode.hold_duration_minutes = hold_duration_minutes
        episode.outcome_verified = True

        # Update statistics
        self.total_pnl += realized_pnl
        if realized_pnl > 0:
            self.total_winning_trades += 1

        # Update policy memory stats
        if self.total_trades > 0:
            self.policy_memory.win_rate = self.total_winning_trades / self.total_trades
            self.policy_memory.avg_pnl_per_trade = self.total_pnl / self.total_trades

    def add_reflection(
        self,
        period: str,
        insights: List[str],
        mistakes: List[str],
        improvements: List[str],
    ) -> ReflectionMemory:
        """Add a reflection session

        Args:
            period: Reflection period (midday, end_of_day, weekly)
            insights: Key insights discovered
            mistakes: Mistakes identified
            improvements: Planned improvements

        Returns:
            Created ReflectionMemory object
        """
        # Calculate stats for this period
        recent_episodes = [e for e in self.episodic_memories if e.outcome_verified]

        winning = sum(1 for e in recent_episodes if e.realized_pnl and e.realized_pnl > 0)
        losing = sum(1 for e in recent_episodes if e.realized_pnl and e.realized_pnl <= 0)
        total_pnl = sum(e.realized_pnl for e in recent_episodes if e.realized_pnl)

        reflection = ReflectionMemory(
            timestamp=datetime.now(NY_TZ).isoformat(),
            period=period,
            total_trades=len(recent_episodes),
            winning_trades=winning,
            losing_trades=losing,
            total_pnl=total_pnl,
            insights=insights,
            mistakes_identified=mistakes,
            improvements_planned=improvements,
        )

        self.reflection_memories.append(reflection)

        # Update policy memory based on reflection
        self.policy_memory.update_from_reflection({
            "win_rate": winning / max(len(recent_episodes), 1),
            "avg_pnl": total_pnl / max(len(recent_episodes), 1),
        })

        return reflection

    def get_recent_episodes(self, n: int = 20) -> List[EpisodicMemory]:
        """Get most recent N episodes

        Args:
            n: Number of episodes to retrieve

        Returns:
            List of recent episodes
        """
        return list(self.episodic_memories)[-n:]

    def get_recent_reflections(self, n: int = 3) -> List[ReflectionMemory]:
        """Get most recent N reflections

        Args:
            n: Number of reflections to retrieve

        Returns:
            List of recent reflections
        """
        return self.reflection_memories[-n:]

    def compress_for_prompt(
        self,
        max_episodes: int = 20,
        max_reflections: int = 2,
    ) -> str:
        """Compress memory into compact prompt format

        Args:
            max_episodes: Max episodes to include
            max_reflections: Max reflections to include

        Returns:
            Formatted memory string for LLM prompt
        """
        prompt = "=" * 80 + "\n"
        prompt += "AGENT MEMORY SYSTEM\n"
        prompt += "=" * 80 + "\n\n"

        # Policy Memory
        prompt += self.policy_memory.to_prompt_summary()
        prompt += "\n"

        # Recent Reflections
        recent_reflections = self.get_recent_reflections(max_reflections)
        if recent_reflections:
            prompt += "RECENT REFLECTIONS:\n"
            for reflection in recent_reflections:
                prompt += reflection.to_summary() + "\n"
            prompt += "\n"

        # Recent Episodes
        recent_episodes = self.get_recent_episodes(max_episodes)
        verified_episodes = [e for e in recent_episodes if e.outcome_verified]

        if verified_episodes:
            prompt += f"RECENT TRADE HISTORY (last {len(verified_episodes)} trades):\n"
            for ep in verified_episodes[-10:]:  # Last 10 only
                prompt += ep.to_summary() + "\n"
            prompt += "\n"

        prompt += "=" * 80 + "\n"

        return prompt

    def save_to_file(self, filepath: str):
        """Save memory system to JSON file

        Args:
            filepath: Path to save file
        """
        data = {
            "timestamp": datetime.now(NY_TZ).isoformat(),
            "total_trades": self.total_trades,
            "total_winning_trades": self.total_winning_trades,
            "total_pnl": self.total_pnl,
            "policy_memory": asdict(self.policy_memory),
            "episodic_memories": [asdict(e) for e in self.episodic_memories],
            "reflection_memories": [asdict(r) for r in self.reflection_memories],
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

    def load_from_file(self, filepath: str):
        """Load memory system from JSON file

        Args:
            filepath: Path to load file
        """
        with open(filepath, "r") as f:
            data = json.load(f)

        self.total_trades = data["total_trades"]
        self.total_winning_trades = data["total_winning_trades"]
        self.total_pnl = data["total_pnl"]

        # Load policy
        self.policy_memory = PolicyMemory(**data["policy_memory"])

        # Load episodes
        self.episodic_memories = deque(
            [EpisodicMemory(**e) for e in data["episodic_memories"]],
            maxlen=self.episodic_memories.maxlen
        )

        # Load reflections
        self.reflection_memories = [
            ReflectionMemory(**r) for r in data["reflection_memories"]
        ]
