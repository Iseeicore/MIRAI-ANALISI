"""Standalone checks for the /batches endpoints (run with: python test_ingest_api.py). No pytest in this project.

Uses FastAPI's TestClient with the in-memory repository (no MySQL) and a local ThreadingHTTPServer as
the image server. Covers the API key (decision 0004), the probe on submit, the network policy errors,
the status/report endpoints and the OpenAPI security scheme.
"""
import functools
import io
import socket
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import load_workbook

import api
from ingest.adapters.memory_repository import MemoryRepository
from ingest_test_support import TEST_SECRET
from ingest.domain import VIEWS, hash_uid
from ingest.sources import (
    DEFAULT_ALLOWED_NETWORKS, SourcePolicy, always_denied_networks, parse_allowed_hosts, parse_networks,
)

API_KEY = "k" * 43
OTHER_KEY = "z" * 43


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory):
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


images = Path(tempfile.mkdtemp(prefix="api_images_"))
(images / "2026-11").mkdir()
(images / "2026-11/x.dcm").write_bytes(b"x")
empty = Path(tempfile.mkdtemp(prefix="api_empty_"))
images_server, images_port = serve(images)
empty_server, empty_port = serve(empty)
images_url = f"http://127.0.0.1:{images_port}/"


def no_dns(host, port):
    raise socket.gaierror(socket.EAI_NONAME, "unknown")


dicom_root = Path(tempfile.mkdtemp(prefix="api_dicom_"))
(dicom_root / "e2e").mkdir()
(dicom_root / "e2e/0012").mkdir()
(dicom_root / "vacia").mkdir()
repo = MemoryRepository()
# The local test servers listen on loopback (always denied), so they are named explicitly as overrides.
policy = SourcePolicy(
    allowed_hosts=parse_allowed_hosts(f"127.0.0.1:{images_port},127.0.0.1:{empty_port},127.0.0.1:1"),
    allowed_networks=parse_networks(DEFAULT_ALLOWED_NETWORKS), denied_networks=always_denied_networks("172.32.0.0/24"),
    resolver=no_dns, dicom_root=dicom_root.as_posix())

api.app.dependency_overrides[api.get_ingest_repository] = lambda: repo
api.app.dependency_overrides[api.get_source_policy] = lambda: policy
api.app.dependency_overrides[api.get_api_keys] = lambda: [API_KEY, OTHER_KEY]
anonymous = TestClient(api.app)
client = TestClient(api.app, headers={"X-API-Key": API_KEY})

# Existing endpoints stay open.
assert anonymous.get("/health").json()["status"] == "ok"

# API key required on every /batches endpoint; missing or wrong -> 401, before anything else happens.
for method, path in [("post", "/batches"), ("get", "/batches/1"), ("get", "/batches/1/fallos.txt"),
                     ("get", "/batches/1/report.xlsx")]:
    for headers in ({}, {"X-API-Key": "wrong" * 10}):
        extra = {"json": {"source": images_url}} if method == "post" else {}
        r = getattr(anonymous, method)(path, headers=headers, **extra)
        assert r.status_code == 401, (method, path, r.status_code, r.text)
        assert r.json() == {"detail": "invalid or missing API key"}
assert not repo.batches, "an unauthenticated request must not create a batch"
assert TestClient(api.app, headers={"X-API-Key": OTHER_KEY}).get("/batches/999").status_code == 404

# Fail closed: no keys configured -> 503 even with a header.
api.app.dependency_overrides[api.get_api_keys] = lambda: []
r = client.post("/batches", json={"source": images_url})
assert r.status_code == 503 and "INGEST_API_KEYS" in r.json()["detail"], r.text
api.app.dependency_overrides[api.get_api_keys] = lambda: [API_KEY, OTHER_KEY]

# Accepted sources (the root is listed before the batch is created).
r = client.post("/batches", json={"source": images_url})
assert r.status_code == 201, r.text
url_batch = r.json()["batch_id"]
assert repo.batches[url_batch]["source_root"] == images_url

r = client.post("/batches", json={"source": f"{dicom_root.as_posix()}/e2e"})
assert r.status_code == 201, r.text


def bad(source, status, reason):
    r = client.post("/batches", json={"source": source})
    assert r.status_code == status, (source, r.status_code, r.text)
    assert reason in r.json()["detail"].lower(), (source, r.json())


bad("http://127.0.0.1:8000/", 400, "denied network")
bad("http://169.254.169.254/", 400, "denied network")
bad("http://172.32.0.4:5000/", 400, "denied network")
bad("http://8.8.8.8/", 400, "not in an allowed network")
bad("http://mirai:5000/", 400, "could not resolve")
bad("ftp://10.0.0.1/", 400, "scheme")
bad("/etc", 400, "outside")
bad(f"{dicom_root.as_posix()}/../etc", 400, "outside")
bad("http://127.0.0.1:1/", 422, "could not connect to 127.0.0.1:1")
bad(f"http://127.0.0.1:{empty_port}/", 422, "not a folder listing or is empty")
bad(f"http://127.0.0.1:{images_port}/no-such-folder/", 422, "not a folder listing or is empty")
bad(f"{dicom_root.as_posix()}/missing", 422, "does not exist")
bad(f"{dicom_root.as_posix()}/vacia", 422, "empty")
assert client.post("/batches", json={}).status_code == 422
assert len(repo.batches) == 2, "rejected sources must not create batches"

