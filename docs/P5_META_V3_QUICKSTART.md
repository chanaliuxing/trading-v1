# P5 + Meta v3 Quick Start Guide

**Last Updated**: 2025-12-03
**Status**: ✅ Production Ready

---

## What Was Built

### P5: Integration & Production (Complete)
- ✅ **P5.1**: Intelligent target selection using UCB algorithm
- ✅ **P5.2**: Automatic patch evaluation (7-stage pipeline)
- ✅ **P5.3**: A/B testing framework for experiments
- ✅ **P5.4**: Prometheus metrics exporter (9 metrics)
- ✅ **P5.5**: Grafana dashboard (9 panels, 8 alerts)
- ✅ **P5.6**: End-to-end integration tests (9/9 passing)

### Meta v3: Cognition & Memory (Phase 1 Complete)
- ✅ **Meta v3.1**: Sleep loop generates LLM-based daily summaries
- ✅ **Meta v3.2**: ResearchAgent retrieves historical memory context
- ⏳ **Meta v3.3**: Vector-based semantic search (pending)
- ⏳ **Meta v3.4**: Self-healing event alignment (pending)
- ⏳ **Meta v3.5**: Auto-merge whitelist (pending)

---

## Quick Start (5 Minutes)

### 1. Verify Installation

```bash
PYTHONPATH=. venv/bin/python tools/verify_meta_v3_features.py
```

**Expected Output**:
```
🎉 All Meta v3 features verified successfully!
```

---

### 2. Generate First Daily Summary

```bash
# Run sleep loop manually (normally runs via cron)
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py
```

**What It Does**:
1. Collects events from `runs/ai_trader/*/ml_events.jsonl`, `brain_calls.jsonl`, `execution_trace.jsonl`
2. Calls LLM (local Ollama qwen2.5:7b by default) to generate 200-word summary
3. Writes to `runs/memory/daily_summaries.jsonl`
4. Promotes STS → MTS → LTS in Memory Fabric

**Check Output**:
```bash
cat runs/memory/daily_summaries.jsonl | jq .
```

**Example Summary**:
```json
{
  "date": "2025-12-03T10:30:00Z",
  "summary": "Today's research cycle focused on data quality remediation...",
  "key_observations": [
    "features_1m_with_labels had 48h lag, triggered sync_active_tables",
    "HPO on strategy_momentum_v2 deferred due to coverage < 80%",
    "Patch eval_20251203_001 auto-approved (benefit=0.75, risk=0.15)"
  ],
  "lessons_learned": [
    "Always check data readiness before HPO (saves 3h compute)",
    "Active sync cron job needs health monitoring"
  ],
  "recommendations": [
    "Add pre-flight check to ml_scheduler.py",
    "Set up alerting for lag > 24h"
  ],
  "statistics": {
    "total_events": 142,
    "event_types": {"completed": 120, "failed": 8, "aborted": 2}
  }
}
```

---

### 3. Test Memory Retrieval

```python
# In Python REPL or script
from libs.memory.memory_retrieval import get_retriever

retriever = get_retriever()

# Get last 7 days of summaries
summaries = retriever.get_recent_summaries(days=7)
print(f"Found {len(summaries)} summaries")

# Search by keyword
results = retriever.search_by_keyword("data quality", days=30)
print(f"Found {len(results)} mentions of 'data quality'")

# Get lessons learned
lessons = retriever.get_lessons_learned(days=14)
for lesson in lessons:
    print(lesson)

# Get LLM context for injection
context = retriever.format_context_for_llm(days=7)
print(context[:500])  # Preview
```

---

### 4. Start Prometheus Metrics Exporter

```bash
# Terminal 1: Start exporter
PYTHONPATH=. venv/bin/python tools/p3_p4_metrics_exporter.py --port 9101 --interval 30
```

**Output**:
```
INFO:metrics_exporter:starting_metrics_exporter port=9101 interval=30
INFO:metrics_exporter:metrics_server_started port=9101
INFO:metrics_exporter:memory_fabric_metrics_collected sts=1234 mts=567 lts=89
```

**Verify**:
```bash
# Terminal 2: Check metrics endpoint
curl http://localhost:9101/metrics | grep memory_fabric
```

**Expected**:
```
# HELP memory_fabric_sts_records_total Total STS records
# TYPE memory_fabric_sts_records_total gauge
memory_fabric_sts_records_total 1234.0
memory_fabric_mts_records_total 567.0
memory_fabric_lts_records_total 89.0
```

---

### 5. Run Research Cycle with Memory Context

```bash
PYTHONPATH=. venv/bin/python scripts/ai_trader/research/run_research_cycle.py
```

