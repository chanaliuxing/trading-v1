import os
import sys
from pathlib import Path
import json

# Add project root to allow importing llm.llm_client
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm.llm_client import LLMClient

def run_test(model_name: str):
    print(f"--- Testing model: {model_name} ---")
    try:
        client = LLMClient()
        # A simple prompt that asks for JSON output
        system_prompt = "You are a helpful assistant that only responds with JSON."
        user_prompt = "Return a JSON object with the key 'msg' and the value 'hello'."
        
        result = client.generate(system_prompt, user_prompt, model=model_name)
        
        print(f"Raw result: {result}")
        
        # Verification
        if "feature_recipe" in result and "training_plan" in result:
            print("--- ❌ FAILED: Offline fallback was returned. ---")
            return False
        
        if isinstance(result, dict) and result.get("msg") == "hello":
            print("--- ✅ PASSED: Parsed JSON successfully. ---")
            return True
        
        # Handle cases where LLM might wrap output in a 'text' key
        if isinstance(result, dict) and "text" in result:
             try:
                nested_json = json.loads(result["text"])
                if nested_json.get("msg") == "hello":
                    print("--- ✅ PASSED: Parsed JSON successfully from nested text. ---")
                    return True
             except (json.JSONDecodeError, TypeError):
                pass
        
        print(f"--- ❌ FAILED: Output was not the expected JSON. Got: {result} ---")
        return False

    except Exception as e:
        print(f"--- ❌ FAILED: An exception occurred: {e} ---")
        import traceback
        traceback.print_exc()
        return False

def main():
    # Set environment variables for the test
    os.environ["LLM_BASE_URL"] = "http://localhost:11434"
    os.environ["LLM_LOCAL_MODELS"] = "qwen2.5vl:latest,my-deepseek-uncensored:latest,fast-local-1:latest,fast-local-2:latest"
    
    print("Starting LLM local model sanity check...")
    
    models_to_test = [
        "qwen2.5vl:latest",
        "my-deepseek-uncensored:latest"
    ]
    
    all_passed = True
    for model in models_to_test:
        if not run_test(model):
            all_passed = False
            
    print("\n--- LLM Sanity Check Summary ---")
    if all_passed:
        print("✅ All tests passed.")
    else:
        print("❌ Some tests failed.")
        # Exit with non-zero to indicate failure in CI/automation
        sys.exit(1)

if __name__ == "__main__":
    main()
