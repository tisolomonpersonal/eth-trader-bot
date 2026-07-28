"""
Rule-based BTC perpetual scalping signal engine.

Deliberately NOT AI-driven: a scalp decision has to be deterministic, instant,
and backtestable. An LLM round-trip per cycle adds latency and non-reproducible
noise to a timeframe measured in minutes. config.AI_VETO_ENABLED can re-add the
model as a veto on top of these rules, but it can never invent an entry.

Three setups, each with a long and a short form. Trading perps rather than spot
is what makes the shorts possible, and they matter: BTC downtrends are faster
and more violent than uptrends, so the short side of a mean-reversion book is
often where the better risk/reward sits. A long-only scalper sits out half the
market.

Regime, from ADX, decides which setup is armed — mean reversion and breakout
fail in opposite conditions, so each covers for the other:

  RANGE  (low ADX)  -> mean-reversion fade back to VWAP / band mid
  TREND  (high ADX) -> squeeze breakout, or pullback continuation

Actions are LONG / SHORT / CLOSE / HOLD. All functions are pure:
(indicators, state) -> Signal. No I/O, no globals.
"""
from dataclasses import dataclass
from typing import Optional

import config
from logger import get_logger

log = get_logger("scalp_signal")


@dataclass
class Signal:
    action: str                      # "LONG" | "SHORT" | "CLOSE" | "HOLD"
    confidence: int                  # 0-100
    reason: str
    setup: str                       # which rule fired, "" when HOLD
    target: Optional[float] = None   # setup's natural profit objective
    provider: str = "rules"

    @property
    def is_entry(self) -> bool:
        return self.action in ("LONG", "SHORT")

    @property
    def side(self) -> Optional[str]:
        """Bybit order side for this action."""
        return {"LONG": "Buy", "SHORT": "Sell"}.get(self.action)


def _hold(reason: str) -> Signal:
    return Signal(action="HOLD", confidence=0, reason=reason, setup="")


# ── Setup 1: mean reversion ───────────────────────────────────────────────────

def _mean_reversion(ind: dict) -> Optional[Signal]:
    """
    Fade an overextension back to the mean. BTC on 1m spends most of its life
    ranging, which makes this the highest-frequency setup — but it is ONLY
    valid while ADX confirms no trend. Fading a real trend is how scalpers blow
    up, so the regime gate is not optional.

    Long:  price at/below the lower band, RSI oversold, below VWAP.
    Short: the exact mirror at the upper band.

    Both target the nearer of band-mid / VWAP, which keeps the objective
    realistic rather than assuming a full reversion.
    """
    if ind["regime"] != "RANGE":
        return None

    price = ind["price"]

    # --- Long: fade the low -------------------------------------------------
    if (ind["bb_pct_b"] <= 0.05
            and ind["rsi"] <= config.RSI_OVERSOLD
            and price < ind["vwap"]
            and ind["htf_bias"] != "BEARISH"):     # never catch a falling knife

        target = min(ind["bb_mid"], ind["vwap"])
        if target > price:
            conf = 60
            if ind["rsi"] <= config.RSI_OVERSOLD - 5:
                conf += 10
            if ind["htf_bias"] == "BULLISH":
                conf += 10        # buying a dip inside an uptrend is the A+ case
            if ind["vol_trend"] == "High":
                conf += 5         # capitulation volume marks exhaustion
            return Signal(
                action="LONG",
                confidence=min(conf, 95),
                reason=(f"Mean-reversion long: ${price:,.2f} at lower band "
                        f"(%B={ind['bb_pct_b']:.2f}), RSI {ind['rsi']}, "
                        f"ADX {ind['adx']} confirms range. Target ${target:,.2f}."),
                setup="MEAN_REVERSION",
                target=target,
            )

    # --- Short: fade the high (the half spot could not trade) ---------------
    if (ind["bb_pct_b"] >= 0.95
            and ind["rsi"] >= config.RSI_OVERBOUGHT
            and price > ind["vwap"]
            and ind["htf_bias"] != "BULLISH"):     # never short into a rally

        target = max(ind["bb_mid"], ind["vwap"])
        if target < price:
            conf = 60
            if ind["rsi"] >= config.RSI_OVERBOUGHT + 5:
                conf += 10
            if ind["htf_bias"] == "BEARISH":
                conf += 10
            if ind["vol_trend"] == "High":
                conf += 5
            return Signal(
                action="SHORT",
                confidence=min(conf, 95),
                reason=(f"Mean-reversion short: ${price:,.2f} at upper band "
                        f"(%B={ind['bb_pct_b']:.2f}), RSI {ind['rsi']}, "
                        f"ADX {ind['adx']} confirms range. Target ${target:,.2f}."),
                setup="MEAN_REVERSION",
                target=target,
            )

    return None


