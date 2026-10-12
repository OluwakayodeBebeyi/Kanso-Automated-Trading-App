"""Read-only loopback API for the development browser extension."""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from app.config import Settings
from app.database import Store


class LocalAPIError(ValueError):
    pass


def validate_api_token(token):
    if not isinstance(token, str) or len(token) < 32:
        raise LocalAPIError("LOCAL_API_TOKEN must contain at least 32 characters")
    return token


def make_handler(store, token, extension_origin=None):
    token = validate_api_token(token)

    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperTraderLocalAPI/1.0"

        def _json(self, status, payload):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            if (
                origin
                and extension_origin
                and hmac.compare_digest(origin, extension_origin)
            ):
                self.send_header("Access-Control-Allow-Origin", extension_origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def _origin_allowed(self):
            origin = self.headers.get("Origin")
            return not origin or (
                extension_origin is not None
                and hmac.compare_digest(origin, extension_origin)
            )

        def _authorized(self):
            supplied = self.headers.get("Authorization", "")
            prefix = "Bearer "
            return supplied.startswith(prefix) and hmac.compare_digest(
                supplied[len(prefix) :], token
            )

        def do_OPTIONS(self):
            if not self._origin_allowed():
                self._json(403, {"error": "origin_not_allowed"})
                return
            self.send_response(204)
            if self.headers.get("Origin") and extension_origin:
                self.send_header("Access-Control-Allow-Origin", extension_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers", "Authorization, Content-Type"
                )
                self.send_header("Vary", "Origin")
            self.end_headers()

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/health":
                self._json(200, {"status": "ok", "mode": "PAPER"})
                return
            if route != "/api/portfolio":
                self._json(404, {"error": "not_found"})
                return
            if not self._origin_allowed():
                self._json(403, {"error": "origin_not_allowed"})
                return
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            portfolio = store.portfolio()
            self._json(
                200,
                {
                    "mode": "PAPER",
                    "cash": str(portfolio["cash"]),
                    "equity": str(portfolio["equity"])
                    if portfolio["equity"] is not None
                    else None,
                    "positions": [
                        {
                            "symbol": position["symbol"],
                            "quantity": str(position["quantity"]),
                            "average_cost": str(position["avg_cost"]),
                            "market_price": None,
                            "market_value": None,
                        }
                        for position in portfolio["positions"]
                    ],
                    "valuation_note": "Market values are unavailable until fresh marks are supplied.",
                },
            )

        def do_POST(self):
            self._json(405, {"error": "method_not_allowed"})

        def log_message(self, format, *args):
            # Never log headers, bearer tokens, or account data.
            return

    return Handler


def create_server(store, token, extension_origin=None, port=8766):
    return ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(store, token, extension_origin)
    )


def main():
    from app.api import validate_api_token

    settings = Settings.from_env()
    token = validate_api_token(os.getenv("LOCAL_API_TOKEN"))
    origin = os.getenv("EXTENSION_ORIGIN") or None
    store = Store(settings.database_path, settings.initial_cash)
    server = create_server(
        store, token, origin, int(os.getenv("LOCAL_API_PORT", "8766"))
    )
    print("Read-only PAPER API bound to 127.0.0.1")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
