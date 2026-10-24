# grid_guard.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional
import re

Number = float | int


@dataclass
class GuardConfig:
    """Configuration for grid parameter constraints and validation."""
    cap_per_param: int = 5
    # Basic constraints
    bounds: Dict[str, Tuple[Number, Number]] = None         # {"ema_short":[3,50], ...}
    types: Dict[str, str] = None                             # {"ema_short":"int"|"float"|"cat"}
    choices: Dict[str, List[Any]] = None                     # {"side":["long","short","both"]}
    # Relational constraints (e.g., "ema_short < ema_long", "stop_loss_bps >= 0")
    relations: List[str] = None
    # Conditional constraints (e.g., if use_filter==0, drop filter_thr)
    conditionals: List[Dict[str, Any]] = None                # [{"if":"use_filter==0","drop":["filter_thr","filter_len"]}]
    # Parameter whitelist (optional)
    allow_params: Optional[List[str]] = None
    # Step alignment for integer parameters
    steps: Dict[str, int] = None                             # {"atr_n": 1, "ema_short": 1}


def _coerce_type(v: Any, t: str) -> Any:
    """Coerce value to specified type."""
    if t == "int":
        try:
            return int(round(float(v)))
        except Exception:
            return None
    if t == "float":
        try:
            return float(v)
        except Exception:
            return None
    # cat: return as-is
    return v


def _clip_bounds(v: Any, lo: Number, hi: Number, t: str) -> Any:
    """Clip value to bounds and coerce to type."""
    try:
        x = float(v)
        x = max(lo, min(hi, x))
        if t == "int":
            return int(round(x))
        if t == "float":
            return float(x)
        return v
    except Exception:
        return None


def _apply_step(v: Any, step: int, t: str) -> Any:
    """Apply step alignment for integer parameters."""
    if t != "int" or step is None or step <= 1:
        return v
    try:
        return int(round(int(v) / step) * step)
    except Exception:
        return v


def _parse_rel(rel: str) -> Tuple[str, str, str]:
    """Parse relational constraint expression."""
    # Supports: <, <=, >, >=, ==, !=
    m = re.match(r"^\s*([A-Za-z0-9_]+)\s*(<=|>=|<|>|==|!=)\s*([A-Za-z0-9_\.]+)\s*$", rel)
    if not m:
        raise ValueError(f"Bad relation: {rel}")
    return m.group(1), m.group(2), m.group(3)


def _relation_prune(grid: Dict[str, List[Any]], rels: List[str], types: Dict[str, str]) -> Tuple[Dict[str, List[Any]], Dict[str, Any]]:
    """
    Prune grid values based on binary relations (e.g., a < b).
    For a < b: remove a values >= max(b), remove b values <= min(a).
    Conservative approximation that reduces illegal combinations.
    """
    report = {"relations_applied": [], "pruned": {}}
    g = {k: list(dict.fromkeys(v)) for k, v in grid.items()}  # deduplicate

    for expr in (rels or []):
        try:
            L, op, R = _parse_rel(expr)
        except Exception:
            continue
        if L not in g or R not in g:
            continue
        tL = (types or {}).get(L, "float")
        tR = (types or {}).get(R, "float")

        left_vals  = g[L][:]
        right_vals = g[R][:]

        # Only prune for numeric relations
        if tL in ("int","float") and tR in ("int","float"):
            try:
                lnums = sorted(float(x) for x in left_vals)
                rnums = sorted(float(x) for x in right_vals)
            except Exception:
                continue

            before = (len(left_vals), len(right_vals))

            def keep_left(x):
                if op == "<":  return x < max(rnums)
                if op == "<=": return x <= max(rnums)
                if op == ">":  return x > min(rnums)
                if op == ">=": return x >= min(rnums)
                if op == "==": return min(rnums) <= x <= max(rnums)
                if op == "!=": return True  # hard to prune boundaries, leave to combination layer
                return True

            def keep_right(y):
                if op == "<":  return y > min(lnums)
                if op == "<=": return y >= min(lnums)
                if op == ">":  return y < max(lnums)
                if op == ">=": return y <= max(lnums)
                if op == "==": return min(lnums) <= y <= max(lnums)
                if op == "!=": return True
                return True

            left_vals  = [v for v in left_vals  if keep_left (float(v))]
            right_vals = [v for v in right_vals if keep_right(float(v))]

            g[L] = left_vals
            g[R] = right_vals
            after = (len(left_vals), len(right_vals))
            report["relations_applied"].append(expr)
            if before != after:
                report["pruned"][expr] = {"before": before, "after": after}
        else:
            # Non-numeric relations skipped (can filter in HPO combination layer)
            continue
    return g, report


