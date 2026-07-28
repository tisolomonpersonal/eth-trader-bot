"""
Bot loop for the Directional Candle Strategy.

Cycle timing:
  - When idle (no position, no pending signal): run every 60 s
    (H1 candle closes happen once per hour; no need to poll faster)
  - When a pending signal is armed: run every 30 s
    (need to catch the M5 fib retracement promptly)
  - When in a position: run every 30 s (SL/TP monitoring)

Signal handling is NOT done here — gunicorn owns the process signals.
The bot thread runs as a daemon inside the gunicorn worker.
"""
import time
import traceback
from datetime import datetime, timezone

import bybit_client
import indicators as ind_calc
import strategy
import telegram_bot as tg
import config
from logger import get_logger

log = get_logger("scheduler")

_running = True


def stop():
    """Called by gunicorn worker_exit hook to request a clean shutdown."""
    global _running
    _running = False
    tradfi_stop()


def run_bot() -> None:
    """Main bot loop — runs inside the gunicorn worker thread."""
    log.info(
        f"BTC Directional Candle Bot starting | "
        f"symbol={config.SYMBOL} category={config.CATEGORY} "
        f"leverage={config.LEVERAGE}× qty={config.BTC_QTY} BTC | "
        f"paper={config.PAPER_MODE} testnet={config.BYBIT_TESTNET}"
    )

    tg.alert_started()

    state        = strategy.load_state()
    last_hour    = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    error_streak = 0

    while _running:
        cycle_start = time.time()

        try:
            state = strategy.reset_daily_if_needed(state)
            state = strategy.run_cycle(state)
            strategy.save_state(state)
            error_streak = 0

        except Exception as e:
            error_streak += 1
            err_msg = str(e)[:300]
            log.error(f"Cycle error (streak={error_streak}): {e}\n{traceback.format_exc()}")

            if error_streak <= 3:
                tg.alert_api_error("run_cycle", err_msg)
            elif error_streak == 4:
                tg.alert_critical(
                    f"Bot has failed {error_streak} consecutive cycles.\n"
                    f"Last error: {err_msg}\nCheck logs urgently."
                )

            backoff = min(30 * (2 ** (error_streak - 1)), 300)
            log.info(f"Retrying in {backoff}s")
            time.sleep(backoff)
            continue

        # Hourly summary
        now = datetime.now(timezone.utc)
        if (now - last_hour).total_seconds() >= 3600:
            try:
                df  = bybit_client.get_klines_h1()
                ind = ind_calc.calculate(df)
                bal = bybit_client.get_balance()
                tg.send_hourly_summary(state, ind, bal)
                last_hour = now.replace(minute=0, second=0, microsecond=0)
            except Exception as e:
                log.error(f"Hourly summary error: {e}")

        # Dynamic sleep:
        #   - Active (position open or pending signal) → 30 s
        #   - Idle (watching for H1 signal)            → 60 s
        active   = state.get("in_position") or bool(state.get("pending_signal"))
        interval = 30 if active else 60
        elapsed  = time.time() - cycle_start
        sleep_s  = max(5, interval - elapsed)
        log.debug(f"Cycle done in {elapsed:.1f}s — sleeping {sleep_s:.0f}s "
                  f"({'active' if active else 'idle'})")
        time.sleep(sleep_s)

    log.info("Bot loop exited cleanly")


# ── TradFi loop (fully independent) ──────────────────────────────────────────

_tradfi_running = True


def tradfi_stop():
    global _tradfi_running
    _tradfi_running = False


def run_tradfi_bot() -> None:
    """TradFi bot loop — runs on its own cycle interval, in its own thread."""
    import tradfi_client
    import tradfi_strategy

    symbol = config.TRADFI_SYMBOL
    log.info(
        f"TradFi Bot starting | symbol={symbol} | "
        f"paper={config.TRADFI_PAPER} testnet={config.BYBIT_TESTNET}"
    )

    tg.alert_tradfi_started(symbol)

    state        = tradfi_strategy.load_state()
    last_hour    = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    error_streak = 0

    while _tradfi_running:
        cycle_start = time.time()

        try:
            state = tradfi_strategy.reset_daily_if_needed(state)
            state = tradfi_strategy.run_cycle(state)
            tradfi_strategy.save_state(state)
            error_streak = 0

        except Exception as e:
            error_streak += 1
            err_msg = str(e)[:300]
            log.error(
                f"TradFi cycle error (streak={error_streak}): {e}\n"
                f"{traceback.format_exc()}"
            )

            if error_streak <= 3:
                tg.alert_api_error("tradfi_run_cycle", err_msg)
            elif error_streak == 4:
                tg.alert_critical(
                    f"TradFi bot has failed {error_streak} consecutive cycles.\n"
                    f"Last error: {err_msg}\nCheck logs urgently."
                )

            backoff = min(30 * (2 ** (error_streak - 1)), 300)
            log.info(f"TradFi retrying in {backoff}s")
            time.sleep(backoff)
            continue

        # Hourly summary
        now = datetime.now(timezone.utc)
        if (now - last_hour).total_seconds() >= 3600:
            try:
                market_open = tradfi_client.is_market_open(state.get("symbol", symbol))
                df  = tradfi_client.get_klines(state.get("symbol", symbol))
                if not df.empty:
                    ind = ind_calc.calculate(df)
                    bal = tradfi_client.get_balance()
                    tg.send_tradfi_hourly_summary(state, ind, bal, market_open)
                last_hour = now.replace(minute=0, second=0, microsecond=0)
            except Exception as e:
                log.error(f"TradFi hourly summary error: {e}")

        elapsed = time.time() - cycle_start
        time.sleep(max(10, config.TRADFI_CYCLE_SECONDS - elapsed))

    log.info("TradFi bot loop exited cleanly")
