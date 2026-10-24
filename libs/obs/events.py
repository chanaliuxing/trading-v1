# libs/obs/events.py
"""
Unified Event Contract v1.1
All structured logging must use these event names and stages.
"""
from enum import Enum


class EventName(str, Enum):
    """
    统一事件契约 (Unified Event Contract)
    所有结构化日志必须使用此枚举中的事件名
    """
    # --- 交易员驾驶舱事件 ---
    POSITION_SNAPSHOT = "position_snapshot"
    ACCOUNT_SNAPSHOT = "account_snapshot"
    MARKET_LEADERS = "market_leaders"

    # --- 运维驾驶舱事件 ---
    GATE_SNAPSHOT = "gate_snapshot"
    ORDER_LIFECYCLE = "order_lifecycle"
    BACKFILL_STATUS = "backfill_status"
    SERVICE_HEARTBEAT = "service_heartbeat"

    # --- 通用事件 ---
    SERVICE_START = "service_start"
    SERVICE_STOP = "service_stop"
    ERROR = "error"
    WARNING = "warning"


class OrderLifecycleStage(str, Enum):
    """订单生命周期阶段"""
    CREATED = "created"
    RISK_OK = "risk_ok"
    RISK_FAIL = "risk_fail"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Gate(str, Enum):
    """决策门控"""
    A = "A"
    B = "B"
    C = "C"
