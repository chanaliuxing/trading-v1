# Trading System Audit Gate - 2026-05-12

Mode: `quick`
Verdict: **PASS**

## Summary

| Layer | Verdict |
|---|---|
| No-live guard | `PASS` |
| Static scan | `PASS` |
| Tests | `PASS` |
| Runtime artifacts | `SKIPPED` |
| Paper/shadow diagnostics | `SKIPPED` |

## Blockers

- none

## Warnings

- none

## Commands

| Name | Status | Summary |
|---|---|---|
| `no_live_guard` | `PASS` | configfile: pytest.ini
plugins: anyio-4.11.0, asyncio-0.21.1, mock-3.12.0
asyncio: mode=Mode.AUTO
collected 4 items

tests/unit/safety/test_no_live_ibkr_in_unit_tests.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m                [100%][0m

[33m========================= [32m4 passed[0m, [33m[1m1 warning[0m[33m in 0.15s[0m[33m =========================[0m |
| `git_diff_check` | `PASS` |  |
| `exec_contract` | `PASS` | tests/unit/exec/test_safety_kernel_oco_persistence.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m [ 88%]
[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m                                                                   [ 90%][0m
tests/unit/exec/test_safety_kernel_position_accounting.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m   [ 94%][0m
tests/unit/exec/test_safety_plan_key_isolation.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m                 [ 96%][0m
tests/unit/exec/test_stop_loss_owner_position_binding.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m         [ 99%][0m
tests/unit/exec/test_trace_utils.py [32m.[0m[32m.[0m[33m                                   [100%][0m

[33m======================= [32m294 passed[0m, [33m[1m5 warnings[0m[33m in 4.25s[0m[33m ========================[0m |
| `services_contract` | `PASS` |   File "./venv_ml/lib/python3.11/site-packages/aiokafka/producer/producer.py", line 344, in __del__
    self._loop.call_exception_handler(context)
  File "/opt/homebrew/Cellar/python@3.11/3.11.14_3/Frameworks/Python.framework/Versions/3.11/lib/python3.11/asyncio/base_events.py", line 1811, in call_exception_handler
    self.default_exception_handler(context)
  File "/opt/homebrew/Cellar/python@3.11/3.11.14_3/Frameworks/Python.framework/Versions/3.11/lib/python3.11/asyncio/base_events.py", line 1785, in default_exception_handler
    logger.error('\n'.join(log_lines), exc_info=exc_info)
Message: 'Unclosed AIOKafkaProducer\nproducer: <aiokafka.producer.producer.AIOKafkaProducer object at 0x120173c50>'
Arguments: () |
| `ib_reconnect_fast` | `PASS` | 
tests/unit/safety/test_no_live_ibkr_in_unit_tests.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m                [ 12%][0m
tests/unit/services/test_ib_connection_manager.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m        [ 59%][0m
tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m     [ 78%][0m
tests/unit/tool/test_run_runtime_operations_watchdog.py [32m.[0m[32m.[0m[32m.[0m[33m              [ 87%][0m
tests/unit/services/test_reconnect_gatekeeper.py [32m.[0m[32m.[0m[32m.[0m[32m.[0m[33m                    [100%][0m

[33m======================== [32m32 passed[0m, [33m[1m1 warning[0m[33m in 1.13s[0m[33m =========================[0m |