# ── Setup 2: squeeze breakout ─────────────────────────────────────────────────

def _squeeze_breakout(ind: dict) -> Optional[Signal]:
    """
    Volatility expansion. Bollinger bandwidth compressed into the bottom
    quartile of its recent range means energy is coiled; the expansion that
    follows is directional and fast. This is BTC's signature move.

    Fires exactly when mean-reversion is switched off, so the two complement
    rather than compete.

    Target is a measured move: half the compressed range projected off the break.
    """
    if ind["regime"] == "RANGE" and not ind["squeeze"]:
        return None

    price = ind["price"]
    range_height = ind["recent_high"] - ind["recent_low"]
    if range_height <= 0:
        return None

    volume_confirms = ind["vol_trend"] == "High"
    if not volume_confirms:
        return None       # an unconfirmed break is usually a fakeout

    # --- Long breakout ------------------------------------------------------
    if (price >= ind["recent_high"] * 0.9995
            and ind["macd_hist"] > 0
            and ind["ema9"] > ind["ema21"]
            and ind["htf_bias"] != "BEARISH"):

        target = price + range_height * 0.5
        conf = 55
        if ind["squeeze"]:
            conf += 15    # a true squeeze release is the high-quality version
        if ind["htf_bias"] == "BULLISH":
            conf += 10
        if ind["adx"] >= config.TREND_ADX_MIN:
            conf += 5
        return Signal(
            action="LONG",
            confidence=min(conf, 95),
            reason=(f"Squeeze breakout long: ${price:,.2f} clearing "
                    f"${ind['recent_high']:,.2f} on high volume, "
                    f"squeeze={ind['squeeze']}, ADX {ind['adx']}. "
                    f"Measured target ${target:,.2f}."),
            setup="SQUEEZE_BREAKOUT",
            target=target,
        )

    # --- Short breakdown ----------------------------------------------------
    # Downside breaks on BTC tend to run faster than upside ones — liquidation
    # cascades are one-directional by construction, since leveraged longs
    # dominate open interest.
    if (price <= ind["recent_low"] * 1.0005
            and ind["macd_hist"] < 0
            and ind["ema9"] < ind["ema21"]
            and ind["htf_bias"] != "BULLISH"):

        target = price - range_height * 0.5
        conf = 55
        if ind["squeeze"]:
            conf += 15
        if ind["htf_bias"] == "BEARISH":
            conf += 10
        if ind["adx"] >= config.TREND_ADX_MIN:
            conf += 5
        return Signal(
            action="SHORT",
            confidence=min(conf, 95),
            reason=(f"Squeeze breakdown short: ${price:,.2f} losing "
                    f"${ind['recent_low']:,.2f} on high volume, "
                    f"squeeze={ind['squeeze']}, ADX {ind['adx']}. "
                    f"Measured target ${target:,.2f}."),
            setup="SQUEEZE_BREAKOUT",
            target=target,
        )

    return None


# ── Setup 3: trend pullback ───────────────────────────────────────────────────

def _trend_pullback(ind: dict) -> Optional[Signal]:
    """
    Continuation after a pullback inside an established trend. Lower conviction
    than the two above — it is the most crowded retail setup, so the edge is
    thinner — but it fills the gap when neither of the others qualifies.
    """
    if ind["regime"] != "TREND":
        return None

    price = ind["price"]

    if (ind["htf_bias"] == "BULLISH"
            and price <= ind["ema21"] * 1.001
            and price > ind["ema9"]
            and ind["rsi"] < 65):

        target = ind["recent_high"]
        if target > price:
            conf = 55 + (5 if ind["vol_trend"] != "Low" else 0) \
                      + (5 if ind["macd_hist"] > 0 else 0)
            return Signal(
                action="LONG",
                confidence=min(conf, 90),
                reason=(f"Trend pullback long: 5m bullish, ${price:,.2f} held "
                        f"EMA21 ${ind['ema21']:,.2f} and reclaimed EMA9. "
                        f"Target ${target:,.2f}."),
                setup="TREND_PULLBACK",
                target=target,
            )

    if (ind["htf_bias"] == "BEARISH"
            and price >= ind["ema21"] * 0.999
            and price < ind["ema9"]
            and ind["rsi"] > 35):

        target = ind["recent_low"]
        if target < price:
            conf = 55 + (5 if ind["vol_trend"] != "Low" else 0) \
                      + (5 if ind["macd_hist"] < 0 else 0)
            return Signal(
                action="SHORT",
                confidence=min(conf, 90),
                reason=(f"Trend pullback short: 5m bearish, ${price:,.2f} "
                        f"rejected EMA21 ${ind['ema21']:,.2f}. "
                        f"Target ${target:,.2f}."),
                setup="TREND_PULLBACK",
                target=target,
            )

    return None