**What's Different**:
- **Before Meta v3.2**: ResearchAgent only sees current state
- **After Meta v3.2**: ResearchAgent sees:
  - Last 7 days of daily summaries
  - Accumulated lessons learned (14 days)
  - Outstanding recommendations (7 days)

**Check Logs**:
```bash
tail -f logs/research.log | grep memory_context
```

**Example Log**:
```
INFO:research_agent:memory_context_retrieved summaries=7 lessons=12 recommendations=3
INFO:research_agent:research_plan_generated experiments=2 memory_informed=true
```

---

## Integration with Existing Workflows

### Nightly Cron Job (Sleep Loop)

Add to `crontab -e`:
```cron
# Run sleep loop at 2 AM daily
0 2 * * * cd . && PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py >> logs/sleep_loop.log 2>&1
```

**Alternative: systemd timer** (Linux):
```ini
# /etc/systemd/system/sleep-loop.timer
[Unit]
Description=Nightly Sleep Loop for Memory Consolidation

[Timer]
OnCalendar=daily
OnCalendar=02:00
Persistent=true

[Install]
WantedBy=timers.target
```

---

### Prometheus Scraping

Add to `infra/prometheus/prometheus.yml`:
```yaml
scrape_configs:
  - job_name: 'p3_p4_memory_evolution'
    scrape_interval: 30s
    static_configs:
      - targets: ['localhost:9101']
        labels:
          service: 'research_pipeline'
          component: 'memory_evolution'
```

Reload:
```bash
cd infra && docker-compose restart prometheus
```

---

### Grafana Dashboard

**Option 1: Import via UI**
1. Open http://localhost:3000
2. Navigate to Dashboards → Import
3. Upload `infra/grafana/dashboards/p3_p4_memory_evolution.json`

**Option 2: Auto-provision**
1. Copy `infra/grafana/provisioning/*` to Grafana's provisioning directory
2. Restart Grafana:
   ```bash
   cd infra && docker-compose restart grafana
   ```

---

## Monitoring & Alerts

### Key Metrics to Watch

| Metric | Threshold | Action |
|--------|-----------|--------|
| `memory_fabric_sts_records_total` | > 10,000 | Investigate promotion delay |
| `patch_approval_queue_total{status="pending"}` | > 10 for 15min | Review pending patches |
| `patch_risk_score` | > 0.8 | Immediate manual review |
| `evolution_target_priority_score` | > 0.9 for 1h | Evolution may be stuck |
| `memory_fabric_promotions_total` | 0 for 6h | Sleep loop not running |

### Alert Channels

Configure in `infra/prometheus/alertmanager.yml`:
```yaml
route:
  receiver: 'slack-notifications'
  group_by: ['alertname', 'severity']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 12h

receivers:
  - name: 'slack-notifications'
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/YOUR/WEBHOOK/URL'
        channel: '#trading-alerts'
        title: 'P3-P4 Alert: {{ .GroupLabels.alertname }}'
        text: '{{ range .Alerts }}{{ .Annotations.description }}{{ end }}'
```

---

## Testing

### Unit Tests
```bash
PYTHONPATH=. venv/bin/pytest tests/unit/ -k memory -v
```

### Integration Tests (P5.6)
```bash
PYTHONPATH=. venv/bin/pytest tests/integration/test_p5_e2e_memory_evolution.py -v
```

**Expected**:
```
9 passed in 148.74s (0:02:28)
```

### Manual Smoke Test

```bash
# 1. Generate summary
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py

# 2. Verify summary exists
cat runs/memory/daily_summaries.jsonl | jq .

# 3. Test retrieval
python -c "from libs.memory.memory_retrieval import get_retriever; print(len(get_retriever().get_recent_summaries(7)))"

# 4. Run research cycle
PYTHONPATH=. venv/bin/python scripts/ai_trader/research/run_research_cycle.py

# 5. Check metrics
curl http://localhost:9101/metrics | head -n 50
```

---

## Troubleshooting

### Issue: LLM Summary Generation Fails

**Symptom**:
```
ERROR:sleep_loop:Failed to generate LLM summary: Connection refused
```

**Causes**:
1. Ollama not running
2. Model not downloaded
3. Network issue

**Fix**:
```bash
# Check Ollama status
ollama list

# Download model if missing
ollama pull qwen2.5:7b-instruct

# Start Ollama (if not running)
ollama serve

# Test manually
echo "Summarize: Today was productive" | ollama run qwen2.5:7b-instruct
```

**Fallback**: If LLM unavailable, sleep loop uses rule-based summary (counts events, success rate).

---

### Issue: Memory Retrieval Returns Empty

