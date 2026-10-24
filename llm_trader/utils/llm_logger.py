"""LLM Request/Response Logger

Records all LLM API calls (requests and responses) to JSON log files for analysis.

Directory structure:
    logs/llm/
    ├── YYYY-MM-DD/
    │   ├── requests/
    │   │   ├── {timestamp}_{provider}_{model}_{symbol}_request.json
    │   │   └── ...
    │   └── responses/
    │       ├── {timestamp}_{provider}_{model}_{symbol}_response.json
    │       └── ...
    └── llm_interactions.jsonl  # Combined log for easy analysis

Each log entry contains:
- timestamp: ISO format timestamp
- provider: openai, gemini, deepseek, etc.
- model: specific model name (gpt-4o, gemini-2.0-flash-exp, etc.)
- symbol: trading symbol being analyzed
- request: full request payload
- response: full response
- latency_ms: time taken for API call
- success: boolean
- error: error message if failed
"""

import json
import time
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
from functools import wraps
import traceback


class LLMLogger:
    """Logger for LLM API interactions"""

    def __init__(self, log_dir: str = "logs/llm"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Combined JSONL log for easy analysis
        self.combined_log = self.log_dir / "llm_interactions.jsonl"

    def _get_daily_dir(self) -> Path:
        """Get today's log directory"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        daily_dir = self.log_dir / date_str
        daily_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        (daily_dir / "requests").mkdir(exist_ok=True)
        (daily_dir / "responses").mkdir(exist_ok=True)

        return daily_dir

    def _sanitize_filename(self, text: str) -> str:
        """Sanitize text for use in filename"""
        # Remove invalid filename characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            text = text.replace(char, "_")
        return text[:100]  # Limit length

    def log_request(
        self,
        provider: str,
        model: str,
        symbol: str,
        request_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Log LLM request

        Args:
            provider: Provider name (openai, gemini, etc.)
            model: Model name
            symbol: Trading symbol
            request_data: Request payload
            metadata: Additional metadata

        Returns:
            Request ID for correlation with response
        """
        timestamp = datetime.now()
        request_id = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{provider}_{model}_{symbol}"

        log_entry = {
            "request_id": request_id,
            "timestamp": timestamp.isoformat(),
            "type": "request",
            "provider": provider,
            "model": model,
            "symbol": symbol,
            "request_data": request_data,
            "metadata": metadata or {},
        }

        # Write to daily request file
        try:
            daily_dir = self._get_daily_dir()
            filename = self._sanitize_filename(f"{request_id}_request.json")
            request_file = daily_dir / "requests" / filename

            with open(request_file, "w") as f:
                json.dump(log_entry, f, indent=2, default=str)

            # Append to combined log
            with open(self.combined_log, "a") as f:
                f.write(json.dumps(log_entry, default=str) + "\n")

        except Exception as e:
            print(f"[LLMLogger] Failed to log request: {e}")

        return request_id

    def log_response(
        self,
        request_id: str,
        provider: str,
        model: str,
        symbol: str,
        response_data: Any,
        latency_ms: float,
        success: bool = True,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Log LLM response

        Args:
            request_id: ID from log_request for correlation
            provider: Provider name
            model: Model name
            symbol: Trading symbol
            response_data: Response payload
            latency_ms: API call latency in milliseconds
            success: Whether the call succeeded
            error: Error message if failed
            metadata: Additional metadata
        """
        timestamp = datetime.now()

        log_entry = {
            "request_id": request_id,
            "timestamp": timestamp.isoformat(),
            "type": "response",
            "provider": provider,
            "model": model,
            "symbol": symbol,
            "response_data": response_data,
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
            "metadata": metadata or {},
        }

        # Write to daily response file
        try:
            daily_dir = self._get_daily_dir()
            filename = self._sanitize_filename(f"{request_id}_response.json")
            response_file = daily_dir / "responses" / filename

            with open(response_file, "w") as f:
                json.dump(log_entry, f, indent=2, default=str)

            # Append to combined log
            with open(self.combined_log, "a") as f:
                f.write(json.dumps(log_entry, default=str) + "\n")

        except Exception as e:
            print(f"[LLMLogger] Failed to log response: {e}")

    def log_interaction(
        self,
        provider: str,
        model: str,
        symbol: str,
        request_data: Dict[str, Any],
        response_data: Any,
        latency_ms: float,
        success: bool = True,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Log complete request-response interaction

        Convenience method to log both request and response together.

        Returns:
            Request ID
        """
        request_id = self.log_request(provider, model, symbol, request_data, metadata)
        self.log_response(
            request_id, provider, model, symbol, response_data, latency_ms, success, error, metadata
        )
        return request_id


# Global logger instance
_global_logger: Optional[LLMLogger] = None


def get_logger(log_dir: str = "logs/llm") -> LLMLogger:
    """Get or create global LLM logger instance"""
    global _global_logger
    if _global_logger is None:
        _global_logger = LLMLogger(log_dir)
    return _global_logger


def log_llm_call(provider_name: str, model_name: str):
    """Decorator to automatically log LLM API calls

    Usage:
        @log_llm_call("openai", "gpt-4o")
        async def analyze_market(self, symbol, market_data, portfolio_state):
            ...

    The decorator will:
    - Log the request before calling the API
    - Measure latency
    - Log the response (success or failure)
    - Re-raise exceptions after logging
    """

    def decorator(func):
        @wraps(func)
        async def async_wrapper(self, symbol: str, market_data: Dict[str, Any], portfolio_state: Dict[str, Any], *args, **kwargs):
            logger = get_logger()

            # Prepare request data
            request_data = {
                "symbol": symbol,
                "market_data": market_data,
                "portfolio_state": portfolio_state,
                "args": args,
                "kwargs": kwargs,
            }

            # Log request
            request_id = logger.log_request(
                provider=provider_name,
                model=model_name,
                symbol=symbol,
                request_data=request_data,
                metadata={"function": func.__name__, "class": self.__class__.__name__},
            )

            # Execute API call and measure latency
            start_time = time.perf_counter()
            success = True
            error_msg = None
            response_data = None

            try:
                # Call the original function
                result = await func(self, symbol, market_data, portfolio_state, *args, **kwargs)
                response_data = {
                    "decision": getattr(result, "decision", None),
                    "quantity": getattr(result, "quantity", None),
                    "confidence": getattr(result, "confidence", None),
                    "reasoning": getattr(result, "reasoning", None),
                    "raw_response": getattr(result, "raw_response", None),
                }
                return result

            except Exception as e:
                success = False
                error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
                response_data = {"error": str(e)}
                raise

            finally:
                # Calculate latency
                latency_ms = (time.perf_counter() - start_time) * 1000

                # Log response
                logger.log_response(
                    request_id=request_id,
                    provider=provider_name,
                    model=model_name,
                    symbol=symbol,
                    response_data=response_data,
                    latency_ms=latency_ms,
                    success=success,
                    error=error_msg,
                    metadata={"function": func.__name__},
                )

        @wraps(func)
        def sync_wrapper(self, symbol: str, market_data: Dict[str, Any], portfolio_state: Dict[str, Any], *args, **kwargs):
            # For sync functions, similar logic
            logger = get_logger()

            request_data = {
                "symbol": symbol,
                "market_data": market_data,
                "portfolio_state": portfolio_state,
                "args": args,
                "kwargs": kwargs,
            }

            request_id = logger.log_request(
                provider=provider_name,
                model=model_name,
                symbol=symbol,
                request_data=request_data,
                metadata={"function": func.__name__, "class": self.__class__.__name__},
            )

            start_time = time.perf_counter()
            success = True
            error_msg = None
            response_data = None

            try:
                result = func(self, symbol, market_data, portfolio_state, *args, **kwargs)
                response_data = {
                    "decision": getattr(result, "decision", None),
                    "quantity": getattr(result, "quantity", None),
                    "confidence": getattr(result, "confidence", None),
                    "reasoning": getattr(result, "reasoning", None),
                    "raw_response": getattr(result, "raw_response", None),
                }
                return result

            except Exception as e:
                success = False
                error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
                response_data = {"error": str(e)}
                raise

            finally:
                latency_ms = (time.perf_counter() - start_time) * 1000

                logger.log_response(
                    request_id=request_id,
                    provider=provider_name,
                    model=model_name,
                    symbol=symbol,
                    response_data=response_data,
                    latency_ms=latency_ms,
                    success=success,
                    error=error_msg,
                    metadata={"function": func.__name__},
                )

        # Return appropriate wrapper based on whether function is async
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
