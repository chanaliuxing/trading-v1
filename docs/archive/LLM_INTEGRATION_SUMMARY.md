# LLM Integration Summary

## Overview

This document summarizes the enhancements made to support multi-provider LLM integration with robust response parsing for the trading research system.

## Changes Made

### 1. Enhanced `llm_client.py`

**File**: `llm_client.py`

**Changes**:
- Complete rewrite to support multiple LLM providers: OpenAI, Anthropic, OpenRouter
- Robust response parsing that handles different output formats:
  - `<thinking>` tags (used by Claude and some models)
  - Markdown code blocks (```json ... ```)
  - Plain JSON
  - Mixed text with embedded JSON
- Environment-based configuration using `.env` file
- Graceful fallback parsing strategies
- Test function: `test_llm_connection(provider, model)`

**Key Functions**:
```python
def ask_for_grid(context, model=None, provider=None) -> Tuple[Dict[str, List[Any]], str]:
    """
    Ask LLM to propose a refined parameter grid.

    Args:
        context: Dictionary with objective, base_grid, top_is, symbols, notes
        model: Model name (e.g., "gpt-4o-mini", "claude-3-5-sonnet-20241022")
        provider: LLM provider ("openai", "anthropic", "openrouter")

    Returns:
        (grid_dict, rationale_text)
    """
```

**Environment Variables** (from `.env`):
- `OPENAI_API_KEY` - OpenAI API key
- `ANTHROPIC_API_KEY` - Anthropic (Claude) API key
- `OPENROUTER_API_KEY` - OpenRouter API key
- `LLM_PROVIDER` - Default provider: "openai" | "anthropic" | "openrouter"
- `LLM_MODEL` - Default model name
- `LLM_BASE_URL` - Optional base URL for custom endpoints

### 2. Updated `alpha_loop.py`

**File**: `alpha_loop.py`

**Changes**:
- Updated `_llm_propose_grid()` function signature to accept `provider` parameter
- Updated all three LLM call sites to pass provider from config:
  1. Line 755-762: Coop mode initial LLM call
  2. Line 823-830: Coop_optuna mode LLM call
  3. Line 922-928: Hybrid/LLM mode LLM call

**Example Usage**:
```python
llm_cfg = cfg.get("llm", {})
grid_llm, rationale = _llm_propose_grid(
    context,
    model=llm_cfg.get("model"),
    provider=llm_cfg.get("provider")
)
```

### 3. Updated `.env.example`

**File**: `.env.example`

**Changes**:
- Added comprehensive documentation for LLM provider configuration
- Added `OPENROUTER_API_KEY` for OpenRouter support
- Added `LLM_BASE_URL` for custom endpoints
- Documented all supported providers and model names
- Added usage examples and recommendations

**New/Updated Variables**:
```bash
# LLM Provider Configuration
LLM_PROVIDER=openai              # openai | anthropic | openrouter | none
LLM_MODEL=gpt-5                  # Provider-specific model name
LLM_API_STYLE=responses          # responses | chat (auto for gpt-5)
LLM_REASONING_EFFORT=low         # low | medium | high (Responses API)
LLM_VERBOSITY=low                # low | medium | high (Responses API)
LLM_BASE_URL=                    # Optional custom endpoint (defaults to https://api.openai.com)
OPENAI_API_KEY=                  # OpenAI API key
ANTHROPIC_API_KEY=               # Anthropic API key
OPENROUTER_API_KEY=              # OpenRouter API key
```

### 4. Updated `alpha_config_example.json`

**File**: `config/alpha_config_example.json`

**Changes**:
- Added `fold_generator` section with `embargo_days` parameter
- Added `overlay_config` section for portfolio risk management
- Enhanced `llm` section with `provider` field
- Added `coop` section with `cap_per_param` parameter
- Added `guard` section with grid guardrail parameters
- Enhanced `optuna` section with `neighbor_scale` parameter
- Added `formula` field to `objective` section

