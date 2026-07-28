from __future__ import annotations

from urllib.parse import urlparse

from anistream.models import (
    MAX_PREFETCHED_PLAYLIST_BYTES,
    ProbeResult,
    ResolvedMedia,
)
from anistream.utils.http import HttpClient


MP4_PROBE_BYTES = 4 * 1024


class RemoteMediaProbe:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def probe(self, media: ResolvedMedia) -> ProbeResult:
        headers = dict(media.headers)
        expected_hls = (
            media.kind == "hls"
            or ".m3u8" in urlparse(media.url).path.casefold()
        )
        maximum = (
            MAX_PREFETCHED_PLAYLIST_BYTES
            if expected_hls
            else MP4_PROBE_BYTES
        )
        headers["Range"] = f"bytes=0-{maximum - 1}"
        try:
            response = self.http.get(
                media.url,
                headers=headers,
                stream=True,
                timeout=(5, 10),
            )
        except Exception as exc:
            return ProbeResult(False, detail=f"connection failed: {exc}")
        try:
            if response.status_code not in (200, 206):
                return ProbeResult(False, detail=f"HTTP {response.status_code}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            first = bytearray()
            complete = True
            for chunk in response.iter_content(64 * 1024):
                if chunk:
                    first.extend(chunk)
                if len(first) > maximum:
                    complete = False
                    break
            body = bytes(first[:maximum])
            content_range_total = (
                response.headers.get("Content-Range", "")
                .rpartition("/")[2]
                .strip()
            )
            if response.status_code == 206:
                complete = (
                    content_range_total.isdigit()
                    and int(content_range_total) <= len(body)
                )
            path = urlparse(response.url).path.lower()
            is_hls = (
                ".m3u8" in path
                or "mpegurl" in content_type
                or body.lstrip().startswith(b"#EXTM3U")
            )
            if is_hls:
                if body.lstrip().startswith(b"#EXTM3U"):
                    return ProbeResult(
                        True,
                        "hls",
                        "valid HLS playlist",
                        body if complete else b"",
                        str(response.url) if complete else "",
                    )
                return ProbeResult(False, "hls", "response did not contain an HLS playlist")
            if content_type.startswith(("text/", "image/")) or "html" in content_type:
                return ProbeResult(False, detail=f"unexpected content type: {content_type or 'unknown'}")
            if len(body) >= 12 and body[4:8] == b"ftyp":
                return ProbeResult(True, "mp4", "ISO Base Media header detected")
            if content_type.startswith("video/") and len(body) >= 1024:
                return ProbeResult(True, "video", f"video response: {content_type}")
            return ProbeResult(False, detail="response did not look like playable media")
        finally:
            response.close()
