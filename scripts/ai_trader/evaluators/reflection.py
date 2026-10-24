import re


PATTERNS = {
    "above_vwap": r"(above|over)\s+vwap|price\s+above\s+vwap|vwap\s+support",
    "rvol_gt_2": r"(rvol|relative\s+volume)\s*[>≥]\s*2|volume\s+spike|high\s+rvol",
    "break_premarket_high": r"(break(out)?|above|through).*(pre-?market|premarket).*high",
}


def extract_claims(text: str):
    text = (text or "").lower()
    return [{"rule": k, "stated": bool(re.search(p, text))} for k, p in PATTERNS.items()]


def check_claims(claims, feats: dict) -> dict:
    def ok(rule):
        if rule == "above_vwap":
            return feats.get("close", 0) > feats.get("vwap", 1e12)
        if rule == "rvol_gt_2":
            return feats.get("rvol20", 0) > 2.0
        if rule == "break_premarket_high":
            return feats.get("last", 0) >= feats.get("premkt_high", 1e12) * 0.999
        return None

    res = []
    passed = 0
    total = 0
    stated_total = 0
    stated_pass = 0
    for c in claims:
        actual = ok(c["rule"])
        if actual is None:
            continue
        total += 1
        passed += 1 if bool(actual) else 0
        if bool(c.get("stated")):
            stated_total += 1
            stated_pass += 1 if bool(actual) else 0
        res.append({**c, "actual": actual, "ok": bool(actual)})

    market_alignment = passed / max(1, total)
    claim_consistency = stated_pass / max(1, stated_total)
    # Keep backward-compatibility: 'consistency' == market_alignment
    return {
        "claims": res,
        "consistency": market_alignment,
        "market_alignment": market_alignment,
        "claim_consistency": claim_consistency,
        "counts": {
            "rules_total": total,
            "rules_pass": passed,
            "stated_total": stated_total,
            "stated_pass": stated_pass,
        },
    }
