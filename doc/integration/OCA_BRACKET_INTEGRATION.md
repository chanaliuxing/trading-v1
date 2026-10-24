# OCA Bracket Order Integration Guide

## Overview

The Premarket Gainer Strategy uses **OCA (One-Cancels-All) Bracket Orders** with **ReduceByAmount** type to implement multi-level take-profit and stop-loss without requiring manual order amendments.

## Order Structure

```
Parent Order (STP_LMT)
    ├── Entry: Trigger at breakout level
    └── transmit=False (not sent yet)

Child Orders (Same OCA Group)
    ├── Stop Loss (STP)
    │   ├── qty = full position
    │   ├── ocaGroup = "G_{strategy}_{symbol}_{date}"
    │   ├── ocaType = "REDUCE"  (ReduceByAmount)
    │   └── transmit = False
    │
    ├── TP1 (LMT)
    │   ├── qty = 30% of position
    │   ├── limit = entry * 1.035 (+3.5%)
    │   ├── ocaGroup = same as SL
    │   ├── ocaType = "REDUCE"
    │   └── transmit = False
    │
    ├── TP2 (LMT)
    │   ├── qty = 35% of position
    │   ├── limit = entry * 1.055 (+5.5%)
    │   ├── ocaGroup = same as SL
    │   ├── ocaType = "REDUCE"
    │   └── transmit = False
    │
    └── TP3 (LMT)  [LAST CHILD - TRIGGERS TRANSMISSION]
        ├── qty = 35% of position
        ├── limit = entry * 1.08 (+8.0%)
        ├── ocaGroup = same as SL
        ├── ocaType = "REDUCE"
        └── transmit = TRUE  ★ Atomic submission
```

## DecisionEnvelope → IBKR Mapping

### Parent Order

```python
DecisionEnvelope(
    decision_id="parent-123",
    symbol="TSLA",
    target_qty=100,  # BUY 100 shares
    order_style=OrderStyle(
        kind="STP_LMT",
        stop_px=248.50,
        limit_px=248.99,
        tif="DAY"
    ),
    extras={
        "leg": "PARENT",
        "bracket_id": "brkt-tsla-20251016",
        "transmit": False
    }
)
```

**IBKR Mapping:**
- `orderType` → "STP LMT"
- `action` → "BUY"
- `totalQuantity` → 100
- `auxPrice` → 248.50 (stop trigger)
- `lmtPrice` → 248.99
- `tif` → "DAY"
- `transmit` → False

---

### Stop Loss Order

```python
DecisionEnvelope(
    decision_id="sl-456",
    symbol="TSLA",
    target_qty=-100,  # SELL 100 shares
    order_style=OrderStyle(
        kind="STP",
        stop_px=244.80
    ),
    extras={
        "leg": "SL",
        "parent_ref": "parent-123",
        "oca_group": "G_pre_v1_TSLA_20251016",
        "oca_type": "REDUCE",
        "transmit": False
    }
)
```

**IBKR Mapping:**
- `orderType` → "STP"
- `action` → "SELL"
- `totalQuantity` → 100
- `auxPrice` → 244.80
- `parentId` → (resolved from parent_ref)
- `ocaGroup` → "G_pre_v1_TSLA_20251016"
- `ocaType` → **2** (ReduceWithBlock / ReduceByAmount)
- `transmit` → False

---

### Take Profit Orders (TP1, TP2, TP3)

```python
DecisionEnvelope(
    decision_id="tp1-789",
    symbol="TSLA",
    target_qty=-30,  # SELL 30 shares
    order_style=OrderStyle(
        kind="LMT",
        limit_px=257.70
    ),
    extras={
        "leg": "TP1",
        "parent_ref": "parent-123",
        "oca_group": "G_pre_v1_TSLA_20251016",
        "oca_type": "REDUCE",
        "transmit": False  # or True for last TP
    }
)
```

**IBKR Mapping:**
- `orderType` → "LMT"
- `action` → "SELL"
- `totalQuantity` → 30 (TP1) / 35 (TP2) / 35 (TP3)
- `lmtPrice` → 257.70 (TP1) / 262.20 (TP2) / 268.00 (TP3)
- `parentId` → (resolved from parent_ref)
- `ocaGroup` → "G_pre_v1_TSLA_20251016"
- `ocaType` → **2**
- `transmit` → False (TP1/TP2), **True (TP3)** ← Atomic submission

---

## Critical Implementation Points

### 1. OCA Type Enumeration

Different SDKs use different naming:
- **ib_insync**: `OcaType.REDUCE_WITH_BLOCK` or enum value `2`
- **TWS API Java**: `OcaType.REDUCE_WITH_BLOCK` (2)
- **Interactive Brokers C++/Python**: Integer `2`

**Action Required:**
- Verify your adapter's constant/enum for "按成交量减少其它订单"
- Map `extras["oca_type"] = "REDUCE"` → correct enum value

### 2. Submission Order

**MUST submit in this sequence:**
1. Parent (transmit=False)
2. Stop Loss (transmit=False)
3. TP1 (transmit=False)
4. TP2 (transmit=False)
5. TP3 (transmit=**True**) ← Triggers atomic submission

**Why?** IBKR requires all orders in a bracket to be submitted before transmission. The last `transmit=True` sends the entire bracket atomically.

### 3. ParentId Resolution

Execution adapter must:
- Store mapping: `decision_id` → IBKR `orderId`
- When processing child orders with `extras["parent_ref"]`, resolve to actual IBKR `orderId`

### 4. OCA Group Naming

**Format:** `G_{strategy_id}_{symbol}_{date}`

Example: `G_premarket_gainer_001_TSLA_20251016`

