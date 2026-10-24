#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT_DIR/venv_ml/bin/python" ]]; then
  PY="$ROOT_DIR/venv_ml/bin/python"
elif [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PY="$ROOT_DIR/venv/bin/python"
else
  PY="python3"
fi

MODE="${1:-full}"
if [[ $# -gt 0 ]]; then
  shift
fi

COMMON_ENV=(
  TRADING_TEST_ALLOW_LIVE_IBKR=0
)

FAST_TESTS=(
  tests/unit/safety/test_no_live_ibkr_in_unit_tests.py
  tests/unit/services/test_ib_connection_manager.py::test_planned_restart_connection_refused_within_grace_does_not_mark_manual_required
  tests/unit/services/test_ib_connection_manager.py::test_connection_refused_outside_grace_enters_recoverable_non_manual_circuit
  tests/unit/services/test_ib_connection_manager.py::test_tcp_open_handshake_timeout_can_mark_gateway_api_half_open
  tests/unit/services/test_ib_connection_manager.py::test_get_market_ib_blocks_real_connect_after_api_preflight_timeout
  tests/unit/services/test_ib_connection_manager.py::test_connection_refused_probe_does_not_mark_manual_required
  tests/unit/services/test_ib_connection_manager.py::test_manual_required_refusal_latch_allows_authorized_recovery_probe
  tests/unit/services/test_ib_connection_manager.py::test_manual_required_half_open_still_blocks_recovery_probe
  tests/unit/services/test_ib_connection_manager.py::test_api_handshake_probe_rejects_unauthorized_recovery_caller
  tests/unit/services/test_ib_connection_manager.py::test_api_handshake_probe_does_not_clear_open_gateway_circuit
  tests/unit/services/test_ib_connection_manager.py::test_wait_until_ready_probes_gateway_circuit_open_for_recovery
  tests/unit/services/test_ib_connection_manager.py::test_authorized_operator_proof_clears_manual_required_after_time_positions_orders
  tests/unit/services/test_ib_connection_manager.py::test_manual_proof_requires_operator_ack_when_manual_required
  tests/unit/services/test_ib_connection_manager.py::test_manual_proof_concurrent_callers_only_one_constructs_ib
  tests/unit/services/test_ib_connection_manager.py::test_manual_proof_unauthorized_source_does_not_construct_ib
  tests/unit/services/test_ib_connection_manager.py::test_manual_proof_failure_uses_report_aborted_not_report_failure
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py::test_operator_proof_cli_dry_run_does_not_construct_ib
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py::test_operator_proof_cli_requires_ack
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py::test_operator_proof_cli_requires_authorized_source
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py::test_operator_proof_cli_lease_denied_does_not_construct_ib
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py::test_operator_proof_cli_success_clears_manual_required
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py::test_operator_proof_cli_failure_does_not_clear_manual_required
  tests/unit/tool/test_run_runtime_operations_watchdog.py::test_manual_required_visible_as_manual_recovery_required_not_generic_startup_retry
  tests/unit/tool/test_run_runtime_operations_watchdog.py::test_operator_proof_visibility_in_production_state
  tests/unit/tool/test_run_runtime_operations_watchdog.py::test_runtime_freshness_stale_keeps_production_no_go_for_ib_recovery_modules
  tests/unit/services/test_reconnect_gatekeeper.py::TestGatewayCircuit
)

FULL_TESTS=(
  tests/unit/services/test_ib_connection_manager.py
  tests/unit/services/test_reconnect_gatekeeper.py
  tests/unit/services/exec/test_ib_adapter_premarket_reconnect.py
  tests/unit/services/test_ib_request_governor.py
  tests/unit/services/test_ib_request_governance_chaos.py
  tests/unit/tool/test_check_ib_request_governance.py
  tests/unit/services/test_system_health_controller.py
  tests/e2e/test_runtime_recovery_drill.py
  tests/integration/test_run_system_startup_smoke.py
  tests/unit/safety/test_no_live_ibkr_in_unit_tests.py
  tests/unit/scripts/test_operator_ib_gateway_recovery_proof.py
)

case "$MODE" in
  fast)
    TESTS=("${FAST_TESTS[@]}")
    ;;
  full)
    TESTS=("${FULL_TESTS[@]}")
    ;;
  *)
    echo "usage: $0 [fast|full]" >&2
    exit 2
    ;;
esac

echo "IB reconnect regression mode: $MODE"
echo "Python: $PY"
echo "Live IBKR disabled for this regression suite."

env "${COMMON_ENV[@]}" "$PY" -m pytest -q "${TESTS[@]}" "$@"