**Symptom**:
```python
summaries = retriever.get_recent_summaries(7)
len(summaries)  # 0
```

**Cause**: No daily summaries generated yet.

**Fix**:
```bash
# Run sleep loop once
PYTHONPATH=. venv/bin/python scripts/ai_trader/memory/sleep_loop.py

# Verify
cat runs/memory/daily_summaries.jsonl
```

---

### Issue: Metrics Exporter Can't Connect to ClickHouse

**Symptom**:
```
ERROR:metrics_exporter:memory_fabric_metrics_failed error='Connection refused'
```

**Fix**:
```bash
# Check ClickHouse status
docker ps | grep clickhouse

# Restart if needed
cd infra && docker-compose restart clickhouse

# Test connection
clickhouse-client --host localhost --query "SELECT 1"
```

---

### Issue: Grafana Dashboard Shows "No Data"

**Causes**:
1. Prometheus not scraping metrics exporter
2. Metrics exporter not running
3. Time range too narrow

**Fix**:
```bash
# 1. Check exporter is running
curl http://localhost:9101/metrics

# 2. Check Prometheus targets
open http://localhost:9090/targets

# 3. Verify scrape config
cat infra/prometheus/prometheus.yml | grep p3_p4

# 4. Adjust Grafana time range to "Last 6 hours"
```

---

## Performance Benchmarks

### Sleep Loop (Meta v3.1)
- **Event Collection**: ~100ms for 1000 events
- **LLM Summary Generation**: ~2-5s (Ollama qwen2.5:7b, local)
- **Memory Write**: ~50ms
- **Consolidation (STS→MTS→LTS)**: ~200ms
- **Total**: ~3-6s per daily run

### Memory Retrieval (Meta v3.2)
- **get_recent_summaries(7)**: < 10ms
- **search_by_keyword**: < 50ms for 30 days
- **format_context_for_llm(7)**: < 20ms
- **Memory footprint**: ~5MB for 30 days of summaries

### Metrics Exporter (P5.4)
- **Collection cycle**: ~300-500ms (9 metrics from ClickHouse)
- **HTTP response time**: < 50ms
- **Memory usage**: ~50MB
- **CPU usage**: < 5% (30s interval)

---

## Next Steps

### Immediate (This Week)
1. ✅ Verify all features work
2. ⏳ Deploy metrics exporter to production
3. ⏳ Set up Grafana dashboard
4. ⏳ Schedule nightly sleep loop cron
5. ⏳ Backfill summaries for last 7 days (optional)

### Short-Term (Next 2 Weeks)
1. 🔲 Implement Meta v3.3 (vector memory with Chroma)
2. 🔲 Implement Meta v3.4 (self-healing event alignment)
3. 🔲 Implement Meta v3.5 (auto-merge whitelist)
4. 🔲 Add Slack alerting for critical metrics
5. 🔲 Train domain-specific embedding model

### Mid-Term (Next Month)
1. 🔲 Measure evolution cycle time before/after auto-approval
2. 🔲 Add Critic "post-mortem" analysis (plan vs actual)
3. 🔲 Implement anomaly detector for predictive immunity
4. 🔲 A/B test different memory retrieval strategies

---

## FAQ

**Q: Do I need to run sleep_loop.py manually?**
A: No, it should run via cron at 2 AM daily. Manual runs are for testing.

**Q: Can I use a different LLM (not Ollama)?**
A: Yes, set `LLM_PROVIDERS_RESEARCH` env var:
```bash
export LLM_PROVIDERS_RESEARCH="openai:gpt-4o-mini,anthropic:claude-3-haiku"
```

**Q: How much disk space do summaries use?**
A: ~2-3KB per day → ~1MB per year. Very lightweight.

**Q: Can I delete old summaries?**
A: Yes, but keep at least 14 days for lessons_learned. Older summaries are in MTS/LTS anyway.

**Q: Does memory retrieval slow down ResearchAgent?**
A: No, < 20ms overhead. Retrieval happens before LLM call (which takes 2-10s).

**Q: What if ClickHouse is unavailable?**
A: Metrics exporter logs warning but continues. Grafana shows stale data until ClickHouse returns.

---

## References

- [P5 + Meta v3 Completion Summary](./P5_META_V3_COMPLETION_SUMMARY.md)
- [P3-P4 Implementation Complete](./P3_P4_IMPLEMENTATION_COMPLETE.md)
- [Meta-Agent Gap Analysis](../ai/TODO_META_AGENT_GAP_COVERAGE.md)
- [Research Pipeline v3 Blueprint](../ai_meta/ai_meta.md)

---

**Status**: ✅ Ready for Production
**Maintainer**: Trading Team
**Last Verified**: 2025-12-03
