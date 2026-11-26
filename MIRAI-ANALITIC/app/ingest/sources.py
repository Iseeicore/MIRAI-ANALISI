"""Source policy and factory: which batch roots are acceptable and which StudySource serves them.

- http(s) URLs: only hosts listed in INGEST_ALLOWED_HOSTS ("host" = any port, "host:port" = that
  port). Empty allowlist = no URL source allowed. No credentials in the URL. Checked when a batch
  is created and again by HttpIndexSource on every request (SSRF guard).
- Anything else must be an absolute path that normalizes to the dicom root (default /dicom) or
  below it.
"""
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

DEFAULT_DICOM_ROOT = "/dicom"
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
URL_SCHEMES = ("http", "https")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class SourceRejected(ValueError):
    """The source is not acceptable; the message is safe to show to API clients."""


@dataclass(frozen=True)
class SourcePolicy:
    allowed_hosts: set = field(default_factory=set)  # {(hostname, port or None)}
    dicom_root: str = DEFAULT_DICOM_ROOT
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES


def parse_allowed_hosts(value: str) -> set:
    hosts = set()
    for item in (value or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if sep and port.isdigit():
            hosts.add((host, int(port)))
        else:
            hosts.add((item, None))
    return hosts


def _effective_port(parts) -> int:
    return parts.port or (443 if parts.scheme == "https" else 80)


def ensure_url_allowed(url: str, allowed_hosts: set):
    """Raise SourceRejected unless the URL is http(s), has no credentials and targets an allowed host."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in URL_SCHEMES:
        raise SourceRejected(f"unsupported scheme '{parts.scheme}': only http and https are allowed")
    if parts.username or parts.password:
        raise SourceRejected("credentials in the URL are not supported")
    host = (parts.hostname or "").lower()
    if not host:
        raise SourceRejected("URL has no host")
    try:
        port = _effective_port(parts)
    except ValueError:
        raise SourceRejected("URL has an invalid port") from None
    if (host, port) not in allowed_hosts and (host, None) not in allowed_hosts:
        raise SourceRejected(f"host '{host}:{port}' is not allowed (see INGEST_ALLOWED_HOSTS)")


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def validate_source(source: str, policy: SourcePolicy) -> str:
    """Return the normalized batch root, or raise SourceRejected with a client-safe reason."""
    source = (source or "").strip()
    if not source:
        raise SourceRejected("source is empty")
    if _SCHEME_RE.match(source):
        ensure_url_allowed(source, policy.allowed_hosts)
        parts = urlsplit(source)
        if parts.query or parts.fragment:
            raise SourceRejected("URL must not have a query or fragment")
        return _normalize_url(source)
    root = posixpath.normpath(policy.dicom_root)
    if not source.startswith("/"):
        raise SourceRejected(f"path is outside {root}: use an absolute path under it")
    path = posixpath.normpath(source)
    if path != root and not path.startswith(root.rstrip("/") + "/"):
        raise SourceRejected(f"path is outside {root}")
    return path


def is_url(source_root: str) -> bool:
    return source_root.lower().startswith(("http://", "https://"))


def make_source(source_root: str, policy: SourcePolicy):
    """StudySource for a batch root. Re-validates the root, so a revoked allowlist entry stops the batch."""
    root = validate_source(source_root, policy)
    if is_url(root):
        from .adapters.http_index_source import HttpIndexSource

        return HttpIndexSource(root, policy.allowed_hosts, policy.max_file_bytes)
    from .adapters.filesystem_source import FilesystemSource

    return FilesystemSource(root)
