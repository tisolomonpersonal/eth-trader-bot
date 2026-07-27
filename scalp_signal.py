"""
Rule-based BTC scalping signal engine.

Deliberately NOT AI-driven: a scalp decision has to be deterministic, instant,
and backtestable. An LLM round-trip per cycle adds latency and non-reproducible
noise to a timeframe measured in minutes. config.AI_VETO_ENABLED can re-add the
model as a veto on top of these rules, but it can never invent an entry.

Two setups, switched by volatility regime, because they fail in opposite
conditions and so cover for each other:

  RANGE regime (low ADX)  -> mean-reversion fade back to VWAP / band mid
  TREND regime (high ADX) -> squeeze breakout, or pullback continuation

Spot is long-only, so every setup here is a LONG. On a perp the mirrored short
of each is equally valid and roughly doubles the opportunity set — see
`_MIRROR_NOTE` at the bottom.

All functions are pure: (indicators, state) -> Signal. No I/O, no globals.
"""
from dataclasses import dataclass
from typing import Optional

import config
from logger import get_logger

log = get_logger("scalp_signal")


@dataclass
class Signal:
    action: str                      # "BUY" | "SELL" | "HOLD"
    confidence: int                  # 0-100
    reason: str
    setup: str                       # which rule fired, "" when HOLD
    target: Optional[float] = None   # setup's natural profit objective
    provider: str = "rules"


def _hold(reason: str) -> Signal:
    return Signal(action="HOLD", confidence=0, reason=reason, setup="")


# ── Entry setups ──────────────────────────────────────────────────────────────

def _mean_reversion(ind: dict) -> Optional[Signal]:
    """
    Setup 1 — fade an overextension back to the mean. BTC on 1m spends most of
    its life ranging, which makes this the highest-frequency setup, but it is
    ONLY valid while ADX confirms no trend. Fading a real trend is how scalpers
    blow up, so the regime gate here is not optional.

    Entry: price stretched below the lower Bollinger band with RSI oversold.
    Target: the band mid / VWAP, whichever is nearer (conservative).
    """
    if ind["regime"] != "RANGE":
        return None

    # Never fade a market that the 5m chart says is falling — that's a knife.
    if ind["htf_bias"] == "BEARISH":
        return None

    stretched = ind["bb_pct_b"] <= 0.05          # at or below the lower band
    oversold = ind["rsi"] <= config.RSI_OVERSOLD
    below_vwap = ind["price"] < ind["vwap"]

    if not (stretched and oversold and below_vwap):
        return None

    # Target the nearer of band-mid / VWAP so the objective is realistic.
    target = min(ind["bb_mid"], ind["vwap"])
    if target <= ind["price"]:
        return None  # mean is already below us — no room, skip

    confidence = 60
    if ind["rsi"] <= config.RSI_OVERSOLD - 5:
        confidence += 10
    if ind["htf_bias"] == "BULLISH":
        confidence += 10          # fading a dip inside an uptrend is the A+ case
    if ind["vol_trend"] == "High":
        confidence += 5           # capitulation volume marks exhaustion

    return Signal(
        action="BUY",
        confidence=min(confidence, 95),
        reason=(f"Mean-reversion fade: price ${ind['price']:,.2f} at lower band "
                f"(%B={ind['bb_pct_b']:.2f}), RSI {ind['rsi']}, ADX {ind['adx']} "
                f"confirms range. Target ${target:,.2f}."),
        setup="MEAN_REVERSION",
        target=target,
    )


def _squeeze_breakout(ind: dict) -> Optional[Signal]:
    """
    Setup 2 — volatility expansion. Bollinger bandwidth compressed into the
    bottom quartile of its recent range means energy is coiled; the subsequent
    expansion is directional and fast. This is BTC's signature move.

    Fires exactly when mean-reversion is switched off, so the two setups
    complement rather than compete.

    Entry: squeeze present (or just released) + price breaking the recent high
    on above-average volume.
    Target: measured move — breakout level plus the compressed range height.
    """
    if ind["regime"] == "RANGE" and not ind["squeeze"]:
        return None

    # Only take breakouts in the direction the 5m chart supports.
    if ind["htf_bias"] == "BEARISH":
        return None

    breaking_out = ind["price"] >= ind["recent_high"] * 0.9995
    volume_confirms = ind["vol_trend"] == "High"
    momentum_up = ind["macd_hist"] > 0 and ind["ema9"] > ind["ema21"]

    if not (breaking_out and volume_confirms and momentum_up):
        return None

    # Measured move: project the pre-breakout range height off the break level.
    range_height = ind["recent_high"] - ind["recent_low"]
    target = ind["price"] + range_height * 0.5

    confidence = 55
    if ind["squeeze"]:
        confidence += 15          # a true squeeze release is the high-quality version
    if ind["htf_bias"] == "BULLISH":
        confidence += 10
    if ind["adx"] >= config.TREND_ADX_MIN:
        confidence += 5

    return Signal(
        action="BUY",
        confidence=min(confidence, 95),
        reason=(f"Squeeze breakout: price ${ind['price']:,.2f} clearing "
                f"${ind['recent_high']:,.2f} on high volume, "
                f"squeeze={ind['squeeze']}, ADX {ind['adx']}. "
                f"Measured target ${target:,.2f}."),
        setup="SQUEEZE_BREAKOUT",
        target=target,
    )