**New Configuration Sections**:
```json
{
  "hpo": {
    "fold_generator": {
      "embargo_days": 0
    },
    "overlay_config": {
      "max_position_pct": 0.25,
      "max_gross_exposure": 1.0,
      "max_net_exposure": 0.8,
      "max_drawdown_halt": 0.20,
      "volatility_lookback": 20,
      "volatility_target": 0.15,
      "vol_scaling_enabled": false
    }
  },
  "llm": {
    "provider": "openai",
    "model": "gpt-5"
  },
  "coop": {
    "cap_per_param": 5
  },
  "guard": {
    "max_combinations": 10000,
    "max_values_per_param": 10,
    "param_range_multiplier": 5.0
  },
  "optuna": {
    "neighbor_scale": 0.20
  }
}
```

## Provider Support

### OpenAI
- **Models**: gpt-4o-mini, gpt-4o, gpt-4-turbo
- **Features**: json_object response format support
- **Configuration**: Set `OPENAI_API_KEY` in `.env`

### Anthropic (Claude)
- **Models**: claude-3-5-sonnet-20241022, claude-3-opus-20240229
- **Features**: Handles `<thinking>` tags automatically
- **Configuration**: Set `ANTHROPIC_API_KEY` in `.env`

### OpenRouter
- **Models**: anthropic/claude-3.5-sonnet, openai/gpt-4o, and many others
- **Features**: Unified API for multiple providers
- **Configuration**: Set `OPENROUTER_API_KEY` in `.env`

### DeepSeek (Official)
- **Models**: deepseek-chat, deepseek-coder
- **API Endpoint**: https://api.deepseek.com
- **Features**: OpenAI-compatible API format
- **Configuration**: Set `DEEPSEEK_API_KEY` in `.env`

### DeepSeek via NVIDIA
- **Models**: deepseek/deepseek-r1
- **API Endpoint**: https://integrate.api.nvidia.com/v1
- **Features**: DeepSeek models via NVIDIA's API gateway
- **Configuration**: Set `DEEPSEEK_NVIDIA_API_KEY` in `.env`

### Google Gemini
- **Models**: gemini-2.0-flash-exp, gemini-1.5-pro, gemini-1.5-flash
- **API Endpoint**: Google AI Studio API
- **Features**: Native JSON mode support, fast inference
- **Configuration**: Set `GEMINI_API_KEY` in `.env`
- **Note**: Requires `pip install google-generativeai`

## Response Parsing

The enhanced `_extract_json_from_response()` function handles:

1. **Thinking Tags**: Removes `<thinking>...</thinking>` blocks
2. **Markdown**: Strips markdown code fences (```json ... ```)
3. **Embedded JSON**: Extracts JSON from mixed text using regex
4. **Fallback**: Attempts partial extraction of grid and notes fields
5. **Error Handling**: Provides detailed error messages with response preview

## Usage Examples

### Command Line Testing

Test LLM connection with different providers:

```bash
# Test OpenAI (default)
python3 llm_client.py

# Test Anthropic
python3 llm_client.py anthropic

# Test specific model
python3 llm_client.py openai gpt-4o
```

### Alpha Loop Usage

Run alpha loop with LLM provider:

```bash
# OpenAI (GPT-4o-mini)
python alpha_loop.py --config alpha_config.json --gens 3 --provider coop

# Anthropic (Claude)
# Set in config: {"llm": {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"}}
python alpha_loop.py --config alpha_config.json --gens 3 --provider llm

# Cooperative mode (LLM + ML)
python alpha_loop.py --config alpha_config.json --gens 3 --provider coop_optuna --k-top 12
```

### Configuration File

Example `alpha_config.json`:

```json
{
  "hpo": {
    "symbols": ["TSLA"],
    "grid": {...},
    "overlay_config": {
      "max_position_pct": 0.25,
      "max_drawdown_halt": 0.20
    }
  },
  "llm": {
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-20241022"
  },
  "optuna": {
    "trials": 50,
    "target": "is"
  }
}
```

## Installation

To use LLM features, install the required packages:

```bash
# OpenAI
pip install openai

# Anthropic
pip install anthropic

# Both (recommended)
pip install openai anthropic
```

## Error Handling

The system gracefully handles:
- Missing API keys (raises `RuntimeError` with clear message)
- Missing packages (returns fallback message)
- Invalid responses (attempts multiple parsing strategies)
- Provider-specific errors (fallback without response_format)

## Testing

The `test_llm_connection()` function provides a simple way to verify:
- API key validity
- Network connectivity
- Response parsing
- Provider-specific features

Example output:
```
[LLM] Connection test successful!
[LLM] Grid: {'test_param': [1, 2, 3]}
[LLM] Notes: Centered around top IS sharpe=1.8 at test_param=2
```

## Best Practices

1. **API Keys**: Store in `.env` file, never commit to git
2. **Provider Selection**:
   - OpenAI: Fast, cost-effective (gpt-4o-mini)
   - Anthropic: Better reasoning, handles thinking tags
   - OpenRouter: Access to multiple models with single API key
3. **Model Selection**:
   - Development: gpt-4o-mini (fast, cheap)
   - Production: claude-3-5-sonnet-20241022 (better quality)
4. **Error Recovery**: Use `provider="hybrid"` to fallback to ML if LLM fails
5. **Cost Control**: Set reasonable `trials` limits in optuna config

## Integration with Other Features

This LLM integration works seamlessly with:
- **Leakage Guard**: `embargo_days` in fold_generator
- **Portfolio Overlay**: `overlay_config` in hpo section
- **Grid Guardrails**: `guard` section with safety limits
- **Optuna Optimization**: Two-stage coop_optuna mode
- **ML Surrogate**: Hybrid mode fallback

## Troubleshooting

### "API key not found"
- Check `.env` file exists and has correct variable names
- Verify API key is set: `echo $OPENAI_API_KEY`
- Ensure `python-dotenv` is installed: `pip install python-dotenv`

### "Failed to parse JSON from LLM response"
- LLM may have returned non-JSON text
- Check raw response in error message (first 500 chars shown)
- Try different provider or model
- Use `provider="hybrid"` to fallback to ML

### "openai package not installed"
- Install required package: `pip install openai`
- Or use different provider: `LLM_PROVIDER=anthropic`

### Response includes thinking tags
- This is automatically handled by `_extract_json_from_response()`
- No action needed

## Future Enhancements

Potential improvements:
1. Streaming support for long-running LLM calls
2. Caching of LLM responses to reduce API costs
3. Multi-shot prompting for better grid proposals
4. Ensemble of multiple LLM providers
5. Fine-tuned models on historical trading data
6. LLM-based strategy synthesis (not just grid refinement)

## Summary of Files Modified

1. ✅ `llm_client.py` - Complete rewrite with multi-provider support
2. ✅ `alpha_loop.py` - Updated all LLM calls to pass provider
3. ✅ `.env.example` - Enhanced LLM documentation and added OpenRouter
4. ✅ `config/alpha_config_example.json` - Added all new configuration sections

## Verification Steps

To verify the integration:

1. Check configuration files exist and are valid:
   ```bash
   ls -la .env.example config/alpha_config_example.json
   ```

2. Test LLM client (will fail gracefully if no API key):
   ```bash
   python3 llm_client.py
   ```

3. Verify alpha_loop imports correctly:
   ```bash
   python3 -c "from alpha_loop import _llm_propose_grid; print('OK')"
   ```

4. Check all new config sections parse correctly:
   ```bash
   python3 -c "import json; print(json.load(open('config/alpha_config_example.json'))['llm'])"
   ```

All verification steps should complete without errors.