# Order matters: highest-conviction setup first, first match wins.
_SETUPS = (_mean_reversion, _squeeze_breakout, _trend_pullback)


# ── Exit logic ────────────────────────────────────────────────────────────────

def _exit_signal(ind: dict, state: dict) -> Optional[Signal]:
    """
    Signal-based exits, evaluated before the hard brackets get a chance to fire.
    A scalp thesis can die before the stop is hit — when the reason for the
    trade evaporates, leave, rather than donating the difference to the stop.
    """
    setup = state.get("setup", "")
    is_long = state.get("side") == "Buy"
    price = ind["price"]

    if setup == "MEAN_REVERSION":
        # These trades exist purely to reach the mean. Once price is back at
        # VWAP/band-mid the edge is spent, whatever the P&L says.
        if is_long and price >= min(ind["bb_mid"], ind["vwap"]):
            return Signal("CLOSE", 80, "Mean reached — fade objective complete.", setup)
        if not is_long and price <= max(ind["bb_mid"], ind["vwap"]):
            return Signal("CLOSE", 80, "Mean reached — fade objective complete.", setup)
        # The range breaking is the fade thesis being invalidated outright.
        if ind["regime"] == "TREND":
            if is_long and ind["htf_bias"] == "BEARISH":
                return Signal("CLOSE", 85,
                              "Range broke into a bearish trend — long fade invalidated.",
                              setup)
            if not is_long and ind["htf_bias"] == "BULLISH":
                return Signal("CLOSE", 85,
                              "Range broke into a bullish trend — short fade invalidated.",
                              setup)

    if setup == "SQUEEZE_BREAKOUT":
        # Breakouts that lose momentum tend to fully retrace the break.
        if is_long and ind["ema9"] < ind["ema21"] and ind["macd_hist"] < 0:
            return Signal("CLOSE", 75, "Breakout momentum lost (EMA9 back under EMA21).", setup)
        if not is_long and ind["ema9"] > ind["ema21"] and ind["macd_hist"] > 0:
            return Signal("CLOSE", 75, "Breakdown momentum lost (EMA9 back over EMA21).", setup)

    if setup == "TREND_PULLBACK":
        if is_long and ind["htf_bias"] == "BEARISH":
            return Signal("CLOSE", 80, "Higher-timeframe trend flipped bearish.", setup)
        if not is_long and ind["htf_bias"] == "BULLISH":
            return Signal("CLOSE", 80, "Higher-timeframe trend flipped bullish.", setup)
        if is_long and ind["rsi"] > 75:
            return Signal("CLOSE", 70, "RSI overbought — taking the scalp.", setup)
        if not is_long and ind["rsi"] < 25:
            return Signal("CLOSE", 70, "RSI oversold — taking the scalp.", setup)

    return None


# ── Public entry point ────────────────────────────────────────────────────────

def get_signal(ind: dict, state: dict) -> Signal:
    """Evaluate all rules and return a single decision."""
    if state.get("in_position"):
        exit_sig = _exit_signal(ind, state)
        if exit_sig:
            return exit_sig
        return _hold(f"Holding {state.get('setup','position')} "
                     f"{state.get('side','')} — thesis intact, brackets managing it.")

    for setup_fn in _SETUPS:
        sig = setup_fn(ind)
        if sig:
            log.info(f"[setup] {sig.setup} {sig.action} conf={sig.confidence}")
            return sig

    return _hold(f"No setup. regime={ind['regime']} ADX={ind['adx']} "
                 f"RSI={ind['rsi']} %B={ind['bb_pct_b']:.2f} "
                 f"squeeze={ind['squeeze']} bias={ind['htf_bias']}")