def _trend_pullback(ind: dict) -> Optional[Signal]:
    """
    Setup 3 — buy the dip inside an established uptrend. Lower conviction than
    the two above (it's the most crowded retail setup, so edge is thinner), but
    it fills the gap when neither of the others qualifies.

    Entry: 5m bullish + 1m trending + price pulled back to EMA21 and reclaimed EMA9.
    """
    if ind["htf_bias"] != "BULLISH":
        return None
    if ind["regime"] != "TREND":
        return None

    pulled_back = ind["price"] <= ind["ema21"] * 1.001
    reclaiming = ind["price"] > ind["ema9"]
    not_overbought = ind["rsi"] < 65

    if not (pulled_back and reclaiming and not_overbought):
        return None

    target = ind["recent_high"]
    if target <= ind["price"]:
        return None

    confidence = 55
    if ind["vol_trend"] != "Low":
        confidence += 5
    if ind["macd_hist"] > 0:
        confidence += 5

    return Signal(
        action="BUY",
        confidence=min(confidence, 90),
        reason=(f"Trend pullback: 5m bullish, price ${ind['price']:,.2f} held "
                f"EMA21 ${ind['ema21']:,.2f} and reclaimed EMA9. "
                f"Target ${target:,.2f}."),
        setup="TREND_PULLBACK",
        target=target,
    )


# Order matters: highest-conviction setup first, first match wins.
_SETUPS = (_mean_reversion, _squeeze_breakout, _trend_pullback)


# ── Exit logic ────────────────────────────────────────────────────────────────

def _exit_signal(ind: dict, state: dict) -> Optional[Signal]:
    """
    Signal-based exits, checked before hard SL/TP brackets get a chance to fire.
    A scalp thesis can die before the stop is hit — when the reason for the
    trade evaporates, leave, rather than donating the difference to the stop.
    """
    setup = state.get("setup", "")

    # Mean-reversion trades exist purely to reach the mean. Once price is back
    # at VWAP/band-mid the edge is spent regardless of what the P&L says.
    if setup == "MEAN_REVERSION":
        if ind["price"] >= min(ind["bb_mid"], ind["vwap"]):
            return Signal("SELL", 80, "Mean reached — fade objective complete.", setup)
        if ind["regime"] == "TREND" and ind["htf_bias"] == "BEARISH":
            return Signal("SELL", 85,
                          "Range broke into a bearish trend — fade thesis invalidated.",
                          setup)

    # Breakouts that lose momentum tend to fully retrace the break.
    if setup == "SQUEEZE_BREAKOUT":
        if ind["ema9"] < ind["ema21"] and ind["macd_hist"] < 0:
            return Signal("SELL", 75,
                          "Breakout momentum lost (EMA9 back under EMA21).", setup)

    if setup == "TREND_PULLBACK":
        if ind["htf_bias"] == "BEARISH":
            return Signal("SELL", 80, "Higher-timeframe trend flipped bearish.", setup)
        if ind["rsi"] > 75:
            return Signal("SELL", 70, "RSI overbought — taking the scalp.", setup)

    return None


# ── Public entry point ────────────────────────────────────────────────────────

def get_signal(ind: dict, state: dict) -> Signal:
    """
    Evaluate all rules and return a single decision.

    Mirrors ai.get_signal()'s contract so strategy.py can swap between them,
    but is deterministic and adds no network latency.
    """
    if state.get("in_position"):
        exit_sig = _exit_signal(ind, state)
        if exit_sig:
            return exit_sig
        return _hold(f"Holding {state.get('setup','position')} — thesis intact, "
                     f"brackets managing the trade.")

    for setup_fn in _SETUPS:
        sig = setup_fn(ind)
        if sig:
            log.info(f"[setup] {sig.setup} fired conf={sig.confidence}")
            return sig

    return _hold(f"No setup. regime={ind['regime']} ADX={ind['adx']} "
                 f"RSI={ind['rsi']} squeeze={ind['squeeze']} bias={ind['htf_bias']}")


# ── Note on the missing half of this strategy ─────────────────────────────────
_MIRROR_NOTE = """
Every setup above has an exact short mirror that spot cannot express:

  MEAN_REVERSION   -> fade %B >= 0.95 with RSI >= 70 back down to VWAP
  SQUEEZE_BREAKOUT -> short the break of recent_low on expansion
  TREND_PULLBACK   -> short rallies to EMA21 inside a 5m downtrend

On Bybit linear perps (category="linear") these are available, and round-trip
fees drop from 0.20% to ~0.11% taker / 0.04% maker. That combination — double
the setups at a quarter of the cost — is a far larger improvement to expectancy
than any amount of tuning to the rules above. It is the single highest-value
change available to this bot, at the cost of taking on liquidation risk.
"""
