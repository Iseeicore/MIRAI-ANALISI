"""Standalone checks for the /batches endpoints (run with: python test_ingest_api.py). No pytest in this project.

Uses FastAPI's TestClient with the in-memory repository, so no MySQL is needed.
"""
import io
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import load_workbook

import api
from ingest.adapters.memory_repository import MemoryRepository
from ingest_test_support import TEST_SECRET
from ingest.domain import VIEWS, hash_uid
from ingest.sources import SourcePolicy, parse_allowed_hosts

dicom_root = Path(tempfile.mkdtemp(prefix="api_dicom_"))
(dicom_root / "e2e").mkdir()
repo = MemoryRepository()
policy = SourcePolicy(allowed_hosts=parse_allowed_hosts("e2e-files:8080"), dicom_root=dicom_root.as_posix())

api.app.dependency_overrides[api.get_ingest_repository] = lambda: repo
api.app.dependency_overrides[api.get_source_policy] = lambda: policy
client = TestClient(api.app)

# Existing endpoint untouched.
assert client.get("/health").json()["status"] == "ok"

# Accepted sources.
r = client.post("/batches", json={"source": "http://e2e-files:8080/"})
assert r.status_code == 201, r.text
url_batch = r.json()["batch_id"]
assert repo.batches[url_batch]["source_root"] == "http://e2e-files:8080/"

r = client.post("/batches", json={"source": f"{dicom_root.as_posix()}/e2e"})
assert r.status_code == 201, r.text


def bad(source, reason):
    r = client.post("/batches", json={"source": source})
    assert r.status_code == 400, (source, r.status_code, r.text)
    assert reason in r.json()["detail"].lower(), (source, r.json())


bad("http://example.com/", "not allowed")
bad("http://e2e-files:9090/", "not allowed")
bad("ftp://e2e-files:8080/", "scheme")
bad("/etc", "outside")
bad(f"{dicom_root.as_posix()}/../etc", "outside")
bad(f"{dicom_root.as_posix()}/missing", "not a directory")
assert client.post("/batches", json={}).status_code == 422
assert len(repo.batches) == 2, "rejected sources must not create batches"

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
assert done_hash not in r.text and "1.2.3" not in r.text and "e2e-files" not in r.text

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
