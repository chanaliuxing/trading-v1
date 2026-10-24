# Vegas DLB / TTL / Edge-Cost Measurement Contract

## Purpose

Vegas parameter changes require evidence. This contract adds an offline
measurement report for `vegas_hma_setup_trigger_risk_v12_spy_001` without
changing DLB, TTL, edge-cost thresholds, strategy logic, or broker behavior.

## Measurement Record

Each record captures the observable chain:

- `signal_ts`
- `decision_ts`
- `validation_ts`
- `submit_intent_ts`
- `broker_submit_ts`
- `final_status`
- `rejection_reasons`
- `dlb_limit_ms`
- `ttl_limit_ms`
- latency fields for signal, decision, validation, and submit boundaries
- `dlb_exceeded` and `ttl_exceeded`
- `edge_value`, `cost_value`, and `edge_minus_cost`
- readiness fields: data, system, risk, execution, tradability

Missing timestamps remain `null`. Latency is computed only when both endpoints
exist.

## Report

The offline report is written to:

`reports/vegas_dlb_edge_measurement/vegas_dlb_edge_measurement_latest.json`

It includes:

- status counts
- latency percentiles
- DLB breach count/rate/worst elapsed
- edge-cost rejection count/rate/distribution
- primary rejection driver classification
- measurement-only recommendations

## Classification

- `INSUFFICIENT_SAMPLE`: sample count below configured minimum.
- `LATENCY_DLB_SYSTEMIC`: DLB/stale decision breach rate is high.
- `EDGE_COST_SYSTEMIC`: edge-insufficient rate is high while latency is not the dominant issue.
- `DATA_READINESS`: data readiness is the dominant failure.
- `SYSTEM_RISK_BLOCKER`: system or risk readiness is the dominant failure.
- `MIXED`: multiple drivers are elevated.

## No Tuning Rule

This batch does not tune parameters. Do not change DLB, TTL, edge-cost, entry
logic, validator rules, or broker submission behavior from this report.

## Acceptance Criteria Before Future Tuning

- `sample_count` reaches the configured minimum.
- timestamp chain coverage is at least 95%.
- broker truth, position safety, tradability, and runtime readiness are green.
- latency p95/p99 are measured.
- edge/cost distribution is measured.
- approved-to-fill path and fill quality are known.

Potential future calibration must be evidence based:

- TTL candidate from p99 signal-to-validation plus safety buffer.
- DLB candidate from measured decision latency percentile.
- edge threshold from spread, slippage, fees, and realized fill quality.
