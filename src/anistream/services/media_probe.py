from __future__ import annotations

from urllib.parse import urlparse

from anistream.models import ProbeResult, ResolvedMedia
from anistream.utils.http import HttpClient


class RemoteMediaProbe:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def probe(self, media: ResolvedMedia) -> ProbeResult:
        headers = dict(media.headers)
        headers["Range"] = "bytes=0-65535"
        try:
            response = self.http.get(media.url, headers=headers, stream=True, timeout=(10, 20))
        except Exception as exc:
            return ProbeResult(False, detail=f"connection failed: {exc}")
        try:
            if response.status_code not in (200, 206):
                return ProbeResult(False, detail=f"HTTP {response.status_code}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            first = b""
            for chunk in response.iter_content(65536):
                if chunk:
                    first += chunk
                if len(first) >= 65536:
                    break
            path = urlparse(response.url).path.lower()
            if ".m3u8" in path or "mpegurl" in content_type or first.lstrip().startswith(b"#EXTM3U"):
                if first.lstrip().startswith(b"#EXTM3U"):
                    return ProbeResult(True, "hls", "valid HLS playlist")
                return ProbeResult(False, "hls", "response did not contain an HLS playlist")
            if content_type.startswith(("text/", "image/")) or "html" in content_type:
                return ProbeResult(False, detail=f"unexpected content type: {content_type or 'unknown'}")
            if len(first) >= 12 and first[4:8] == b"ftyp":
                return ProbeResult(True, "mp4", "ISO Base Media header detected")
            if content_type.startswith("video/") and len(first) >= 1024:
                return ProbeResult(True, "video", f"video response: {content_type}")
            return ProbeResult(False, detail="response did not look like playable media")
        finally:
            response.close()
