"""StudySource over an HTTP directory index (Apache / nginx / Python http.server autoindex pages,
or nginx `autoindex_format json`).

Paths are relative to the root URL, percent-encoded as they appear in the index, without leading or
trailing slashes (same contract as FilesystemSource). Every listed link is resolved and kept only
when it is a direct child of the folder being listed and stays inside the root URL (same scheme,
host and port, no `..` even percent-encoded). Every request re-checks the host allowlist, never
follows redirects, and full downloads are capped at max_file_bytes.
"""
import io
import json
import posixpath
import tempfile
from html.parser import HTMLParser
from typing import BinaryIO, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests

from ..domain import Entry, normalize_rel_path
from ..ports import ByteRange
from ..sources import SourceRejected, ensure_url_allowed

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 120
MAX_INDEX_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024


class SourceError(OSError):
    """A request to the source failed (HTTP error, redirect, oversized file). An OSError, so the
    workers skip the item instead of crashing."""


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def _origin(parts):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


def _child_entry(href: str, dir_url: str, root_url: str, size: Optional[int]) -> Optional[Entry]:
    """Turn one index link into an Entry, or None when it is not a safe direct child of dir_url."""
    href = href.strip()
    if not href or href.startswith(("?", "#")):
        return None
    href_parts = urlsplit(href)
    if href_parts.scheme or href_parts.netloc:  # absolute links (any host, even our own)
        return None
    resolved = urlsplit(urljoin(dir_url, href))
    if resolved.query or resolved.fragment:
        return None
    root, current = urlsplit(root_url), urlsplit(dir_url)
    if _origin(resolved) != _origin(root):
        return None
    decoded = unquote(resolved.path)
    if any(seg in (".", "..") for seg in decoded.split("/")) or "\\" in decoded:
        return None
    if not decoded.startswith(unquote(root.path)) or posixpath.normpath(decoded) != decoded.rstrip("/"):
        return None
    if not resolved.path.startswith(current.path) or len(resolved.path) <= len(current.path):
        return None  # parent, self or sibling link
    rest = resolved.path[len(current.path):]
    is_dir = rest.endswith("/")
    rest = rest.rstrip("/")
    if not rest or "/" in rest:
        return None  # only direct children
    rel = normalize_rel_path(resolved.path[len(root.path):])
    return Entry(name=unquote(rest), path=rel, is_dir=is_dir, size=None if is_dir else size)


def _json_links(body: bytes) -> list:
    links = []
    for item in json.loads(body.decode("utf-8")):
        name = str(item.get("name", ""))
        if not name:
            continue
        is_dir = item.get("type") == "directory"
        href = quote(name, safe="") + ("/" if is_dir else "")
        size = item.get("size") if isinstance(item.get("size"), int) else None
        links.append((href, size))
    return links


def parse_listing(body: bytes, content_type: str, dir_url: str, root_url: str) -> list:
    """Parse an autoindex response into sorted, de-duplicated, safe Entries."""
    if "json" in (content_type or "").lower() or body.lstrip()[:1] == b"[":
        links = _json_links(body)
    else:
        parser = _LinkParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        links = [(href, None) for href in parser.hrefs]
    entries = {}
    for href, size in links:
        entry = _child_entry(href, dir_url, root_url, size)
        if entry is not None:
            entries.setdefault(entry.path, entry)
    return sorted(entries.values(), key=lambda e: e.name)


class HttpIndexSource:
    def __init__(self, root_url: str, allowed_hosts: set, max_file_bytes: int, session=None):
        ensure_url_allowed(root_url, allowed_hosts)
        self._root = root_url if root_url.endswith("/") else root_url + "/"
        self._allowed_hosts = allowed_hosts
        self._max_file_bytes = max_file_bytes
        self._session = session or requests.Session()

    def _url(self, rel_path: str, is_dir: bool) -> str:
        rel = normalize_rel_path(rel_path)
        if any(seg in (".", "..") for seg in unquote(rel).split("/")):
            raise SourceRejected("path escapes the source root")
        url = self._root + rel
        return url + "/" if is_dir and rel else url

    def _get(self, url: str, headers=None):
        ensure_url_allowed(url, self._allowed_hosts)
        try:
            response = self._session.get(url, headers=headers or {}, stream=True, allow_redirects=False,
                                         timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))
        except requests.RequestException as exc:
            raise SourceError(f"request failed ({type(exc).__name__})") from None
        if 300 <= response.status_code < 400:
            response.close()
            raise SourceError(f"redirect refused (HTTP {response.status_code})")
        if response.status_code >= 400:
            response.close()
            raise SourceError(f"HTTP {response.status_code}")
        return response

    def _read_capped(self, response, limit: int, sink):
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            response.close()
            raise SourceError("file exceeds the size limit")
        total = 0
        try:
            for chunk in response.iter_content(CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise SourceError("file exceeds the size limit")
                sink.write(chunk)
        except requests.RequestException as exc:
            raise SourceError(f"download failed ({type(exc).__name__})") from None
        finally:
            response.close()

    @staticmethod
    def display_path(path: str) -> str:
        """Human-readable form of a root-relative path (percent-decoded) for reports."""
        return unquote(path)

    def list_dir(self, path: str = "") -> list:
        url = self._url(path, is_dir=True)
        response = self._get(url)
        body = io.BytesIO()
        self._read_capped(response, MAX_INDEX_BYTES, body)
        return parse_listing(body.getvalue(), response.headers.get("Content-Type", ""), url, self._root)

    def open(self, path: str, byte_range: Optional[ByteRange] = None) -> BinaryIO:
        url = self._url(path, is_dir=False)
        if byte_range is not None:
            start, end = byte_range
            response = self._get(url, {"Range": f"bytes={start}-{end}"})
            skip = 0 if response.status_code == 206 else start  # 200: server ignored Range
            wanted = end - start + 1
            data = bytearray()
            try:
                for chunk in response.iter_content(CHUNK_BYTES):
                    data += chunk
                    if len(data) >= skip + wanted:
                        break
            except requests.RequestException as exc:
                raise SourceError(f"download failed ({type(exc).__name__})") from None
            finally:
                response.close()
            return io.BytesIO(bytes(data[skip:skip + wanted]))
        response = self._get(url)
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY_BYTES)
        try:
            self._read_capped(response, self._max_file_bytes, spool)
        except BaseException:
            spool.close()
            raise
        spool.seek(0)
        return spool
