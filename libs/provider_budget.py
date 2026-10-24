"""
Provider Budget Tracker

Tracks API call usage per provider with daily limits.
Storage: Redis for fast lookups and automatic expiration.
"""
import redis
import datetime
import pytz
from typing import Optional, Dict
from libs.config import config
from libs.logger import get_logger

logger = get_logger(__name__)

# Redis keys: provider_budget:YYYY-MM-DD:provider_name
KEY_PREFIX = "provider_budget"

def _get_redis() -> Optional[redis.Redis]:
    """Get Redis connection."""
    try:
        r = redis.Redis(
            host=config.get('redis.host', 'localhost'),
            port=config.get('redis.port', 6379),
            db=config.get('redis.db', 0),
            decode_responses=True
        )
        r.ping()
        return r
    except Exception as e:
        logger.warning("redis_unavailable", err=str(e))
        return None


def _get_today_key(provider: str) -> str:
    """Get Redis key for today's budget tracking."""
    et = pytz.timezone('America/New_York')
    today = datetime.datetime.now(et).date().isoformat()
    return f"{KEY_PREFIX}:{today}:{provider}"


def _get_daily_limit(provider: str) -> int:
    """Get daily call limit for provider from config."""
    try:
        limits = config.get('providers.budgets.daily_max_calls', {})
        return limits.get(provider, 1000)  # Default: 1000 calls
    except Exception:
        return 1000


def check_budget(provider: str) -> Dict[str, any]:
    """Check if provider has remaining budget.

    Returns:
        {
            'allowed': bool,         # Whether call is allowed
            'used': int,             # Calls used today
            'limit': int,            # Daily limit
            'remaining': int,        # Calls remaining
            'resets_at': str         # Next reset time (midnight ET)
        }
    """
    r = _get_redis()
    limit = _get_daily_limit(provider)

    if r is None:
        # Redis unavailable - allow but warn
        logger.warning("budget_check_degraded", provider=provider, reason="redis_unavailable")
        return {
            'allowed': True,
            'used': 0,
            'limit': limit,
            'remaining': limit,
            'resets_at': 'unknown',
            'degraded': True
        }

    key = _get_today_key(provider)

    try:
        used = int(r.get(key) or 0)
        remaining = max(0, limit - used)
        allowed = used < limit

        # Calculate next reset (midnight ET)
        et = pytz.timezone('America/New_York')
        now = datetime.datetime.now(et)
        tomorrow = now.date() + datetime.timedelta(days=1)
        next_reset = et.localize(datetime.datetime.combine(tomorrow, datetime.time.min))

        return {
            'allowed': allowed,
            'used': used,
            'limit': limit,
            'remaining': remaining,
            'resets_at': next_reset.isoformat(),
            'degraded': False
        }
    except Exception as e:
        logger.error("budget_check_failed", provider=provider, err=str(e))
        # Fail open - allow call but log error
        return {
            'allowed': True,
            'used': 0,
            'limit': limit,
            'remaining': limit,
            'resets_at': 'unknown',
            'error': str(e)
        }


def increment_usage(provider: str) -> Dict[str, any]:
    """Increment provider usage counter.

    Returns same dict as check_budget() but after increment.
    """
    r = _get_redis()

    if r is None:
        logger.warning("usage_tracking_degraded", provider=provider)
        return check_budget(provider)

    key = _get_today_key(provider)

    try:
        # Increment and set expiration (36 hours - covers timezone edge cases)
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 36 * 3600)  # 36 hours TTL
        used, _ = pipe.execute()

        logger.info("provider_call_tracked", provider=provider, used=used)

        # Return updated budget status
        return check_budget(provider)

    except Exception as e:
        logger.error("usage_increment_failed", provider=provider, err=str(e))
        return check_budget(provider)


def get_all_budgets() -> Dict[str, Dict]:
    """Get budget status for all providers.

    Returns:
        {
            'deepseek_nvidia': {...},
            'deepseek': {...},
            ...
        }
    """
    try:
        providers = config.get('providers.budgets.daily_max_calls', {}).keys()
        return {p: check_budget(p) for p in providers}
    except Exception as e:
        logger.error("get_all_budgets_failed", err=str(e))
        return {}


def get_daily_costs() -> Dict[str, float]:
    """Calculate daily costs for all providers.

    Returns:
        {
            'deepseek_nvidia': 2.80,  # $2.80
            'deepseek': 1.05,
            'total': 3.85
        }
    """
    r = _get_redis()

    if r is None:
        return {'error': 'redis_unavailable'}

    try:
        budgets = get_all_budgets()
        costs_per_call = config.get('providers.budgets.cost_per_call_usd', {})

        costs = {}
        total = 0.0

        for provider, budget in budgets.items():
            used = budget.get('used', 0)
            cost_per_call = costs_per_call.get(provider, 0.0)
            cost = used * cost_per_call
            costs[provider] = round(cost, 2)
            total += cost

        costs['total'] = round(total, 2)
        return costs

    except Exception as e:
        logger.error("cost_calculation_failed", err=str(e))
        return {'error': str(e)}


def reset_budget(provider: str) -> bool:
    """Manually reset budget for a provider (emergency use)."""
    r = _get_redis()

    if r is None:
        return False

    try:
        key = _get_today_key(provider)
        r.delete(key)
        logger.warning("budget_manually_reset", provider=provider)
        return True
    except Exception as e:
        logger.error("budget_reset_failed", provider=provider, err=str(e))
        return False
