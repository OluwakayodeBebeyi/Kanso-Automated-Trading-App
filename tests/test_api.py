from datetime import datetime, timezone
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from app.api import create_server
from app.database import Store


def test_local_api_auth_cors_and_no_fabricated_marks(tmp_path):
    store = Store(str(tmp_path / "api.sqlite3"), 900)
    store.trade(
        symbol="XYZ",
        side="BUY",
        quantity=1,
        price=10,
        fee_rate=0,
        max_order=100,
        max_position=100,
        max_exposure=100,
        prices={"XYZ": 10},
        idempotency_key="api-seed",
        price_timestamp=datetime.now(timezone.utc),
    )
    server = create_server(store, "x" * 40, "chrome-extension://approved-id", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    root = f"http://{host}:{port}"
    try:
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(root + "/api/portfolio")
        assert unauthorized.value.code == 401
        request = Request(
            root + "/api/portfolio",
            headers={
                "Origin": "chrome-extension://approved-id",
                "Authorization": "Bearer " + "x" * 40,
            },
        )
        with urlopen(request) as response:
            payload = response.read().decode()
            assert response.status == 200
            assert (
                response.headers["Access-Control-Allow-Origin"]
                == "chrome-extension://approved-id"
            )
        assert '"mode": "PAPER"' in payload and '"equity": null' in payload
        denied_origin = Request(
            root + "/api/portfolio",
            headers={
                "Origin": "chrome-extension://other",
                "Authorization": "Bearer " + "x" * 40,
            },
        )
        with pytest.raises(HTTPError) as forbidden:
            urlopen(denied_origin)
        assert forbidden.value.code == 403
        assert '"market_price": null' in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_api_refuses_short_tokens_and_has_no_order_endpoint():
    from app.api import LocalAPIError, validate_api_token

    with pytest.raises(LocalAPIError, match="32 characters"):
        validate_api_token("short")
    store = Store(":memory:", 1000)
    server = create_server(store, "z" * 40, None, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        request = Request(
            f"http://{host}:{port}/api/orders",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer " + "z" * 40},
        )
        with pytest.raises(HTTPError) as response:
            urlopen(request)
        assert response.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
