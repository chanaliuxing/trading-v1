# ops/secrets.py
from __future__ import annotations
import os, json
from typing import Optional


class SecretError(Exception):
    pass


def _load_local_vault(path: str) -> dict:
    """Load secrets from local vault file."""
    if not path or not os.path.exists(path):
        return {}
    try:
        return json.load(open(path,"r",encoding="utf-8"))
    except Exception:
        return {}


def get_secret(name: str, vault_path: Optional[str] = None, required: bool = True) -> str:
    """
    Retrieve secret with whitelist-based access control.

    Priority: environment variable > vault.json

    Args:
        name: Secret name (must be in whitelist)
        vault_path: Path to vault.json (default: /etc/alpha/vault.json)
        required: Whether to raise error if secret not found

    Returns:
        Secret value

    Raises:
        SecretError: If secret not in whitelist or not found when required
    """
    ALLOW = {
        "LLM_API_KEY", "CLICKHOUSE_PASSWORD",
        "BROKER_KEY", "BROKER_SECRET", "WEBHOOK_TOKEN"
    }

    if name not in ALLOW:
        raise SecretError(f"Secret {name} not allowed (whitelist).")

    # Try environment variable first
    v = os.getenv(name)
    if v:
        return v

    # Fall back to vault file
    vault = _load_local_vault(vault_path or os.getenv("ALPHA_VAULT","/etc/alpha/vault.json"))
    v = vault.get(name)

    if not v and required:
        raise SecretError(f"Missing secret: {name}")

    return v or ""