def _apply_conditionals(grid: Dict[str, List[Any]], conds: List[Dict[str, Any]]) -> Tuple[Dict[str, List[Any]], Dict[str, Any]]:
    """
    Apply conditional constraints: if param==value, drop specified parameters.
    Supports 'param==value' and 'param!=value' expressions.
    """
    rep = {"conditionals": []}
    g = {k: list(v) for k, v in grid.items()}

    for c in (conds or []):
        expr = str(c.get("if","")).strip()
        drop = c.get("drop", [])
        m_eq = re.match(r"^\s*([A-Za-z0-9_]+)\s*==\s*([A-Za-z0-9_\.-]+)\s*$", expr)
        m_ne = re.match(r"^\s*([A-Za-z0-9_]+)\s*!=\s*([A-Za-z0-9_\.-]+)\s*$", expr)

        def _satisfied():
            if not m_eq and not m_ne:
                return False
            p = (m_eq or m_ne).group(1)
            val = (m_eq or m_ne).group(2)
            # If grid contains this value, condition may be satisfied, conservatively apply drop
            return p in g and any(str(x)==val for x in g[p])

        if _satisfied():
            for d in drop:
                if d in g:
                    del g[d]
            rep["conditionals"].append({"if": expr, "drop": drop})
    return g, rep


def sanitize_and_constrain_grid(
    grid: Dict[str, List[Any]],
    guard_cfg: Dict[str, Any] | None
) -> Tuple[Dict[str, List[Any]], Dict[str, Any]]:
    """
    Entry point: Apply full parameter guardrails to LLM/ML/merged grids.

    Applies:
    1. Type coercion and validation
    2. Bounds clipping
    3. Whitelist filtering
    4. Choices validation
    5. Step alignment
    6. Conditional constraints
    7. Relational constraints

    Args:
        grid: Raw parameter grid {param_name: [values]}
        guard_cfg: Guard configuration dict

    Returns:
        (sanitized_grid, report) - cleaned grid and audit report
    """
    guard = GuardConfig(
        cap_per_param = (guard_cfg or {}).get("cap_per_param", 5),
        bounds        = (guard_cfg or {}).get("bounds") or {},
        types         = (guard_cfg or {}).get("types") or {},
        choices       = (guard_cfg or {}).get("choices") or {},
        relations     = (guard_cfg or {}).get("relations") or [],
        conditionals  = (guard_cfg or {}).get("conditionals") or [],
        allow_params  = (guard_cfg or {}).get("allow_params"),
        steps         = (guard_cfg or {}).get("steps") or {},
    )

    report: Dict[str, Any] = {
        "dropped": {}, "coerced": {}, "clipped": {},
        "choices": {}, "steps": {}, "cap": guard.cap_per_param
    }

    # 1) Whitelist filtering
    g = {}
    for k, vals in (grid or {}).items():
        if guard.allow_params and k not in guard.allow_params:
            report["dropped"].setdefault(k, []).extend(vals)
            continue
        g[k] = list(vals) if isinstance(vals, list) else [vals]

    # 2) Type coercion, bounds, choices, steps, deduplication, capping
    out: Dict[str, List[Any]] = {}
    for k, vals in g.items():
        t = guard.types.get(k, "float" if k not in (guard.choices or {}) else "cat")
        lo, hi = None, None
        if k in (guard.bounds or {}):
            lo, hi = guard.bounds[k]
        cho = (guard.choices or {}).get(k)
        step = (guard.steps or {}).get(k)

        cleaned = []
        for v in vals:
            vv = _coerce_type(v, t)
            if vv is None:
                report["dropped"].setdefault(k, []).append(v)
                continue

            # Validate choices
            if cho is not None:
                if vv not in cho:
                    report["dropped"].setdefault(k, []).append(vv)
                    continue
                report["choices"].setdefault(k, []).append(vv)

            # Clip bounds
            if lo is not None and hi is not None and t in ("int","float"):
                v_before = vv
                vv = _clip_bounds(vv, lo, hi, t)
                if vv is None:
                    report["dropped"].setdefault(k, []).append(v_before)
                    continue
                if v_before != vv:
                    report["clipped"].setdefault(k, []).append({"from": v_before, "to": vv})

            # Apply step alignment
            if step is not None:
                v_before = vv
                vv = _apply_step(vv, step, t)
                if v_before != vv:
                    report["steps"].setdefault(k, []).append({"from": v_before, "to": vv})

            cleaned.append(vv)

        # Deduplicate and cap
        uniq = []
        for x in cleaned:
            if x not in uniq:
                uniq.append(x)
        out[k] = uniq[:guard.cap_per_param]

    # 3) Apply conditional constraints
    out, rep_cnd = _apply_conditionals(out, guard.conditionals)
    report.update(rep_cnd)

    # 4) Apply relational constraints
    out, rep_rel = _relation_prune(out, guard.relations or [], guard.types or {})
    report.update(rep_rel)

    # 5) Apply combination limit (prevent grid explosion)
    max_total_points = (guard_cfg or {}).get("max_total_points", 300)
    out, rep_prune = _prune_to_max_combinations(out, max_total_points)
    report.update(rep_prune)

    return out, report


