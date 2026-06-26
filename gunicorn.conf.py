"""
Gunicorn configuration.
Uses 1 worker (to prevent multiple bot instances) and starts the
bot trading thread in the post_fork hook so it runs inside the worker.
"""
import os

workers    = 1
threads    = 2
bind       = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
timeout    = 120
loglevel   = "info"
accesslog  = "-"
errorlog   = "-"


def post_fork(server, worker):
    """Start the bot thread after the worker process is forked."""
    import threading
    from scheduler import run_bot
    t = threading.Thread(target=run_bot, daemon=True, name="bnb-bot")
    t.start()
    server.log.info("BNB bot thread started in worker")
