"""Standalone checks for the HTTP index source and the source policy (run with: python test_ingest_http_source.py).

No pytest in this project. Covers: autoindex parsing (Apache, nginx HTML/JSON, Python http.server) with
hostile links, source validation (allowlist, schemes, /dicom), and HttpIndexSource against a real
ThreadingHTTPServer on 127.0.0.1 (listing, nested crawl, Range, size cap, redirects, disallowed host).
"""
import functools
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ingest.adapters.http_index_source import HttpIndexSource, SourceError, parse_listing
from ingest.adapters.memory_repository import MemoryRepository
from ingest.domain import hash_uid
from ingest_test_support import TEST_SECRET, new_uid, write_dicom
from ingest.sources import SourcePolicy, SourceRejected, make_source, parse_allowed_hosts, validate_source
from ingest.workers.crawler import crawl_next_batch

ROOT = "http://files:8080/studies/"

HOSTILE = """
<a href="../">../</a>
<a href="./">self</a>
<a href="?C=N;O=D">Name</a>
<a href="#top">top</a>
<a href="http://evil.example/">evil</a>
<a href="//evil.example/x">evil2</a>
<a href="https://files:8080/studies/other-scheme/">other scheme</a>
<a href="http://files:9090/studies/other-port/">other port</a>
<a href="/other/">outside</a>
<a href="%2e%2e/">encoded parent</a>
<a href="%2E%2E%2Fsecret/">encoded parent slash</a>
<a href="a%2F..%2F..%2Fescape/">encoded traversal</a>
<a href="PAT_X/?C=M">query</a>
<a href="javascript:alert(1)">js</a>
<a>no href</a>
"""

