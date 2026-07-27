from __future__ import annotations

import socket

import pytest
import requests
import responses

from anistream.utils.http import HttpClient


def test_sync_http_client_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(requests.exceptions.InvalidURL):
        HttpClient.validate_public_url("https://internal.example/secret")


def test_sync_http_client_pins_the_validated_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls: list[str] = []
    connected: list[tuple[object, ...]] = []

    def fake_getaddrinfo(host: str, port: int, **kwargs: object):
        dns_calls.append(host)
        address = "93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    class FakeSocket:
        def settimeout(self, value: object) -> None:
            return None

        def setsockopt(self, *args: object) -> None:
            return None

        def bind(self, address: tuple[object, ...]) -> None:
            return None

        def connect(self, address: tuple[object, ...]) -> None:
            connected.append(address)

        def close(self) -> None:
            return None

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: FakeSocket())

    client = HttpClient()
    session = client._session()
    request = requests.Request("GET", "http://rebind.example/").prepare()
    adapter = session.get_adapter(request.url)
    pool = adapter.get_connection_with_tls_context(
        request,
        True,
        proxies={},
        cert=None,
    )
    connection = pool._new_conn()
    sock = connection._new_conn()

    assert dns_calls == ["rebind.example"]
    assert connected == [("93.184.216.34", 80)]
    sock.close()


def test_sync_http_client_rejects_private_dns_on_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 80),
            )
        ],
    )
    client = HttpClient()
    session = client._session()
    request = requests.Request("GET", "http://metadata.example/").prepare()
    adapter = session.get_adapter(request.url)

    with pytest.raises(requests.exceptions.InvalidURL):
        adapter.get_connection_with_tls_context(
            request,
            True,
            proxies={},
            cert=None,
        )


def test_sync_http_client_falls_back_only_to_other_validated_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 80),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.35", 80),
            ),
        ],
    )

    class FakeSocket:
        def settimeout(self, value: object) -> None:
            return None

        def setsockopt(self, *args: object) -> None:
            return None

        def bind(self, address: tuple[object, ...]) -> None:
            return None

        def connect(self, address: tuple[object, ...]) -> None:
            connected.append(address)
            if len(connected) == 1:
                raise OSError("first address unavailable")

        def close(self) -> None:
            return None

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: FakeSocket())
    client = HttpClient()
    session = client._session()
    request = requests.Request("GET", "http://media.example/").prepare()
    adapter = session.get_adapter(request.url)
    pool = adapter.get_connection_with_tls_context(
        request,
        True,
        proxies={},
        cert=None,
    )
    connection = pool._new_conn()
    sock = connection._new_conn()

    assert connected == [
        ("93.184.216.34", 80),
        ("93.184.216.35", 80),
    ]
    sock.close()


def test_sync_http_client_preserves_hostname_for_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ],
    )
    client = HttpClient()
    session = client._session()
    request = requests.Request("GET", "https://media.example/video.mp4").prepare()
    adapter = session.get_adapter(request.url)
    pool = adapter.get_connection_with_tls_context(
        request,
        True,
        proxies={},
        cert=None,
    )
    connection = pool._new_conn()

    assert pool.host == "media.example"
    assert connection.host == "media.example"
    assert connection._resolved_addresses == ("93.184.216.34",)


@responses.activate
def test_sync_http_client_limits_provider_response_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses.add(
        responses.GET,
        "https://provider.example/catalogue",
        body=b"x" * (70 * 1024),
        status=200,
    )
    client = HttpClient(max_response_bytes=64 * 1024)
    with pytest.raises(requests.exceptions.RequestException, match="size limit"):
        client.get("https://provider.example/catalogue")


@responses.activate
def test_sync_http_client_limits_terminal_redirect_response_size() -> None:
    responses.add(
        responses.GET,
        "https://provider.example/redirect",
        body=b"x" * (70 * 1024),
        status=302,
        headers={"Location": "https://provider.example/next"},
    )
    client = HttpClient(max_response_bytes=64 * 1024, max_redirects=0)

    with pytest.raises(requests.exceptions.RequestException, match="size limit"):
        client.get("https://provider.example/redirect")


@responses.activate
def test_sync_http_client_preserves_small_terminal_redirect_response() -> None:
    responses.add(
        responses.GET,
        "https://provider.example/redirect",
        body=b"redirect unavailable",
        status=302,
    )
    client = HttpClient(max_response_bytes=64 * 1024)

    response = client.get("https://provider.example/redirect")

    assert response.status_code == 302
    assert response.content == b"redirect unavailable"
