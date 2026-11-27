"""Standalone checks for the URL network policy (decision 0004) (run with: python test_ingest_network_policy.py).

No pytest in this project. Uses a stubbed resolver; IP pinning is verified against a local
ThreadingHTTPServer that records the Host header it receives.
"""
import functools
import socket
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_network
from pathlib import Path

from ingest.adapters.http_index_source import HttpIndexSource
from ingest.config import load_source_policy
from ingest.sources import (
    DEFAULT_ALLOWED_NETWORKS, SourcePolicy, SourceRejected, always_denied_networks, parse_allowed_hosts,
    parse_networks, validate_source,
)

DNS = {
    "files.lan": ["10.0.5.20"],
    "pacs.lan": ["192.168.1.7"],
    "mixed.lan": ["10.0.0.5", "127.0.0.1"],
    "mirai": ["172.32.0.4"],
    "public.example": ["93.184.216.34"],
}


def stub_resolver(host, port):
    if host not in DNS:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return list(DNS[host])


policy = SourcePolicy(allowed_networks=parse_networks(DEFAULT_ALLOWED_NETWORKS),
                      denied_networks=always_denied_networks("172.32.0.0/24"), resolver=stub_resolver)

# Allowed: private LAN addresses, literal or by name.
for url in ["http://10.0.5.20:8080/mamografias/", "http://files.lan/x/", "http://192.168.1.7/", "http://pacs.lan:8080/",
            "https://files.lan/"]:
    assert validate_source(url, policy).startswith(url.split("://")[0]), url


def rejected(url, contains, pol=policy):
    try:
        validate_source(url, pol)
    except SourceRejected as exc:
        message = str(exc)
        assert contains in message, (url, message)
        return message
    raise AssertionError(f"accepted: {url}")


rejected("http://127.0.0.1:8000/", "denied network")
rejected("http://[::1]:8000/", "denied network")
rejected("http://[::ffff:127.0.0.1]/", "denied network")
rejected("http://169.254.169.254/", "denied network")
rejected("http://[fe80::1]/", "denied network")
rejected("http://0.0.0.0/", "denied network")
rejected("http://224.0.0.1/", "denied network")
rejected("http://255.255.255.255/", "denied network")
rejected("http://172.32.0.4:5000/", "denied network")  # compose subnet
rejected("http://172.17.0.2/", "denied network")  # Docker default bridge
rejected("http://8.8.8.8/", "not in an allowed network")
rejected("http://public.example/", "not in an allowed network")
rejected("http://mixed.lan/", "denied network")  # every resolved IP must pass
rejected("http://nowhere.lan/", "could not resolve host 'nowhere.lan'")
rejected("ftp://files.lan/", "scheme")
# A name that resolves inside the compose network is denied without revealing the internal IP.
assert "172.32" not in rejected("http://mirai:5000/", "denied network")

# Explicit override for a named host skips the network check (and DNS).
override = SourcePolicy(allowed_hosts=parse_allowed_hosts("mirai:5000"), allowed_networks=policy.allowed_networks,
                        denied_networks=policy.denied_networks, resolver=stub_resolver)
assert validate_source("http://mirai:5000/", override) == "http://mirai:5000/"
rejected("http://mirai:5001/", "denied network", override)

# Policy from the environment: defaults, compose subnet always denied, bad CIDR fails fast.
env_policy = load_source_policy({"COMPOSE_SUBNET": "172.40.0.0/24"})
assert set(env_policy.allowed_networks) == {ip_network(n) for n in DEFAULT_ALLOWED_NETWORKS.split(",")}
assert ip_network("172.40.0.0/24") in env_policy.denied_networks
assert ip_network("172.17.0.0/16") in env_policy.denied_networks
assert ip_network("127.0.0.0/8") in env_policy.denied_networks
custom = load_source_policy({"INGEST_ALLOWED_NETWORKS": "10.20.0.0/16", "COMPOSE_SUBNET": "172.40.0.0/24"})
assert custom.allowed_networks == (ip_network("10.20.0.0/16"),)
try:
    load_source_policy({"INGEST_ALLOWED_NETWORKS": "not-a-cidr"})
    raise AssertionError("invalid CIDR accepted")
except ValueError:
    pass

# IP pinning: http connects to the validated IP and sends the original Host header.
tree = Path(tempfile.mkdtemp(prefix="pin_"))
(tree / "PATIENT_FOLDER").mkdir()
seen_hosts = []


class RecordingHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        seen_hosts.append(self.headers.get("Host"))
        super().do_GET()


server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(RecordingHandler, directory=str(tree)))
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]
lan = {"images.lan": ["127.0.0.1"]}
pin_policy = SourcePolicy(allowed_networks=(ip_network("127.0.0.0/8"),), denied_networks=(),
                          resolver=lambda host, p: list(lan.get(host, [])) or stub_resolver(host, p))
src = HttpIndexSource(f"http://images.lan:{port}/", pin_policy)
assert [e.name for e in src.list_dir("")] == ["PATIENT_FOLDER"]
assert seen_hosts == [f"images.lan:{port}"], seen_hosts

# Re-validated on every request: if the name now resolves elsewhere (DNS rebinding), nothing is sent.
lan["images.lan"] = ["8.8.8.8"]
try:
    src.list_dir("")
    raise AssertionError("request sent after the name moved out of the allowed networks")
except SourceRejected as exc:
    assert "not in an allowed network" in str(exc)
assert len(seen_hosts) == 1
server.shutdown()

print("OK: URL network policy checks passed")
