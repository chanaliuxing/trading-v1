# SFS System Upgrade Analysis - Documentation Index
**Created**: 2025-10-21
**Scope**: Complete analysis of ClickHouse SFS schema additions and premarket strategy adaptations

---

## Document Overview

This analysis package contains comprehensive documentation of the recent system upgrade (Oct 19-21, 2025) that introduced the **Sliding Feature Service (SFS)** layer for sub-second trading decision-making.

### Quick Links

1. **SFS_SYSTEM_UPGRADE_ANALYSIS.md** (831 lines, 28 KB)
   - Comprehensive 13-section deep dive
   - Technical architecture details
   - Complete schema specifications
   - Service implementations
   - Integration checklist
   - Risk mitigation strategies

2. **SFS_QUICK_REFERENCE.md** (275 lines, 7.2 KB)
   - Executive summary of changes
   - Quick command reference
   - Status dashboard
   - Performance metrics
   - Testing checklist

---

## Key Facts (5-Minute Summary)

### What Changed

1. **New ClickHouse Database**: `sfs` namespace
   - `sfs.sfs_5s` - Sliding 5-second OHLCV bars (ReplacingMergeTree, 30-day TTL)
   - `sfs.sfs_1m_tumble` - 1-minute tumbling aggregation (MergeTree, 365-day TTL)
   - `trading.sfs_features` - Unified feature store (ReplacingMergeTree, 90-day TTL)

2. **New Services** (Phase 1-2)
   - **SFS-1s Service** (`services/market/sfs_1s_service.py`): Kafka ticks → Redis 1s bars (✅ Complete)
   - **SFS-5s Service** (`services/market/sfs_5s_service.py`): Redis 1s → ClickHouse 5s features (⚠️ Stub)
   - **SFS Facade** (`libs/dal/sfs_facade.py`): Strategy-friendly Redis reader (✅ Complete)

3. **Multi-Strategy Enablement** (Oct 20-21)
   - Before: 1 strategy (TSLA only)
   - After: 6 parallel strategies (TSLA, AAPL, NVDA, META, MSFT, AMD)
   - Each uses `TSLASimplifiedV7_2` template with symbol-specific JSON config

4. **Critical Kafka Fix** (Oct 20, commit `03d74e7c`)
   - SFS-1s now handles both flat and envelope-wrapped Kafka messages
   - Prevents silent data loss from schema variance

5. **IB Gateway Update**
   - Client ID: `7` → `31` (new gateway instance)

### Data Flow Architecture

```
Kafka Ticks → SFS-1s → Redis (1s ring) → Strategies (6 parallel)
                                      ↘ → SFS-5s → ClickHouse
```

### Scale & Performance

| Metric | Value |
|--------|-------|
| **Daily Data Volume** | ~34 MB (Redis 27.4 MB + ClickHouse 6.6 MB) |
| **Tick-to-Decision Latency** | 300-800 ms |
| **Redis per-symbol footprint** | ~1.5 MB |
| **TTL strategy** | Auto-expire old data to control costs |

### Status

| Component | Status | Notes |
|-----------|--------|-------|
| SFS-1s (Redis) | ✅ Complete | Kafka→Redis working, envelope parsing fixed |
| SFS-5s (ClickHouse) | ⚠️ Partial | Schema ready, batch write logic commented out |
| Strategies | ✅ Active | 6 strategies running, SFS-aware |
| Premarket V4/V4a | ✅ Active | Both versions running with VREF cache |

---

## Document Sections

### SFS_SYSTEM_UPGRADE_ANALYSIS.md Structure

1. **Executive Summary** - High-level overview
2. **Architecture Overview** - Data flow layers, timeline of changes
3. **ClickHouse Schema Additions** - 3 new tables, engines, TTLs
4. **SFS Service Layer** - L1 (1s), L2 (5s), facade implementation
5. **Strategy Layer Adaptations** - Multi-strategy enablement, config structure
6. **Data Integration Points** - Redis ← Strategies → ClickHouse
7. **System Readiness Status** - Completed vs. pending components
8. **Integration Checklist** - Tasks for Data Eng, Strategy Dev, Ops, QA
9. **Key Files & Locations** - Complete file inventory
10. **Performance Characteristics** - Data volume, latency, storage
11. **Migration Path** - Phase 1-4 roadmap
12. **Risk & Mitigation** - Data integrity, performance, operational risks
13. **Observability & Monitoring** - Metrics, logging, alert thresholds
14. **Conclusion** - Summary and next steps

### SFS_QUICK_REFERENCE.md Sections

1. **What Changed** - 5 key changes summarized
2. **Architecture at a Glance** - Simple diagram
3. **Key Data Formats** - Redis 1s bar, ClickHouse sfs_5s row examples
4. **Files to Know** - Organized by category (schemas, services, strategies)
5. **Quick Commands** - Useful bash/SQL snippets
6. **Status Dashboard** - Current state of each component
7. **Known Issues & TODOs** - High-priority blockers
8. **Performance Reference** - Data volume, latency, resource footprint
9. **Testing Checklist** - Verification tasks

