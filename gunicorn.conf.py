"""
Gunicorn configuration for BNB Spot Bot.
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
    """Start bot thread after the worker process is forked (not in master)."""
    import threading
    from scheduler import run_bot
    t = threading.Thread(target=run_bot, daemon=True, name="bnb-bot")
    t.start()
    server.log.info("BNB bot thread started in worker")


def worker_exit(server, worker):
    """Notify Telegram when the worker exits (restart / shutdown)."""
    try:
        import scheduler
        scheduler.stop()           # signal the bot loop to exit
        from telegram_bot import alert_stopped
        alert_stopped()
    except Exception as e:
        server.log.warning(f"worker_exit hook error: {e}")
