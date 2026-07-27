from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ProxyError
from requests.models import PreparedRequest
from requests.utils import select_proxy
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.util.retry import Retry


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def _url_parts(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise requests.exceptions.InvalidURL("malformed upstream URL") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise requests.exceptions.InvalidURL(
            "only absolute HTTP(S) upstream URLs are allowed"
        )
    if parsed.username or parsed.password:
        raise requests.exceptions.InvalidURL("upstream URL credentials are forbidden")
    if port not in {80, 443}:
        raise requests.exceptions.InvalidURL("upstream port is not allowed")
    return parsed.scheme, host, port


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        addresses = tuple(
            dict.fromkeys(
                item[4][0]
                for item in socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                )
            )
        )
    except socket.gaierror as exc:
        raise requests.exceptions.ConnectionError(
            f"upstream DNS resolution failed for {host}"
        ) from exc
    if not addresses:
        raise requests.exceptions.ConnectionError(
            f"upstream DNS returned no addresses for {host}"
        )
    for value in addresses:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise requests.exceptions.ConnectionError(
                f"upstream DNS returned an invalid address for {host}"
            ) from exc
        if not address.is_global:
            raise requests.exceptions.InvalidURL(
                f"upstream host resolved to a non-public address: {host}"
            )
    return addresses


class _PinnedConnectionMixin:
    def __init__(
        self,
        *args: object,
        resolved_addresses: tuple[str, ...],
        **kwargs: object,
    ) -> None:
        self._resolved_addresses = resolved_addresses
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        last_error: Exception | None = None
        for value in self._resolved_addresses:
            address = ipaddress.ip_address(value.split("%", 1)[0])
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    source: tuple[object, ...] = self.source_address
                    if family == socket.AF_INET6 and len(source) == 2:
                        source = (source[0], source[1], 0, 0)
                    sock.bind(source)
                destination: tuple[object, ...] = (value, self.port)
                if family == socket.AF_INET6:
                    destination = (value, self.port, 0, 0)
                sock.connect(destination)
                sys.audit("http.client.connect", self, self.host, self.port)
                return sock
            except socket.timeout as exc:
                last_error = ConnectTimeoutError(
                    self,
                    f"Connection to {self.host} timed out. "
                    f"(connect timeout={self.timeout})",
                )
                last_error.__cause__ = exc
            except OSError as exc:
                last_error = NewConnectionError(
                    self,
                    f"Failed to establish a new connection: {exc}",
                )
            sock.close()
        if last_error is not None:
            raise last_error
        raise NewConnectionError(self, "DNS returned no usable public addresses")


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    pass


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedHTTPAdapter(HTTPAdapter):
    """Resolve, validate and pin every connection to the same DNS answer set."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._pinned_pools: OrderedDict[
            tuple[object, ...],
            HTTPConnectionPool,
        ] = OrderedDict()

    def get_connection_with_tls_context(
        self,
        request: PreparedRequest,
        verify: object,
        proxies: dict[str, str] | None = None,
        cert: object = None,
    ) -> HTTPConnectionPool:
        proxy = select_proxy(request.url, proxies)
        if proxy:
            raise ProxyError("upstream proxying is disabled for SSRF safety")
        scheme, host, port = _url_parts(request.url)
        addresses = _resolve_public_addresses(host, port)
        _, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        key = (
            scheme,
            host,
            port,
            addresses,
            str(verify),
            repr(cert),
        )
        cached = self._pinned_pools.get(key)
        if cached is not None:
            self._pinned_pools.move_to_end(key)
            return cached

        common: dict[str, object] = {
            "maxsize": self._pool_maxsize,
            "block": self._pool_block,
            "retries": self.max_retries,
            "resolved_addresses": addresses,
        }
        if scheme == "https":
            pool: HTTPConnectionPool = _PinnedHTTPSConnectionPool(
                host,
                port,
                **common,
                **pool_kwargs,
            )
        else:
            pool = _PinnedHTTPConnectionPool(host, port, **common)
        self._pinned_pools[key] = pool
        if len(self._pinned_pools) > 32:
            _, oldest = self._pinned_pools.popitem(last=False)
            oldest.close()
        return pool

    def close(self) -> None:
        for pool in self._pinned_pools.values():
            pool.close()
        self._pinned_pools.clear()
        super().close()


class HttpClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        cookie: str = "",
        cookie_hosts: set[str] | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
        max_redirects: int = 3,
        max_response_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.cookie = cookie.strip()
        self.cookie_hosts = {host.lower() for host in (cookie_hosts or set())}
        self.timeout = timeout
        self.max_redirects = max(0, min(5, max_redirects))
        self.max_response_bytes = max(64 * 1024, min(20 * 1024 * 1024, max_response_bytes))
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            retry = Retry(
                total=2,
                connect=2,
                read=2,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET", "HEAD"}),
            )
            adapter = _PinnedHTTPAdapter(
                max_retries=retry,
                pool_connections=8,
                pool_maxsize=8,
            )
            session = requests.Session()
            session.trust_env = False
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.8,fr;q=0.6",
        }
        if extra:
            headers.update(extra)
        return headers

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        supplied = kwargs.pop("headers", None)
        headers = self.headers(supplied if isinstance(supplied, Mapping) else None)
        kwargs.setdefault("timeout", self.timeout)
        follow_redirects = bool(kwargs.pop("allow_redirects", True))
        caller_streams = bool(kwargs.pop("stream", False))
        current_url = url
        current_method = method.upper()
        for redirect_count in range(self.max_redirects + 1):
            _, host, _ = _url_parts(current_url)
            current_headers = dict(headers)
            if self.cookie and host in self.cookie_hosts and "Cookie" not in current_headers:
                current_headers["Cookie"] = self.cookie
            response = self._session().request(
                current_method,
                current_url,
                headers=current_headers,
                allow_redirects=False,
                stream=True,
                **kwargs,
            )
            if not follow_redirects or response.status_code not in {301, 302, 303, 307, 308}:
                return response if caller_streams else self._bounded_response(response)
            location = response.headers.get("Location", "").strip()
            if not location or redirect_count >= self.max_redirects:
                return response if caller_streams else self._bounded_response(response)
            next_url = urljoin(current_url, location)
            _url_parts(next_url)
            response.close()
            if response.status_code == 303 or (
                response.status_code in {301, 302} and current_method == "POST"
            ):
                current_method = "GET"
                kwargs.pop("data", None)
                kwargs.pop("json", None)
            current_url = next_url
        raise requests.exceptions.TooManyRedirects(f"too many redirects for {url}")

    def _bounded_response(self, response: requests.Response) -> requests.Response:
        body = bytearray()
        try:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    raise requests.exceptions.RequestException(
                        "upstream response exceeded the size limit"
                    )
            response._content = bytes(body)
            response._content_consumed = True
            return response
        except Exception:
            response.close()
            raise
        finally:
            if response._content_consumed:
                response.close()

    def get(self, url: str, **kwargs: object) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> requests.Response:
        return self.request("POST", url, **kwargs)

    @staticmethod
    def validate_public_url(url: str) -> str:
        _, host, port = _url_parts(url)
        _resolve_public_addresses(host, port)
        return host
