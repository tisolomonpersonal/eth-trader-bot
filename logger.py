"""Shared logging setup — import and call get_logger(name) in every module."""
import logging
import os
import sys

# Logs go to STDERR so a tool's actual output on stdout stays machine-readable —
# `backtest.py --json` piped to a file should be valid JSON, not JSON buried in
# log lines. Zeabur captures both streams, so nothing is lost in deployment.
_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(h)
        logger.setLevel(_LEVEL)
    return logger
