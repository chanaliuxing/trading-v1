"""OCC (Options Clearing Corporation) symbol format utilities.

OCC Symbol Format: SSSSSS YYMMDD C PPPPPPPP
- SSSSSS: Underlying symbol (6 chars, right-padded with spaces)
- YYMMDD: Expiration date
- C: Option type (C=Call, P=Put)
- PPPPPPPP: Strike price (8 digits, implied 3 decimal places)

Example: SPY    250120C00450000 represents SPY Call expiring 2025-01-20 at strike $450.00
"""

from __future__ import annotations
import re
from datetime import datetime
from typing import Dict, Any, Optional


def build_occ_symbol(
    underlying: str,
    expiry_date: str,  # YYYYMMDD or YYYY-MM-DD
    right: str,  # 'C' or 'P'
    strike: float,
) -> str:
    """Build OCC-compliant option symbol.

    Args:
        underlying: Underlying symbol (e.g., 'SPY', 'QQQ')
        expiry_date: Expiration date in YYYYMMDD or YYYY-MM-DD format
        right: Option type - 'C' for Call, 'P' for Put
        strike: Strike price (e.g., 450.0)

    Returns:
        OCC symbol string (e.g., 'SPY   250120C00450000')

    Raises:
        ValueError: If inputs are invalid

    Examples:
        >>> build_occ_symbol('SPY', '20250120', 'C', 450.0)
        'SPY   250120C00450000'
        >>> build_occ_symbol('QQQ', '2025-01-20', 'P', 380.5)
        'QQQ   250120P00380500'
    """
    # Validate and normalize inputs
    if not underlying or len(underlying) > 6:
        raise ValueError(f"Underlying must be 1-6 characters: {underlying}")

    # Normalize expiry date to YYMMDD
    expiry_clean = expiry_date.replace('-', '')
    if len(expiry_clean) == 8:  # YYYYMMDD
        try:
            dt = datetime.strptime(expiry_clean, '%Y%m%d')
            expiry_yymmdd = dt.strftime('%y%m%d')
        except ValueError:
            raise ValueError(f"Invalid expiry date format: {expiry_date}")
    elif len(expiry_clean) == 6:  # YYMMDD
        try:
            datetime.strptime(expiry_clean, '%y%m%d')
            expiry_yymmdd = expiry_clean
        except ValueError:
            raise ValueError(f"Invalid expiry date format: {expiry_date}")
    else:
        raise ValueError(f"Expiry date must be YYYYMMDD or YYMMDD: {expiry_date}")

    # Validate right
    right_upper = right.upper()
    if right_upper not in ('C', 'P'):
        raise ValueError(f"Right must be 'C' or 'P': {right}")

    # Validate strike
    if strike <= 0:
        raise ValueError(f"Strike must be positive: {strike}")

    # Format strike as 8-digit integer (3 decimal places implied)
    # E.g., 450.0 -> 00450000, 380.5 -> 00380500
    strike_int = int(round(strike * 1000))
    if strike_int > 99999999:
        raise ValueError(f"Strike too large: {strike}")
    strike_str = f"{strike_int:08d}"

    # Build OCC symbol: underlying (6 chars padded) + YYMMDD + C/P + strike (8 digits)
    underlying_padded = underlying.upper().ljust(6)
    return f"{underlying_padded}{expiry_yymmdd}{right_upper}{strike_str}"


def parse_occ_symbol(occ_symbol: str) -> Dict[str, Any]:
    """Parse OCC symbol into components.

    Args:
        occ_symbol: OCC-formatted option symbol

    Returns:
        Dict with keys: underlying, expiry_date (YYYY-MM-DD), right, strike

    Raises:
        ValueError: If symbol format is invalid

    Examples:
        >>> parse_occ_symbol('SPY   250120C00450000')
        {'underlying': 'SPY', 'expiry_date': '2025-01-20', 'right': 'C', 'strike': 450.0}
        >>> parse_occ_symbol('QQQ   250120P00380500')
        {'underlying': 'QQQ', 'expiry_date': '2025-01-20', 'right': 'P', 'strike': 380.5}
    """
    # OCC format: 6 chars + 6 digits + 1 char + 8 digits = 21 chars
    if len(occ_symbol) != 21:
        raise ValueError(f"OCC symbol must be 21 characters: {occ_symbol}")

    # Extract components
    underlying = occ_symbol[:6].strip()
    expiry_yymmdd = occ_symbol[6:12]
    right = occ_symbol[12]
    strike_str = occ_symbol[13:21]

    # Validate components
    if not re.match(r'^[A-Z]+$', underlying):
        raise ValueError(f"Invalid underlying: {underlying}")

    if not re.match(r'^\d{6}$', expiry_yymmdd):
        raise ValueError(f"Invalid expiry format: {expiry_yymmdd}")

    if right not in ('C', 'P'):
        raise ValueError(f"Invalid right (must be C or P): {right}")

    if not re.match(r'^\d{8}$', strike_str):
        raise ValueError(f"Invalid strike format: {strike_str}")

    # Parse expiry date
    try:
        dt = datetime.strptime(expiry_yymmdd, '%y%m%d')
        expiry_date = dt.strftime('%Y-%m-%d')
    except ValueError:
        raise ValueError(f"Invalid expiry date: {expiry_yymmdd}")

    # Parse strike (divide by 1000 to get actual price)
    strike = int(strike_str) / 1000.0

    return {
        'underlying': underlying,
        'expiry_date': expiry_date,
        'right': right,
        'strike': strike,
    }


def validate_occ_symbol(occ_symbol: str) -> bool:
    """Check if a string is a valid OCC symbol.

    Args:
        occ_symbol: Symbol to validate

    Returns:
        True if valid, False otherwise
    """
    try:
        parse_occ_symbol(occ_symbol)
        return True
    except (ValueError, IndexError):
        return False


def get_contract_description(occ_symbol: str) -> str:
    """Get human-readable description of an OCC symbol.

    Args:
        occ_symbol: OCC-formatted option symbol

    Returns:
        Human-readable description

    Examples:
        >>> get_contract_description('SPY   250120C00450000')
        'SPY 2025-01-20 450.00 Call'
    """
    try:
        parsed = parse_occ_symbol(occ_symbol)
        right_name = 'Call' if parsed['right'] == 'C' else 'Put'
        return f"{parsed['underlying']} {parsed['expiry_date']} {parsed['strike']:.2f} {right_name}"
    except ValueError:
        return f"Invalid OCC symbol: {occ_symbol}"
