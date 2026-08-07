"""
Bot loop for the 4H Bollinger Band Short Strategy.

Cycle timing:
  - When idle (no position): run every 5 minutes (300 s).
    4H candles close every 240 minutes; polling every 5 min is more than enough
    to catch a new setup within one cycle of its candle closing.
  - When in a position: run every 60 s.
    Monitors the moving MA28 take-profit and fixed stop-loss.

Signal handling is NOT done here — gunicorn owns process signals.
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
    grid_stop()


def run_bot() -> None:
    """Main bot loop — runs inside the gunicorn worker thread."""
    log.info(
        f"4H BB Short Bot starting | "
        f"symbol={config.SYMBOL} category={config.CATEGORY} "
        f"leverage={config.LEVERAGE}× qty={config.BTC_QTY} BTC | "
        f"BB({config.BB_PERIOD},{config.BB_STD}) MA{config.MA_SHORT}/MA{config.MA_LONG} | "
        f"ATR cap={config.ATR_CAP_MULT}× | "
        f"paper={config.PAPER_MODE} testnet={config.BYBIT_TESTNET}"
    )

    tg.alert_started()

    state        = strategy.load_state()
    state        = strategy.reconcile_position_on_startup(state)
    strategy.save_state(state)
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
                df  = bybit_client.get_klines_h4()
                ind = ind_calc.calculate(df)
                bal = bybit_client.get_balance()
                tg.send_hourly_summary(state, ind, bal)
                last_hour = now.replace(minute=0, second=0, microsecond=0)
            except Exception as e:
                log.error(f"Hourly summary error: {e}")

        # Dynamic sleep:
        #   - In position → 60 s  (monitor SL/TP and moving MA28 more frequently)
        #   - Idle        → 300 s (4H candles close every 240 min; 5 min polling is plenty)
        in_position = state.get("in_position", False)
        interval    = 60 if in_position else 300
        elapsed     = time.time() - cycle_start
        sleep_s     = max(5, interval - elapsed)
        log.debug(
            f"Cycle done in {elapsed:.1f}s — sleeping {sleep_s:.0f}s "
            f"({'in position' if in_position else 'idle'})"
        )
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


# ── Grid loop (BTC perp hedged grid, fully independent) ──────────────────────

_grid_running = True


def grid_stop():
    """
    Stop the grid loop and pull its resting orders.

    Cancelling here is deliberate: on a redeploy or restart the loop thread is
    a daemon and may be killed before it can clean up after itself, which would
    leave leveraged limit orders resting on the book with nothing supervising
    them. This runs synchronously from the worker_exit hook instead.
    """
    global _grid_running
    _grid_running = False

    import grid_config as gc
    if not gc.GRID_ENABLED:
        return
    try:
        import grid_client
        grid_client.cancel_all_ours()
    except Exception as e:
        log.error(f"Could not cancel grid orders on shutdown: {e}")


def run_grid_bot() -> None:
    """Hedged grid loop — runs on its own interval, in its own thread."""
    import grid_config as gc
    import grid_strategy

    state = grid_strategy.load_state()

    try:
        state = grid_strategy.startup(state)
        grid_strategy.save_state(state)
    except Exception as e:
        log.error(f"Grid startup failed, loop not starting: {e}")
        tg.alert_critical(f"Grid bot failed to start: {str(e)[:300]}")
        return

    error_streak = 0

    while _grid_running:
        cycle_start = time.time()

        try:
            state = grid_strategy.reset_daily_if_needed(state)
            state = grid_strategy.run_cycle(state)
            grid_strategy.save_state(state)
            error_streak = 0

        except Exception as e:
            error_streak += 1
            err_msg = str(e)[:300]
            log.error(
                f"Grid cycle error (streak={error_streak}): {e}\n"
                f"{traceback.format_exc()}"
            )
            state["last_error"] = err_msg
            grid_strategy.save_state(state)

            if error_streak <= 3:
                tg.alert_api_error("grid_run_cycle", err_msg)
            elif error_streak == 4:
                # Repeated failures with resting orders and live leverage is
                # the dangerous case — pull the book rather than leave it.
                tg.alert_critical(
                    f"Grid bot failed {error_streak} consecutive cycles.\n"
                    f"Last error: {err_msg}\nCancelling resting grid orders."
                )
                try:
                    import grid_client
                    grid_client.cancel_all_ours()
                except Exception as ce:
                    log.error(f"Emergency cancel failed: {ce}")

            backoff = min(30 * (2 ** (error_streak - 1)), 300)
            log.info(f"Grid retrying in {backoff}s")
            time.sleep(backoff)
            continue

        elapsed = time.time() - cycle_start
        time.sleep(max(5, gc.GRID_CYCLE_SECONDS - elapsed))

    log.info("Grid bot loop exited cleanly")
