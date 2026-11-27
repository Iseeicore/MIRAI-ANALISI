"""Source policy and factory: which batch roots are acceptable and which StudySource serves them.

http(s) URLs (decision 0004, SSRF guard):
- The scheme must be http or https, with no credentials in the URL.
- A host named in INGEST_ALLOWED_HOSTS ("host" = any port, "host:port" = that port) is an explicit
  override: no network check.
- Otherwise the host is resolved and EVERY resolved IP must be inside INGEST_ALLOWED_NETWORKS
  (default: the private ranges) and outside the always-denied networks: loopback, link-local,
  0.0.0.0/8, multicast, broadcast, the compose network (COMPOSE_SUBNET) and the Docker default bridge.
- The check runs when a batch is created and again on every request (HttpIndexSource), which also
  pins plain-http requests to the validated IP.
- Error messages name the reason and only echo what the caller sent (never a resolved internal IP).

Anything else must be an absolute path that normalizes to the dicom root (default /dicom) or below it.
"""
import ipaddress
import posixpath
import re
import socket
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

DEFAULT_DICOM_ROOT = "/dicom"
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_ALLOWED_NETWORKS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
DEFAULT_COMPOSE_SUBNET = "172.30.0.0/24"
DOCKER_DEFAULT_BRIDGE = "172.17.0.0/16"
ALWAYS_DENIED = (
    "127.0.0.0/8", "::1/128", "::/128",           # loopback / unspecified
    "169.254.0.0/16", "fe80::/10",                # link-local (cloud metadata lives here)
    "0.0.0.0/8",                                  # "this network"
    "224.0.0.0/4", "ff00::/8",                    # multicast
    "255.255.255.255/32",                         # broadcast
    DOCKER_DEFAULT_BRIDGE,
)
URL_SCHEMES = ("http", "https")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
DENIED_HINT = "loopback, link-local, multicast, broadcast, Docker or compose network"


class SourceRejected(ValueError):
    """The source is not acceptable (400); the message is safe to show to API clients."""


class SourceUnavailable(ValueError):
    """The source is acceptable but could not be listed, or is empty (422)."""


def parse_networks(value: str) -> tuple:
    """CIDR list -> tuple of ip_network. Raises ValueError on an invalid entry (fail fast at startup)."""
    return tuple(ipaddress.ip_network(item.strip(), strict=False) for item in (value or "").split(",") if item.strip())


def always_denied_networks(compose_subnet: Optional[str]) -> tuple:
    return parse_networks(",".join(ALWAYS_DENIED + ((compose_subnet,) if compose_subnet else ())))


def system_resolver(host: str, port: int) -> list:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


@dataclass(frozen=True)
class SourcePolicy:
    allowed_hosts: set = field(default_factory=set)  # {(hostname, port or None)}: explicit overrides
    allowed_networks: tuple = field(default_factory=lambda: parse_networks(DEFAULT_ALLOWED_NETWORKS))
    denied_networks: tuple = field(default_factory=lambda: always_denied_networks(DEFAULT_COMPOSE_SUBNET))
    resolver: Callable = system_resolver
    dicom_root: str = DEFAULT_DICOM_ROOT
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES


@dataclass(frozen=True)
class UrlTarget:
    scheme: str
    host: str
    port: int
    pinned_ip: Optional[str]  # validated IP to connect to; None for explicit host overrides


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


def _as_ip(value: str):
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])  # drop an IPv6 scope id
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _check_ip(ip, host: str, literal: bool, policy: SourcePolicy):
    subject = f"'{host}'" if literal else f"'{host}' resolves to an address that"
    if any(ip in net for net in policy.denied_networks if net.version == ip.version):
        raise SourceRejected(f"{subject} is in a denied network ({DENIED_HINT})" if literal
                             else f"'{host}' resolves to a denied network ({DENIED_HINT})")
    if not any(ip in net for net in policy.allowed_networks if net.version == ip.version):
        raise SourceRejected(f"{subject} is not in an allowed network (INGEST_ALLOWED_NETWORKS)")


def resolve_target(url: str, policy: SourcePolicy) -> UrlTarget:
    """Validate a URL against the policy and return where to connect. Raises SourceRejected."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in URL_SCHEMES:
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
    if (host, port) in policy.allowed_hosts or (host, None) in policy.allowed_hosts:
        return UrlTarget(scheme, host, port, None)

    literal = _as_ip(host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            addresses = [ip for ip in (_as_ip(a) for a in policy.resolver(host, port)) if ip is not None]
        except (OSError, UnicodeError):
            addresses = []
        if not addresses:
            raise SourceRejected(f"could not resolve host '{host}'")
    for ip in addresses:
        _check_ip(ip, host, literal is not None, policy)
    return UrlTarget(scheme, host, port, str(addresses[0]))


def ensure_url_allowed(url: str, policy: SourcePolicy):
    resolve_target(url, policy)


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
        resolve_target(source, policy)
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


def make_source(source_root: str, policy: SourcePolicy, read_timeout: Optional[float] = None):
    """StudySource for a batch root. Re-validates the root, so a policy change stops the batch."""
    root = validate_source(source_root, policy)
    if is_url(root):
        from .adapters.http_index_source import HttpIndexSource

        return HttpIndexSource(root, policy, read_timeout=read_timeout)
    from .adapters.filesystem_source import FilesystemSource

    return FilesystemSource(root)
