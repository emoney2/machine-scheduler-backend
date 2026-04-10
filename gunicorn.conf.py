# Gunicorn config for production (e.g. Render).
# Start with:  gunicorn -c gunicorn.conf.py server:app
#
# Flask-SocketIO + eventlet: single worker only (see Flask-SocketIO deployment docs).
# Default gunicorn timeout is 30s; /api/process-shipment (UPS + QBO + Sheets + Drive + PDF)
# often needs longer or the proxy returns 502 and the browser shows a fake "CORS" error.

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
worker_class = "eventlet"

timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# Log timeouts clearly in Render logs
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
