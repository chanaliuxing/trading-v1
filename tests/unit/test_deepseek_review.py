
import asyncio
import json
import sys
sys.path.append('.') # Add current directory to path

from scripts.ai_trader.brain.provider_fallback import LLMCallResult

# Copied from scripts/ai_trader/brain/brain.py with careful escaping
ML_RESULT_REVIEW_PROMPT = (
    "You are an ML result reviewer for a trading system. Return ONLY strict JSON.\n"
    "Schema: {\"summary\": {\"oos_sharpe\": <number>|null, \"oos_mdd\": <number>|null, \"trades\": <int>|null, \"coverage\": <number>|null, \"profit_factor\": <number>|null, \"expectancy\": <number>|null, \"return\": <number>|null, \"mem_peak_mb\": <number>|null, \"grid_points\": <int>|null}, \"diagnosis\": [<string>], \"history_delta\": {\"n_bars\": {\"prev\": <number>|null, \"cur\": <number>|null}|null, \"ic\": {\"prev\": <number>|null, \"cur\": <number>|null}|null, \"auc\": {\"prev\": <number>|null, \"cur\": <number>|null}|null}|null, \"effect_of_last_action\": <string|null>, \"next_actions\": [{\"action\": \"backfill|extend_window|reduce_window|data_quality_check|propose_features|queue_job|fix_config|pause_ml|resume_ml|wait|do_nothing|escalate\", \"params\": <object>, \"confidence\": <number>}], \"action_rationale\": <string>, \"stop_reason\": <string|null>, \"prev_attempts_summary\": <string>, \"numbers_used\": <array|null>, \"tool_calls\": <array|null>}\n"
    "Guidelines:\n"
    "- Prefer OUT-OF-SAMPLE (OOS) metrics over in-sample.\n"
    "- PROFIT-FIRST POLICY: NEVER suggest relaxing risk/quality thresholds (e.g., lowering conf_floor, loosening gates) just to 'make it pass'.\n"
    "  Only proceed to apply models/params when profitability gates are met (Sharpe>=1.0, PF>1.0, Expectancy>0, Coverage>=0.9).\n"
)

async def main():
    model_name = "my-deepseek-uncensored:latest"
    print(f"--- Testing model: {model_name} with the complex 'review_ml' prompt ---")

    mock_context = {
        "job": { "job_type": "oof", "metrics": { "xgb_auc": 0.51, "fusion_auc": 0.505, "n_bars": 5000 } },
        "stage0": { "allowed_actions": ["propose_features", "extend_window", "wait"] }
    }
    
    full_prompt = ML_RESULT_REVIEW_PROMPT + "\nINPUT:\n" + json.dumps(mock_context)

    try:
        from scripts.ai_trader.brain.provider_fallback import OllamaProvider
        
        provider = OllamaProvider(model=model_name, timeout=300)

        print("--- Calling provider.call_llm() directly to see raw output ---")
        res: LLMCallResult = await provider.call_llm(full_prompt, "test_raw_output")
        
        print("\n--- Full LLMCallResult ---")
        print(res)
        print("\n--- Raw Response ---")
        print(res.raw)
        print("--- End Raw Response ---")

    except Exception as e:
        import traceback
        print(f"An exception occurred: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
