"""Expose only opaque tracking routes when running a public HTTPS tunnel."""

import os
import re

from werkzeug.serving import run_simple
from werkzeug.wrappers import Response

from app import app


def tracking_gateway(environ, start_response):
    path = environ.get("PATH_INFO", "")
    allowed = path == "/t/health" or re.fullmatch(r"/t/(?:o/[A-Za-z0-9_-]+\.gif|c/[A-Za-z0-9_-]+)", path)
    if environ.get("REQUEST_METHOD") not in {"GET", "HEAD"} or not allowed:
        return Response("Not found", status=404, mimetype="text/plain")(environ, start_response)
    return app(environ, start_response)


if __name__ == "__main__":
    run_simple("127.0.0.1", int(os.getenv("TRACKING_GATEWAY_PORT", "5001")),
               tracking_gateway, use_debugger=False, use_reloader=False, threaded=True)