APACHE = f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html><head><title>Index of /studies</title></head><body><h1>Index of /studies</h1>
<table><tr><th><a href="?C=N;O=D">Name</a></th><th><a href="?C=M;O=A">Last modified</a></th></tr>
<tr><td><a href="/">Parent Directory</a></td></tr>
<tr><td><a href="PAT_A/">PAT_A/</a></td><td>2026-11-25 10:00</td><td>-</td></tr>
<tr><td><a href="image-00000.dcm">image-00000.dcm</a></td><td>2026-11-25 10:00</td><td>26M</td></tr>
</table>{HOSTILE}</body></html>"""

NGINX_HTML = f"""<html><head><title>Index of /studies/</title></head><body>
<h1>Index of /studies/</h1><hr><pre><a href="../">../</a>
<a href="PAT%20B/">PAT B/</a>                                             25-Nov-2026 10:00       -
<a href="image-00001">image-00001</a>                                       25-Nov-2026 10:00  27262976
{HOSTILE}</pre><hr></body></html>"""

NGINX_JSON = b"""[
{ "name":"PAT_C", "type":"directory", "mtime":"Wed, 25 Nov 2026 10:00:00 GMT" },
{ "name":"x.dcm", "type":"file", "mtime":"Wed, 25 Nov 2026 10:00:00 GMT", "size":123 },
{ "name":"../etc", "type":"directory", "mtime":"Wed, 25 Nov 2026 10:00:00 GMT" },
{ "name":"..", "type":"directory", "mtime":"Wed, 25 Nov 2026 10:00:00 GMT" }
]"""

PYTHON_SERVER = f"""<!DOCTYPE HTML><html lang="en"><head><title>Directory listing for /studies/</title></head>
<body><h1>Directory listing for /studies/</h1><hr><ul>
<li><a href="PAT_D/">PAT_D/</a></li>
<li><a href="file%20name.dcm">file name.dcm</a></li>
</ul>{HOSTILE}<hr></body></html>"""


def names(entries):
    return [(e.name, e.path, e.is_dir, e.size) for e in entries]


# --- parser ---------------------------------------------------------------
assert names(parse_listing(APACHE.encode(), "text/html", ROOT, ROOT)) == [
    ("PAT_A", "PAT_A", True, None),
    ("image-00000.dcm", "image-00000.dcm", False, None),
], names(parse_listing(APACHE.encode(), "text/html", ROOT, ROOT))

assert names(parse_listing(NGINX_HTML.encode(), "text/html; charset=utf-8", ROOT, ROOT)) == [
    ("PAT B", "PAT%20B", True, None),
    ("image-00001", "image-00001", False, None),
]

assert names(parse_listing(NGINX_JSON, "application/json", ROOT, ROOT)) == [
    ("PAT_C", "PAT_C", True, None),
    ("x.dcm", "x.dcm", False, 123),
]

assert names(parse_listing(PYTHON_SERVER.encode(), "text/html", ROOT, ROOT)) == [
    ("PAT_D", "PAT_D", True, None),
    ("file name.dcm", "file%20name.dcm", False, None),
]

# Listing a sub-folder: children are root-relative; the Apache parent link points back up and is dropped.
sub = '<a href="/studies/">Parent Directory</a><a href="series-00000/">s</a><a href="/studies/PAT_A/x.dcm">abs</a>'
assert names(parse_listing(sub.encode(), "text/html", ROOT + "PAT_A/", ROOT)) == [
    ("series-00000", "PAT_A/series-00000", True, None),
    ("x.dcm", "PAT_A/x.dcm", False, None),
]

# --- source policy --------------------------------------------------------
assert parse_allowed_hosts("") == set()
assert parse_allowed_hosts(" e2e-files:8080 , Files.Example ") == {("e2e-files", 8080), ("files.example", None)}

policy = SourcePolicy(allowed_hosts=parse_allowed_hosts("e2e-files:8080,files.example"), dicom_root="/dicom")
assert validate_source("http://e2e-files:8080/", policy) == "http://e2e-files:8080/"
assert validate_source("http://e2e-files:8080/studies", policy) == "http://e2e-files:8080/studies/"
assert validate_source("https://files.example:8443/x/", policy) == "https://files.example:8443/x/"
assert validate_source("/dicom/e2e", policy) == "/dicom/e2e"
assert validate_source("/dicom/e2e/../e2e/", policy) == "/dicom/e2e"


def rejected(source, pol=policy, contains=""):
    try:
        validate_source(source, pol)
    except SourceRejected as exc:
        assert contains.lower() in str(exc).lower(), (source, str(exc))
        return True
    return False


assert rejected("http://e2e-files:9090/", contains="not allowed")
assert rejected("http://example.com/", contains="not allowed")
assert rejected("http://e2e-files:8080/", SourcePolicy(allowed_hosts=set()), contains="not allowed")
assert rejected("ftp://e2e-files:8080/", contains="scheme")
assert rejected("file:///etc/passwd", contains="scheme")
assert rejected("http://user:pw@e2e-files:8080/", contains="credentials")
assert rejected("/etc", contains="/dicom")
assert rejected("/dicom/../etc", contains="/dicom")
assert rejected("/dicomx", contains="/dicom")
assert rejected("dicom/e2e", contains="/dicom")
assert rejected("", contains="empty")

# --- live server ----------------------------------------------------------
tree = Path(tempfile.mkdtemp(prefix="http_src_"))
uid_a, uid_b = new_uid(), new_uid()
write_dicom(tree / "PAT_A/series-00000/image-00000.dcm", uid_a, "L_CC")
write_dicom(tree / "PAT_A/series-00001/deeper/image-00000", uid_a, "L_MLO")
write_dicom(tree / "PAT B/series-00000/image-00000.dcm", uid_b, "R_CC")
(tree / "PAT_A/notes.txt").write_bytes(b"junk" * 50)
DICOM_A = (tree / "PAT_A/series-00000/image-00000.dcm").read_bytes()
DICOM_B = (tree / "PAT B/series-00000/image-00000.dcm").read_bytes()


class Handler(SimpleHTTPRequestHandler):
    support_range = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://evil.example/")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        target = Path(self.translate_path(self.path))
        if self.support_range and rng and target.is_file():
            start, end = (int(x) for x in rng.split("=")[1].split("-"))
            data = target.read_bytes()[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(tree)))
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]
root_url = f"http://127.0.0.1:{port}/"
live_policy = SourcePolicy(allowed_hosts=parse_allowed_hosts(f"127.0.0.1:{port}"), max_file_bytes=10_000)
src = make_source(root_url, live_policy)
assert isinstance(src, HttpIndexSource)

assert [(e.name, e.is_dir) for e in src.list_dir("")] == [("PAT B", True), ("PAT_A", True)]
assert [(e.name, e.is_dir) for e in src.list_dir("PAT_A")] == [
    ("notes.txt", False), ("series-00000", True), ("series-00001", True)]

# Range read: server ignoring Range (200) and server honoring it (206) both yield exactly 132 bytes.
for support in (False, True):
    Handler.support_range = support
    with src.open("PAT_A/series-00000/image-00000.dcm", byte_range=(0, 131)) as fh:
        head = fh.read()
    assert head == DICOM_A[:132], (support, len(head))
Handler.support_range = False

with src.open("PAT%20B/series-00000/image-00000.dcm") as fh:
    assert fh.read() == DICOM_B

# Nested walk through the crawler, using only the StudySource port (ranged header reads over HTTP).
repo = MemoryRepository()
batch_id = repo.create_batch(root_url)
assert crawl_next_batch(repo, lambda root: make_source(root, live_policy), "crawler", 600, secret=TEST_SECRET)
studies = {s["study_uid_hash"]: s for s in repo.studies_of(batch_id)}
assert set(studies) == {hash_uid(uid_a, TEST_SECRET), hash_uid(uid_b, TEST_SECRET)}
assert sorted(i["source_path"] for i in repo.images_of(studies[hash_uid(uid_a, TEST_SECRET)]["id"])) == [
    "PAT_A/series-00000/image-00000.dcm", "PAT_A/series-00001/deeper/image-00000"]
assert studies[hash_uid(uid_b, TEST_SECRET)]["folder"] == "PAT B/series-00000", "folders are shown decoded"
assert [(s["folder"], s["file_name"], s["reason"]) for s in repo.skipped_of(batch_id)] == [
    ("PAT_A", "notes.txt", "not DICOM")]


def raises_source_error(fn):
    try:
        fn()
    except SourceError:
        return True
    return False


# Size cap.
capped = make_source(root_url, SourcePolicy(allowed_hosts=live_policy.allowed_hosts, max_file_bytes=100))
assert raises_source_error(lambda: capped.open("PAT_A/series-00000/image-00000.dcm").read())

# Redirects are errors, never followed.
assert raises_source_error(lambda: src.list_dir("redirect"))
assert raises_source_error(lambda: src.open("redirect/x.dcm"))

# 404 surfaces as SourceError (an OSError, so workers skip the item instead of crashing).
assert raises_source_error(lambda: src.open("missing.dcm"))
assert issubclass(SourceError, OSError)

# Disallowed host: rejected before any request is sent, also per request.
try:
    HttpIndexSource(root_url, allowed_hosts={("other-host", None)}, max_file_bytes=1000)
    raise AssertionError("disallowed host accepted")
except SourceRejected:
    pass
src._allowed_hosts = set()  # allowlist revoked after creation: every request re-checks
try:
    src.list_dir("")
    raise AssertionError("request to a no-longer-allowed host was sent")
except (SourceRejected, SourceError):
    pass

# Filesystem sources still come from the same factory.
fs_root = Path(tempfile.mkdtemp(prefix="dicom_root_"))
(fs_root / "e2e").mkdir()
fs_policy = SourcePolicy(allowed_hosts=set(), dicom_root=str(fs_root.as_posix()))
assert make_source(f"{fs_root.as_posix()}/e2e", fs_policy).list_dir("") == []

server.shutdown()
print("OK: HTTP index source and source policy checks passed")
