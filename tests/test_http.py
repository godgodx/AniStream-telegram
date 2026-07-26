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


@responses.activate
def test_sync_http_client_limits_provider_response_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        HttpClient,
        "validate_public_url",
        staticmethod(lambda url: "provider.example"),
    )
    responses.add(
        responses.GET,
        "https://provider.example/catalogue",
        body=b"x" * (70 * 1024),
        status=200,
    )
    client = HttpClient(max_response_bytes=64 * 1024)
    with pytest.raises(requests.exceptions.RequestException, match="size limit"):
        client.get("https://provider.example/catalogue")
