from __future__ import annotations

import asyncio
import pytest


@pytest.fixture(autouse=True)
def cleanup_qqq_stop_audit_tasks():
    yield
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        return
    if loop.is_closed():
        return
    pending = [
        t
        for t in asyncio.all_tasks(loop)
        if (not t.done()) and str(t.get_name()).startswith("qqq_stop_audit:")
    ]
    if not pending:
        return
    for task in pending:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

