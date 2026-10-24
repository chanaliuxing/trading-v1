# QQQ Data Quality And MA Parity TODO

## Goal
- [ ] Make intraday market data at least 99% clean for strategy and backtest use.
- [ ] Make system MA calculation match a professional charting profile exactly.
- [ ] Do not change current live default data path until validation is complete.
- [ ] Keep all changes auditable, reversible, and isolated from existing live strategies.

## Scope
- [ ] Cover `QQQ` first, then extend to `SPY` and other core ETFs after validation.
- [ ] Focus on `5s -> 1m -> MA` path.
- [ ] Treat raw data retention and clean data serving as separate concerns.
- [ ] Do not change order execution, broker sync, or live strategy logic in this phase.

## Confirmed Problems
- [ ] `IB ticker.last` can enter `5s` aggregation with only `> 0` validation.
- [ ] Abnormal `5s` bars can flow into `bars_1m_ibkr` through `agg_5s_mv`.
- [ ] Current replay MA uses implicit rules:
  - standard 1m bars
  - close price
  - SMA
  - `is_rth = 1`
  - cross-day rolling window
- [ ] Current MA profile is implementation detail, not an explicit contract.

## Implementation TODO

## Phase 1: Data Quality Guardrails
- [ ] Add trade sanity validation before `5s` bucket update in [./services/market/aggregator/five_sec_agg.py](./services/market/aggregator/five_sec_agg.py).
- [ ] Validate incoming `last` against available quote context when bid/ask exist.
- [ ] Validate incoming `last` against previous accepted trade for the same symbol.
- [ ] Reject or quarantine trades with impossible jump magnitude.
- [ ] Add reason codes for rejected trades:
  - `NON_POSITIVE_PRICE`
  - `NO_SIZE`
  - `TOO_FAR_FROM_QUOTE`
  - `TOO_FAR_FROM_LAST_GOOD_TRADE`
  - `SPIKE_WITHOUT_QUOTE_SUPPORT`

## Phase 2: 5s Bar Quality Gate
- [ ] Add `5s` bar-level sanity check before publish.
- [ ] Detect impossible OHLC structure within one bar.
- [ ] Detect bar range outliers relative to recent clean bars.
- [ ] Mark bad bars as quarantined instead of publishing to clean downstream path.
- [ ] Preserve raw bad bars in audit path for later diagnosis.

## Phase 3: Clean Data Path
- [ ] Introduce a clean `5s` serving path for strategy/backtest consumption.
- [ ] Build clean `1m` only from clean `5s`.
- [ ] Keep existing raw tables untouched.
- [ ] Keep live default readers on current path until acceptance passes.

## Phase 4: MA Profile Standardization
- [ ] Add explicit MA profile definition for replay and strategy consumers.
- [ ] Support at minimum:
  - `standard_close_rth_sma`
  - `standard_close_extended_sma`
- [ ] Make profile fields explicit:
  - `bar_type`
  - `price_source`
  - `session_mode`
  - `ma_mode`
  - `cross_day`
  - `warmup_bars`
- [ ] Ensure warmup preload is sufficient for MA200 at market open.

## Phase 5: Audit And Tooling
- [ ] Add anomaly report for day-level `5s` and `1m` bars.
- [ ] Add clean-vs-raw comparison report.
- [ ] Add MA parity check script against selected benchmark timestamps.
- [ ] Add daily completeness and anomaly summary for core ETFs.

## Test Plan

## Unit Tests
- [ ] `five_sec_agg` rejects non-positive and absurd trade prices.
- [ ] `five_sec_agg` accepts normal trades near bid/ask and recent last trade.
- [ ] `five_sec_agg` quarantines impossible OHLC bars.
- [ ] MA profile builder computes exact SMA values for fixed fixtures.
- [ ] Warmup loader returns enough history for MA200 at first decision bar.

## Integration Tests
- [ ] Raw abnormal `5s` inputs do not appear in clean `5s`.
- [ ] Clean `1m` excludes upstream bad `5s`.
- [ ] Existing raw path remains readable and unchanged.
- [ ] Replay can switch between raw profile and clean profile without breaking baseline flow.
- [ ] `QQQ` and `SPY` replay runs complete with clean profile enabled.

## Regression Tests
- [ ] Existing live strategy tests still pass without config changes.
- [ ] Existing replay baseline results remain unchanged on raw profile.
- [ ] Current execution, reconciliation, and Telegram tests remain unaffected.

## Manual Verification
- [ ] Verify `QQQ 2026-03-25 11:36-11:44` bad bars are excluded from clean path.
- [ ] Verify `QQQ 2026-03-25 11:11-11:14` MA values under chosen profile.
- [ ] Verify MA200 is available immediately after open with warmup preload.
- [ ] Verify selected benchmark timestamps against target chart profile.

## Acceptance Checklist

## Data Quality Gates
- [ ] Clean path contains no obviously impossible ETF intraday prices.
- [ ] Clean path anomaly rate is below `0.1%` for core ETF intraday bars.
- [ ] Known bad `QQQ 2026-03-25` bars are quarantined and auditable.
- [ ] Raw data remains preserved for forensic analysis.

## MA Parity Gates
- [ ] MA profile is explicit and versioned.
- [ ] `MA5/10/20/200` match the target chart profile at sampled timestamps.
- [ ] MA parity check includes both mid-session and near-open timestamps.
- [ ] MA200 does not depend on same-day accumulation only.

## Isolation Gates
- [ ] No live strategy default configuration is changed.
- [ ] No existing live module changes data source by default.
- [ ] Replay/backtest can opt into clean path independently.
- [ ] Rollback is one config switch, not a code rollback.

## Operational Gates
- [ ] Data anomalies generate visible audit output.
- [ ] Failures in clean path do not block raw ingestion.
- [ ] Backfill and replay tooling can target clean path deterministically.

## Rollout Order
1. [ ] Implement anomaly rejection and quarantine in `5s` path.
2. [ ] Add clean `5s` and clean `1m` path.
3. [ ] Add MA profile abstraction and warmup checks.
4. [ ] Add unit and integration tests.
5. [ ] Run `QQQ` single-day validation.
6. [ ] Run `QQQ` multi-day validation.
7. [ ] Run `SPY` comparison validation.
8. [ ] Only after acceptance, evaluate optional live migration plan.

## Out Of Scope
- [ ] Changing current live strategy entries or exits.
- [ ] Changing broker execution or reconciliation behavior.
- [ ] Rebuilding option execution models in this phase.
- [ ] Replacing raw IBKR market data storage.

## Deliverables
- [ ] Code changes for clean data guardrails.
- [ ] Test coverage for abnormal tick and MA parity.
- [ ] Day-level anomaly audit report.
- [ ] MA parity benchmark report.
- [ ] Final cutover recommendation after acceptance review.
