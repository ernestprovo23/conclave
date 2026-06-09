"""Direct tests for the shared async HTTP transport (the single network boundary).

The rest of the suite patches ``conclave.transport.post_json`` to stay offline,
which means the real ``post_json`` body -- status return, JSON-vs-text decoding,
and the timeout / ``HTTPError`` -> :class:`TransportError` normalization -- never
runs under test. These tests close that gap by driving the *real* ``post_json``
through an :class:`httpx.MockTransport` mounted on the pooled client, so every
byte path is exercised with no network and no API key.

Design:
* ``mock_client`` installs an ``httpx.AsyncClient`` backed by a caller-supplied
  ``handler`` into ``transport._client`` and restores the module global on
  teardown. Because that injected client is open, ``post_json`` reuses it via
  ``_get_client`` (covering the "client already live" branch) instead of building
  a default networked client.
* Handlers either return an :class:`httpx.Response` (success / non-JSON / error
  status) or ``raise`` an httpx exception (timeout / connect error) so the
  transport's own ``except`` arms run.
"""

from __future__ import annotations

import httpx
import pytest

from conclave import transport
from conclave.transport import TransportError, post_json


@pytest.fixture
async def mock_client():
    """Install a MockTransport-backed pooled client; restore the global after.

    Returns an installer ``use(handler)`` where ``handler(request) -> Response``
    (or raises an httpx exception). The created client is open, so ``post_json``
    reuses it through ``_get_client`` -- exercising the real transport end to end.
    """
    saved = transport._client
    created: list[httpx.AsyncClient] = []

    def use(handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(client)
        transport._client = client
        return client

    yield use

    # Close anything we created, then put the module global back as we found it.
    for client in created:
        if not client.is_closed:
            await client.aclose()
    transport._client = saved


async def test_post_json_success_returns_status_and_decoded_json(mock_client):
    """A 200 with a JSON body comes back as ``(200, dict)`` decoded by httpx."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    mock_client(handler)
    status, body = await post_json(
        "https://api.example.test/v1/chat",
        {"Authorization": "Bearer FAKE-NOT-A-REAL-KEY"},
        {"model": "x", "messages": []},
        timeout=30.0,
    )

    assert status == 200
    assert body == {"choices": [{"message": {"content": "ok"}}]}
    # The real request the adapter would have built actually reached the transport.
    assert seen["url"] == "https://api.example.test/v1/chat"
    assert seen["auth"] == "Bearer FAKE-NOT-A-REAL-KEY"
    assert b'"model":"x"' in seen["body"].replace(b" ", b"")


async def test_post_json_error_status_with_json_body_is_returned_not_raised(mock_client):
    """A 4xx/5xx JSON body is returned verbatim -- the transport never raises on status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    mock_client(handler)
    status, body = await post_json(
        "https://api.example.test/v1/chat",
        {"Authorization": "Bearer FAKE"},
        {"model": "x"},
        timeout=30.0,
    )

    assert status == 401
    assert body == {"error": {"message": "invalid api key"}}


async def test_post_json_non_json_body_falls_back_to_text(mock_client):
    """A non-JSON body (e.g. an HTML 502 page) decodes to the raw text fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html><body>Bad Gateway</body></html>")

    mock_client(handler)
    status, body = await post_json(
        "https://api.example.test/v1/chat",
        {},
        {"model": "x"},
        timeout=30.0,
    )

    assert status == 502
    assert body == "<html><body>Bad Gateway</body></html>"
    assert isinstance(body, str)


async def test_post_json_timeout_becomes_transport_error(mock_client):
    """An httpx timeout maps to TransportError naming the timeout, not the headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    mock_client(handler)
    with pytest.raises(TransportError) as excinfo:
        await post_json(
            "https://api.example.test/v1/chat",
            {"Authorization": "Bearer FAKE-SECRET-VALUE"},
            {"model": "x"},
            timeout=5.0,
        )

    msg = str(excinfo.value)
    assert "timed out" in msg
    assert "5s" in msg  # the timeout value, formatted as in transport.py
    # The header value must never leak into the normalized error message.
    assert "FAKE-SECRET-VALUE" not in msg


async def test_post_json_connect_error_becomes_transport_error(mock_client):
    """A generic httpx.HTTPError (connect failure) maps to a leak-free TransportError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_client(handler)
    with pytest.raises(TransportError) as excinfo:
        await post_json(
            "https://api.example.test/v1/chat",
            {"Authorization": "Bearer FAKE-SECRET-VALUE"},
            {"model": "x"},
            timeout=30.0,
        )

    msg = str(excinfo.value)
    # Message names the failure *kind* (class name) only -- never str(exc), never the key.
    assert "network error" in msg
    assert "ConnectError" in msg
    assert "FAKE-SECRET-VALUE" not in msg
    assert "connection refused" not in msg


async def test_post_json_reuses_open_pooled_client(mock_client):
    """Two calls in a row reuse the same open client (pooling) -- no rebuild."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    client = mock_client(handler)
    s1, b1 = await post_json("https://api.example.test/a", {}, {}, timeout=10.0)
    # The client the second call resolves must be the very same object.
    assert transport._get_client() is client
    s2, b2 = await post_json("https://api.example.test/b", {}, {}, timeout=10.0)

    assert (s1, s2) == (200, 200)
    assert b1 == {"n": 1}
    assert b2 == {"n": 2}
    assert calls["n"] == 2


async def test_get_client_rebuilds_after_close():
    """_get_client creates a fresh client when the cached one was closed."""
    # Force a clean slate, then create -> close -> ensure a new instance is built.
    await transport.aclose()
    first = transport._get_client()
    assert not first.is_closed
    await first.aclose()
    assert first.is_closed

    second = transport._get_client()
    assert second is not first
    assert not second.is_closed
    await transport.aclose()


async def test_aclose_is_idempotent():
    """Calling aclose twice (and on a fresh None client) is safe and resets state."""
    transport._get_client()  # ensure a live client exists
    await transport.aclose()
    assert transport._client is None
    # Second close with the global already None must not raise.
    await transport.aclose()
    assert transport._client is None