---

## Critical Findings

### Completed (Oct 20-21)

- [x] SFS-1s service deployed and envelope parsing fixed
- [x] Redis ring buffer configured (TTL=3600s, per-symbol)
- [x] 6 strategies enabled and running
- [x] ClickHouse schema for SFS created
- [x] Premarket V4 & V4a with VREF cache integration
- [x] Multi-strategy coordination in place

### In Progress (Phase 2, Planned Nov 1-15)

- [ ] SFS-5s batch ClickHouse writes (currently placeholder)
- [ ] FeatureCalculator implementation (ATR, ORB, regime derivation)
- [ ] Hybrid feature backtest (1s + 1m)
- [ ] SFS metrics dashboard in Grafana

### Known Limitations

1. **SFS-5s write logic** (Line 136-140 `sfs_5s_service.py`)
   - Comment: `# TODO: Implement the actual batch insert logic`
   - Service logs but doesn't persist to ClickHouse

2. **Feature derivation** (`trading.sfs_features` fields)
   - `atr_1m_tumble`, `orb5_high/low`, `q80_ewma`, etc. are placeholders
   - Requires FeatureCalculator service

3. **Multi-strategy coordination**
   - Order queuing tested but not under heavy load
   - Need mock gateway stress test

---

## Use Cases for This Analysis

### For Infrastructure/DevOps
- Deployment checklist (Section 7.1)
- Service monitoring setup (Section 12)
- ClickHouse TTL and retention policy (Section 2.2)
- Risk mitigation for operational issues (Section 11.3)

### For Strategy Developers
- Strategy layer integration (Section 5)
- SFS facade usage (Section 3.3)
- Multi-symbol configuration (Section 4.2)
- Feature contract definitions (Section 8.3)

### For QA/Testing
- Integration checklist (Section 7.4)
- Known issues and TODOs (SFS_QUICK_REFERENCE)
- Performance benchmarks (Section 9)
- Load testing strategy (Section 11.2)

### For Product/Management
- Executive summary (Start here!)
- Architecture overview (Section 1)
- Roadmap (Section 10)
- Risk assessment (Section 11)

---

## Related Documentation

### In This Repository

- **CLAUDE.md** - Project rules, modification guidelines, strategy development guide
- **doc/sfs_implementation_plan.md** - Original v2.2 design document
- **config.yml** - System configuration (6 strategies, 19 symbols)
- **schema/clickhouse/*.sql** - All SFS schema files

### External References

- **Commit `03d74e7c`**: Fixed SFS data inserting (Kafka envelope support)
- **Commit `ce94d4d6`**: SFS implementation plan update + intraday strategy refactor
- **Commit `1672e12e`**: Added new strategy + SFS schema

---

## How to Use These Documents

### First Time?
1. Read **SFS_QUICK_REFERENCE.md** (5 min) for overview
2. Skim **SFS_SYSTEM_UPGRADE_ANALYSIS.md** sections 1-2 (10 min)
3. Jump to relevant section for your role (10-20 min)

### Deep Dive?
1. Read **SFS_SYSTEM_UPGRADE_ANALYSIS.md** sequentially
2. Cross-reference with actual code files (loc listed in Section 8)
3. Review related commits via `git show <commit-hash>`

### Looking for Something Specific?
- Performance metrics → Section 9 or SFS_QUICK_REFERENCE
- Kafka fix details → Section 3.1.2
- Strategy config example → Section 4.2
- Monitoring setup → Section 12

---

## Verification

Both documents created 2025-10-21:

```
SFS_SYSTEM_UPGRADE_ANALYSIS.md    831 lines    28 KB
SFS_QUICK_REFERENCE.md            275 lines     7 KB
Total                            1106 lines    35 KB
```

All paths are absolute (e.g., `./...`)
All code snippets tested against actual codebase as of commit `653f3faf`

---

## Next Steps

1. **Immediate (This week)**
   - Review risk mitigation section (11)
   - Verify SFS-1s service is running
   - Spot-check Redis ring buffer sizes

2. **Short-term (Next 2 weeks)**
   - Activate SFS-5s batch write logic (Phase 2)
   - Backtest with hybrid 1s+1m features
   - Set up Grafana SFS dashboard

3. **Medium-term (Next month)**
   - Deploy FeatureCalculator
   - Add ML feature enrichment
   - Implement multi-timeframe regime detection

---

**Document prepared by**: Analysis system (Claude)
**Last verified**: 2025-10-21 (Commit `653f3faf`)
**Version**: 1.0
**Status**: Complete and ready for distribution

For questions or updates, consult:
- Infrastructure team (SFS service issues)
- Strategy team (config/backtest questions)
- CLAUDE.md (modification guidelines)

