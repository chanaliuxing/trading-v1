from __future__ import annotations

from .contracts import DirectionDecision, EntryContext, TranslationDecision


def tqqq_sqqq_translation(ctx: EntryContext, decision: DirectionDecision) -> TranslationDecision:
    if decision.direction == "LONG":
        return TranslationDecision(
            allowed=True,
            reason=decision.reason,
            side="TQQQ",
            stock_side="LONG",
            payload={
                **dict(decision.payload or {}),
                "entry_model": ctx.entry_model,
            },
        )
    if decision.direction == "SHORT":
        return TranslationDecision(
            allowed=True,
            reason=decision.reason,
            side="SQQQ",
            stock_side="SHORT",
            payload={
                **dict(decision.payload or {}),
                "entry_model": ctx.entry_model,
            },
        )
    return TranslationDecision(
        allowed=False,
        reason=decision.reason,
        side=None,
        stock_side="NONE",
        payload=dict(decision.payload or {}),
    )


def qqq_option_translation(ctx: EntryContext, decision: DirectionDecision) -> TranslationDecision:
    if decision.direction == "LONG":
        return TranslationDecision(
            allowed=True,
            reason=decision.reason,
            side="CALL",
            stock_side="LONG",
            payload={
                **dict(decision.payload or {}),
                "entry_model": ctx.entry_model,
            },
        )
    if decision.direction == "SHORT":
        return TranslationDecision(
            allowed=True,
            reason=decision.reason,
            side="PUT",
            stock_side="SHORT",
            payload={
                **dict(decision.payload or {}),
                "entry_model": ctx.entry_model,
            },
        )
    return TranslationDecision(
        allowed=False,
        reason=decision.reason,
        side=None,
        stock_side="NONE",
        payload=dict(decision.payload or {}),
    )
