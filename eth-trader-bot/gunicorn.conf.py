"""
Gunicorn configuration for BTC Directional Candle Bot.
- 1 worker: prevents multiple simultaneous bot instances
- post_fork: starts the bot trading thread inside the worker
- worker_exit: sends Telegram alert when the worker shuts down
"""
import os

workers    = 1
threads    = 2
bind       = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
timeout    = 120
loglevel   = "info"
accesslog  = "-"
errorlog   = "-"


def post_fork(server, worker):
    """Start bot thread(s) after the worker process is forked (not in master)."""
    import threading
    import config
    from scheduler import run_bot, run_grid_bot

    if config.BB_ENABLED:
        t = threading.Thread(target=run_bot, daemon=True, name="btc-bot")
        t.start()
        server.log.info("BB short bot thread started in worker")
    else:
        server.log.info("BB_ENABLED=false — BB short thread not started (grid owns the symbol)")

    # Hedged BTC grid — same pattern, own master switch.
    import grid_config as gc
    if gc.GRID_ENABLED:
        gt = threading.Thread(target=run_grid_bot, daemon=True, name="grid-bot")
        gt.start()
        server.log.info(
            f"Grid bot thread started in worker "
            f"(symbol={gc.GRID_SYMBOL} qty={gc.GRID_QTY} x{gc.GRID_LEVERAGE})"
        )
    else:
        server.log.info("GRID_ENABLED=false — grid bot thread not started")


def worker_exit(server, worker):
    """Notify Telegram when the worker exits (restart / shutdown)."""
    try:
        import scheduler
        scheduler.stop()           # signal the bot loop to exit
        from telegram_bot import alert_stopped
        alert_stopped()
    except Exception as e:
        server.log.warning(f"worker_exit hook error: {e}")