# Swagger: example, Spanish description, APIKeyHeader security scheme, documented error responses.
assert anonymous.get("/docs").status_code == 200
spec = anonymous.get("/openapi.json").json()
schemes = spec["components"]["securitySchemes"]
assert any(s["type"] == "apiKey" and s["in"] == "header" and s["name"] == "X-API-Key" for s in schemes.values()), schemes
post = spec["paths"]["/batches"]["post"]
assert post.get("security"), post
assert {"201", "400", "401", "422", "503"} <= set(post["responses"]), post["responses"].keys()
assert "Pegue la URL" in str(spec), "Spanish guidance for the supervisor"
assert "http://10.0.5.20:8080/mamografias/" in str(spec["components"]["schemas"]["BatchRequest"])
for path in ("/batches/{batch_id}", "/batches/{batch_id}/fallos.txt", "/batches/{batch_id}/report.xlsx"):
    assert spec["paths"][path]["get"].get("security"), path
assert not spec["paths"]["/health"]["get"].get("security")

# A DONE study, an INCOMPLETE one and a skipped file.
done_hash, inc_hash = hash_uid("1.2.3.done", TEST_SECRET), hash_uid("1.2.3.inc", TEST_SECRET)
for view in VIEWS:
    repo.register_image(url_batch, done_hash, f"2026-11/0012/{view}.dcm", 1, view)
repo.register_image(url_batch, inc_hash, "2026-11/0013/a.dcm", 1, "L_CC")
repo.register_skipped(url_batch, "leeme.txt", "not DICOM")
job = repo.claim_fetch("f", 600, 20)
repo.mark_ready(job.study_id, "f", done_hash, "anon",
                {i.view_type: (i.id, f"{job.study_id}/anon/{i.id}.dcm") for i in job.images})
scores = {"Year 1": 0.1, "Year 2": 0.2, "Year 3": 0.3, "Year 4": 0.4, "Year 5": 0.5}
sj = repo.claim_score("s", 900)
repo.mark_done(sj.study_id, "s", scores)
batch = repo.claim_batch("c", 600)
repo.finish_batch(batch.id, "c")
inc_id = [s["id"] for s in repo.studies_of(url_batch) if s["study_uid_hash"] == inc_hash][0]

# Status: counts, DONE scores and INCOMPLETE/FAILED reasons with folder + pseudonym; no UID, no hash.
r = client.get(f"/batches/{url_batch}")
assert r.status_code == 200, r.text
body = r.json()
assert body["batch_id"] == url_batch and body["status"] == "CRAWLED"
assert body["counts"]["DONE"] == 1 and body["counts"]["INCOMPLETE"] == 1
assert body["total_skipped"] == 1
assert body["done"] == [{"study_id": sj.study_id, "pseudonym": done_hash[:12], "folder": "2026-11/0012",
                         "scores": scores}], body["done"]
assert body["problems"] == [{"study_id": inc_id, "pseudonym": inc_hash[:12], "folder": "2026-11/0013",
                             "status": "INCOMPLETE", "error": "missing views: L_MLO, R_CC, R_MLO"}], body["problems"]
assert done_hash not in r.text and "1.2.3" not in r.text and images_url not in r.text

# Reports, built on demand.
r = client.get(f"/batches/{url_batch}/fallos.txt")
assert r.status_code == 200
assert r.headers["content-type"] == "text/plain; charset=utf-8", r.headers["content-type"]
assert r.headers["content-disposition"] == f"attachment; filename=fallos_batch_{url_batch}.txt"
assert r.text.splitlines() == [
    "carpeta\tseudonimo\testado\tmotivo",
    f"2026-11/0013\t{inc_hash[:12]}\tINCOMPLETE\tmissing views: L_MLO, R_CC, R_MLO",
    ".\t-\tIGNORADO\tleeme.txt: not DICOM",
], r.text

r = client.get(f"/batches/{url_batch}/report.xlsx")
assert r.status_code == 200
assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
assert r.headers["content-disposition"] == f"attachment; filename=report_batch_{url_batch}.xlsx"
assert load_workbook(io.BytesIO(r.content)).sheetnames == ["Resumen", "Estudios", "Fallos", "Leyenda"]

for path in ("/batches/999999", "/batches/999999/fallos.txt", "/batches/999999/report.xlsx"):
    assert client.get(path).status_code == 404, path

print("OK: /batches API checks passed")