**Why?** Unique per strategy + symbol + day to avoid cross-contamination.

### 5. OCA Behavior Verification

**Expected Behavior:**
- Parent fills → All children become active
- TP1 fills 30 shares → SL reduces from 100 to 70, TP2/TP3 remain at 35 each
- TP2 fills 35 shares → SL reduces from 70 to 35, TP3 remains at 35
- TP3 fills 35 shares → SL cancels (position = 0)

**Test in Paper Trading:**
- Manually trigger partial fills
- Verify child orders auto-reduce quantities
- Confirm no over-selling or naked positions

---

## Execution Adapter Checklist

### ✅ Required Mappings

- [ ] `DecisionEnvelope.order_style.kind` → IBKR `orderType`
  - `"STP_LMT"` → `"STP LMT"`
  - `"STP"` → `"STP"`
  - `"LMT"` → `"LMT"`
  - `"MKT"` → `"MKT"`

- [ ] `DecisionEnvelope.order_style.stop_px` → IBKR `auxPrice` or `stopPrice`

- [ ] `DecisionEnvelope.order_style.limit_px` → IBKR `lmtPrice`

- [ ] `DecisionEnvelope.target_qty` → IBKR `action` + `totalQuantity`
  - Positive → `action="BUY"`, `qty=abs(target_qty)`
  - Negative → `action="SELL"`, `qty=abs(target_qty)`

- [ ] `DecisionEnvelope.extras["parent_ref"]` → IBKR `parentId`
  - Resolve decision_id to orderId via internal mapping

- [ ] `DecisionEnvelope.extras["oca_group"]` → IBKR `ocaGroup`

- [ ] `DecisionEnvelope.extras["oca_type"]` → IBKR `ocaType`
  - `"REDUCE"` → Integer `2` (or OcaType enum)

- [ ] `DecisionEnvelope.extras["transmit"]` → IBKR `transmit`

### ✅ Required Validations

- [ ] Bracket orders submitted in correct sequence (Parent → SL → TP1-3)
- [ ] Last child has `transmit=True`, others `transmit=False`
- [ ] All children share same `ocaGroup`
- [ ] All children have `ocaType=2` (ReduceByAmount)
- [ ] ParentId correctly resolved before submission
- [ ] No duplicate `ocaGroup` across different brackets

---

## Example Flow (Pseudo-code)

```python
# Execution Adapter receives 5 DecisionEnvelopes in sequence

envelope_queue = [parent, sl, tp1, tp2, tp3]
oca_state = {}  # Track bracket state

for envelope in envelope_queue:
    # 1. Map to IBKR Order
    ib_order = map_decision_to_ib_order(envelope)

    # 2. Resolve parentId if child order
    if envelope.extras.get("parent_ref"):
        parent_id = resolve_decision_id(envelope.extras["parent_ref"])
        ib_order.parentId = parent_id

    # 3. Set OCA fields
    if envelope.extras.get("oca_group"):
        ib_order.ocaGroup = envelope.extras["oca_group"]
        ib_order.ocaType = 2  # ReduceByAmount

    # 4. Set transmit flag
    ib_order.transmit = envelope.extras.get("transmit", False)

    # 5. Submit to IBKR
    order_id = ibkr_client.placeOrder(contract, ib_order)

    # 6. Store mapping
    decision_id_map[envelope.decision_id] = order_id

    if ib_order.transmit:
        logger.info(f"✅ Bracket submitted atomically | OCA={ib_order.ocaGroup}")
```

---

## Debugging Tips

### Check TWS Order Status

1. Open TWS → Order Management
2. Find your symbol
3. Verify:
   - All child orders show same OCA Group
   - OCA Type = "Reduce with Block" or "2"
   - Parent order shows as "Parent"

### Simulate Partial Fill

```python
# In paper trading, manually execute TP1 at limit price
# Expected: SL and other TPs auto-reduce quantity
```

### Log OCA Fields

```python
logger.info("OCA Order Submitted",
           order_id=order_id,
           oca_group=ib_order.ocaGroup,
           oca_type=ib_order.ocaType,
           parent_id=ib_order.parentId,
           qty=ib_order.totalQuantity,
           transmit=ib_order.transmit)
```

---

## Testing Checklist

### Unit Tests
- [ ] DecisionEnvelope → IB Order mapping
- [ ] ParentId resolution
- [ ] OCA Type enum mapping
- [ ] Submission sequence validation

### Integration Tests (Paper Trading)
- [ ] Scanner → Top3 → Entry bracket submission
- [ ] Parent fills → All children active
- [ ] TP1 partial fill → SL/TP2/TP3 auto-reduce
- [ ] TP1 full + TP2 partial → SL/TP3 auto-reduce
- [ ] Force exit at 10:10 → All remaining orders cancel

### Performance Tests
- [ ] Signal → Order submission < 300ms (P95)
- [ ] Entry slippage < 10bp (average over 10 trades)
- [ ] No naked positions (position = sum of active child orders)

---

## References

- [IBKR OCA Types Documentation](https://interactivebrokers.github.io/tws-api/orders.html#oca)
- [ib_insync OCA Example](https://ib-insync.readthedocs.io/recipes.html#bracket-orders)
- Strategy Doc: `doc/newStrategy/premarket.md`
- Sample DecisionEnvelopes: `doc/newStrategy/premarket.md` lines 600-710

---

## Support

If OCA auto-reduce is not working:
1. Verify `ocaType=2` in TWS Order Manager
2. Check TWS logs for "OCA violation" errors
3. Ensure all children submitted **before** transmit=True
4. Test with minimal example (1 parent + 2 children)

**Contact:** Check `#trading-execution` channel or execution team
