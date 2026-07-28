"""
Main bot loop: 1-minute trading cycles + hourly Telegram summary.
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


def _warn_if_legacy_position_orphaned() -> None:
    """
    Switching to SCALP_MODE points the bot at a different symbol AND a different
    state file. Any position the old swing bot still had open is therefore left
    with nothing watching its stop-loss — it simply stops being managed, silently.

    This warns loudly rather than refusing to start: blocking startup would
    leave the position equally unmanaged while also taking the bot down. A human
    has to close it (or move it) manually.
    """
    import json
    try:
        if not config.STATE_FILE.exists():
            return
        legacy = json.loads(config.STATE_FILE.read_text())
        if not legacy.get("in_position"):
            return

        msg = (
            f"ORPHANED SPOT POSITION: the swing bot's state file still shows an "
            f"open position — {legacy.get('qty')} @ ${legacy.get('entry_price')}, "
            f"SL ${legacy.get('sl_price')} / TP ${legacy.get('tp_price')}.\n\n"
            f"SCALP_MODE is now active, which trades the {config.SYMBOL} "
            f"PERPETUAL off a separate state file. That is a different market — "
            f"nothing is monitoring the old spot position's stop-loss any more.\n\n"
            f"Sell it manually on Bybit, or set SCALP_MODE=false to hand it back "
            f"to the swing bot."
        )
        log.critical(msg)
        try:
            tg.alert_critical(msg)
        except Exception:
            pass
    except Exception as e:
        log.error(f"Could not check legacy state for an orphaned position: {e}")


def _check_account_viable() -> None:
    """
    Can this account open the smallest position the exchange allows?

    BTCUSDT perp has a 0.001 BTC minimum. At a six-figure BTC price that is
    ~$100+ of notional, so at 1x leverage the account needs ~$100 of margin
    just to place one trade. A small float cannot meet that without raising
    leverage — which is a real decision about liquidation risk, not a detail.

    Checked loudly at startup so it surfaces as a clear message rather than an
    endless stream of rejected orders.
    """
    import perp_client
    try:
        price = float(perp_client.get_klines(limit=1)["close"].iloc[-1])
        balance = perp_client.get_balance()
        ok, detail = perp_client.check_account_viable(balance.get("usdt", 0), price)

        if ok:
            log.info(f"Account viability: {detail}")
            return

        min_q = perp_client.min_qty()
        needed_lev = (min_q * price) / max(balance.get("usdt", 0), 0.01)
        msg = (
            f"ACCOUNT TOO SMALL TO TRADE {config.SYMBOL} PERP.\n\n{detail}\n\n"
            f"To open even the minimum position with this balance you would "
            f"need roughly {needed_lev:.1f}x leverage (currently "
            f"{config.LEVERAGE:g}x). At that leverage a ~{100/max(needed_lev,1):.1f}% "
            f"adverse move liquidates the account, and BTC moves that much "
            f"intraday regularly.\n\n"
            f"Options: fund the account to about "
            f"${min_q * price:,.0f} to trade at 1x, or trade a smaller-minimum "
            f"instrument. Raising leverage to force a position through is not "
            f"a fix — it converts a sizing problem into a liquidation problem."
        )
        log.critical(msg)
        try:
            tg.alert_critical(msg)
        except Exception:
            pass
    except Exception as e:
        log.error(f"Could not run the account viability check: {e}")


def run_bot() -> None:
    """Main bot loop — runs inside the gunicorn worker thread."""
    # SCALP_MODE swaps the whole signal/exit engine. The original AI-led swing
    # path stays reachable with SCALP_MODE=false.
    if config.SCALP_MODE:
        import scalp_strategy
        engine = scalp_strategy
        _warn_if_legacy_position_orphaned()
        _check_account_viable()
        cycle_seconds = config.SCALP_CYCLE_SECONDS
        min_sleep = 5
        log.info(f"{config.SYMBOL} SCALP bot starting | cycle={cycle_seconds}s | "
                 f"paper={config.PAPER_MODE} | testnet={config.BYBIT_TESTNET} | "
                 f"round-trip fees={config.ROUND_TRIP_FEE_PCT:.2f}% | "
                 f"min TP={config.ROUND_TRIP_FEE_PCT * config.MIN_EDGE_FEE_MULT:.2f}%")
    else:
        engine = strategy
        cycle_seconds = 60
        min_sleep = 5
        log.info(f"{config.SYMBOL} swing bot starting | paper={config.PAPER_MODE} | "
                 f"testnet={config.BYBIT_TESTNET}")

    tg.alert_started()

    state        = engine.load_state()
    last_hour    = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    error_streak = 0

    while _running:
        cycle_start = time.time()

        try:
            state = engine.reset_daily_if_needed(state)
            state = engine.run_cycle(state)
            engine.save_state(state)
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
                    f"Last error: {err_msg}\nCheck Zeabur logs urgently."
                )

            # Exponential back-off: 30s → 60s → 120s → cap at 300s
            backoff = min(30 * (2 ** (error_streak - 1)), 300)
            log.info(f"Retrying in {backoff}s")
            time.sleep(backoff)
            continue

        # Hourly summary
        now = datetime.now(timezone.utc)
        if (now - last_hour).total_seconds() >= 3600:
            try:
                # Report against the market actually being traded — spot for
                # the swing path, the perp for the scalper.
                if config.SCALP_MODE:
                    import perp_client
                    client = perp_client
                else:
                    client = bybit_client
                df  = client.get_klines()
                ind = ind_calc.calculate(df)
                bal = client.get_balance()
                tg.send_hourly_summary(state, ind, bal)
                last_hour = now.replace(minute=0, second=0, microsecond=0)
            except Exception as e:
                log.error(f"Hourly summary error: {e}")

        # Sleep the remainder of the cycle
        elapsed = time.time() - cycle_start
        time.sleep(max(min_sleep, cycle_seconds - elapsed))

    log.info("Bot loop exited cleanly")


# ── TradFi loop (fully independent — own state, own thread, own errors) ───────
# Only runs when config.TRADFI_ENABLED=true. A crash or repeated failure here
# NEVER touches the BNB spot loop above, and vice versa.

_tradfi_running = True


def tradfi_stop():
    global _tradfi_running
    _tradfi_running = False


def run_tradfi_bot() -> None:
    """TradFi bot loop — runs on its own cycle interval, in its own thread."""
    import tradfi_client
    import tradfi_strategy

    symbol = config.TRADFI_SYMBOL
    log.info(f"TradFi Bot starting | symbol={symbol} | paper={config.PAPER_MODE} | "
             f"testnet={config.BYBIT_TESTNET}")

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
            log.error(f"TradFi cycle error (streak={error_streak}): {e}\n{traceback.format_exc()}")

            if error_streak <= 3:
                tg.alert_api_error("tradfi_run_cycle", err_msg)
            elif error_streak == 4:
                tg.alert_critical(
                    f"TradFi bot has failed {error_streak} consecutive cycles.\n"
                    f"Last error: {err_msg}\nCheck Zeabur logs urgently."
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
