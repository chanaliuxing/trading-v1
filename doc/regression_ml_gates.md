# ML Gate Regression (OOF + Brain Loop)

Quick commands to validate end-to-end gates and metric emission.

1) Run ML Brain Loop (no trading) with gates + auto-heal:
```bash
ENV_MODE=paper venv/bin/python tools/ml_brain_loop.py --symbols TSLA AAPL --start-job-type oof --sleep-sec 5 --max-cycles 1
```
Expect: data/label gates pass (or heal then pass) before loop starts.

2) Run nightly OOF flow with enforced EV/Beta metrics:
```bash
venv/bin/python tools/nightly_oof_flow.py --symbols TSLA,AAPL --apply --ev-thr 0.0 --beta-corr-thr 0.8
```
Expect: `runs/ai_trader/<date>/oof_eval/oof_eval.json` contains `ev`/`beta_corr`; ml_events include these fields even on failure.

3) Optional smoke backtest (gated):
```bash
venv/bin/python run_backtest_simple.py --symbol TSLA --days 10
```
Expect: gate passes or auto-heal kicks in; aborts with clear message if still blocked.

4) Vector store fallback (no Chroma/FAISS installed):
```bash
VSTORE_BACKEND=chroma pip uninstall -y chromadb faiss-cpu  # or run in a clean venv
venv/bin/python -c "from libs.memory.vector_store import VectorStore; s=VectorStore(backend='chroma'); s.add('t1','hello'); print(s.search('hello'))"
```
Expect: initialization falls back to memory-only backend without raising; search returns empty or memory results, and main flows continue.
