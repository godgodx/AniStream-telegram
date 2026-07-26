from __future__ import annotations

import threading
import ipaddress
import socket
from collections.abc import Mapping
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


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
            adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
            session = requests.Session()
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
            host = self.validate_public_url(current_url)
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
                return response
            next_url = urljoin(current_url, location)
            self.validate_public_url(next_url)
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
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            port = parsed.port
        except ValueError as exc:
            raise requests.exceptions.InvalidURL("malformed upstream URL") from exc
        if parsed.scheme not in {"http", "https"} or not host:
            raise requests.exceptions.InvalidURL(
                "only absolute HTTP(S) upstream URLs are allowed"
            )
        if parsed.username or parsed.password:
            raise requests.exceptions.InvalidURL("upstream URL credentials are forbidden")
        if port not in {None, 80, 443}:
            raise requests.exceptions.InvalidURL("upstream port is not allowed")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host,
                    port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise requests.exceptions.ConnectionError(
                f"upstream DNS resolution failed for {host}"
            ) from exc
        if not addresses:
            raise requests.exceptions.ConnectionError(
                f"upstream DNS returned no addresses for {host}"
            )
        for value in addresses:
            address = ipaddress.ip_address(value.split("%", 1)[0])
            if not address.is_global:
                raise requests.exceptions.InvalidURL(
                    f"upstream host resolved to a non-public address: {host}"
                )
        return host
