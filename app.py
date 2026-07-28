"""
Root-level shim for Zeabur auto-detection.

Zeabur ignores Dockerfile/Procfile/zeabur.yaml and auto-generates:
  gunicorn app:app   (run from /app, the repo root)

The real bot lives in eth-trader-bot/. This shim:
  1. Changes CWD to eth-trader-bot/ so all relative file ops work
  2. Inserts eth-trader-bot/ into sys.path so module imports resolve
  3. Loads the real app.py via importlib (avoids circular import)
  4. Re-exports `app` so gunicorn finds it as app:app
"""
import sys
import os
import importlib.util

_here = os.path.dirname(os.path.abspath(__file__))
_bot_dir = os.path.join(_here, 'eth-trader-bot')

# Change working directory so open(), logging paths, /data writes, etc. work
os.chdir(_bot_dir)

# Prepend bot dir to sys.path so `import config`, `import strategy`, etc. resolve
sys.path.insert(0, _bot_dir)

# Load eth-trader-bot/app.py without triggering a circular import on this file
_spec = importlib.util.spec_from_file_location(
    'bot_app', os.path.join(_bot_dir, 'app.py')
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules['bot_app'] = _mod
_spec.loader.exec_module(_mod)

# Expose Flask app — gunicorn discovers it as `app:app`
app = _mod.app
