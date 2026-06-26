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


def run_bot() -> None:
    """Main bot loop — runs inside the gunicorn worker thread."""
    log.info(f"BNB/USDT Spot Bot starting | paper={config.PAPER_MODE} | "
             f"testnet={config.BYBIT_TESTNET}")

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
                df  = bybit_client.get_klines()
                ind = ind_calc.calculate(df)
                bal = bybit_client.get_balance()
                tg.send_hourly_summary(state, ind, bal)
                last_hour = now.replace(minute=0, second=0, microsecond=0)
            except Exception as e:
                log.error(f"Hourly summary error: {e}")

        # Sleep the remainder of the 60-second cycle
        elapsed = time.time() - cycle_start
        time.sleep(max(5, 60 - elapsed))

    log.info("Bot loop exited cleanly")
