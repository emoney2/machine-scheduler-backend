"""
Gunicorn config for Render (or any host). Use:

    gunicorn -c gunicorn.conf.py server:app

Override timeouts with env: GUNICORN_TIMEOUT, GUNICORN_GRACEFUL_TIMEOUT.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
worker_class = "eventlet"
workers = 1
# Default raised from 120: large Sheets batchGet + lock contention can exceed 120s
# without the worker being "broken"; low value caused WORKER TIMEOUT + SIGKILL loops.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "120"))


def post_worker_init(worker):
    """Background watchers that keep running while the Render web service is up."""
    import logging

    log = logging.getLogger("gunicorn.error")
    try:
        import wholesale_signup_notify

        wholesale_signup_notify.start_background_poller()
    except Exception:
        log.exception("wholesale signup notifier failed to start")
    try:
        import digitizing_inbox

        digitizing_inbox.start_background_poller()
    except Exception:
        log.exception("digitizing inbox poller failed to start")