def _prune_to_max_combinations(
    grid: Dict[str, List[Any]],
    max_total_points: int
) -> Tuple[Dict[str, List[Any]], Dict[str, Any]]:
    """
    Prune grid to limit total number of combinations.

    Strategy:
    1. Calculate current total combinations
    2. If over limit, reduce values per parameter proportionally
    3. Prioritize parameters with fewer values (keep diversity)

    Args:
        grid: Parameter grid
        max_total_points: Maximum total combinations allowed

    Returns:
        (pruned_grid, report)
    """
    import math

    if not grid:
        return grid, {"combinations_pruned": False}

    # Calculate current combinations
    def calc_combinations(g):
        total = 1
        for vals in g.values():
            total *= len(vals)
        return total

    before = calc_combinations(grid)

    if before <= max_total_points:
        return grid, {
            "combinations_pruned": False,
            "total_combinations_before": before,
            "total_combinations_after": before
        }

    # Need to prune
    out = {k: list(v) for k, v in grid.items()}

    # Strategy: iteratively reduce the parameter with most values
    # until we're under the limit
    pruned_params = []

    while calc_combinations(out) > max_total_points:
        # Find parameter with most values
        max_len = 0
        max_param = None
        for k, vals in out.items():
            if len(vals) > max_len and len(vals) > 1:  # Don't reduce to 0
                max_len = len(vals)
                max_param = k

        if max_param is None:
            break  # Can't reduce further

        # Remove one value (keep first and last, remove from middle)
        vals = out[max_param]
        if len(vals) > 2:
            # Remove middle element
            mid_idx = len(vals) // 2
            removed = vals.pop(mid_idx)
        else:
            # Remove last
            removed = vals.pop()

        pruned_params.append({
            "param": max_param,
            "removed_value": removed,
            "remaining_count": len(vals)
        })

    after = calc_combinations(out)

    return out, {
        "combinations_pruned": True,
        "total_combinations_before": before,
        "total_combinations_after": after,
        "max_allowed": max_total_points,
        "pruned_params": pruned_params,
        "pruned_count": len(pruned_params)
    }
