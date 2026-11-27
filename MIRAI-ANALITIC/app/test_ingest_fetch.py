"""Standalone checks for the fetch worker (run with: python test_ingest_fetch.py). No pytest in this project.

Crawls synthetic DICOMs into the in-memory repository and runs the real anonymize/scan/sanitize
pipeline through the fetch worker.
"""
import tempfile
from pathlib import Path

import pydicom

from ingest.adapters.filesystem_source import FilesystemSource
from ingest.adapters.memory_repository import FakeClock, MemoryRepository
from ingest.domain import StudyStatus, hash_uid
from ingest.workers.crawler import crawl_next_batch
from ingest.workers.fetch_worker import fetch_next_study
from ingest_test_support import TEST_SECRET, new_uid, write_dicom

root = Path(tempfile.mkdtemp(prefix="fetch_test_src_"))
staging = Path(tempfile.mkdtemp(prefix="fetch_test_staging_"))

# SPREAD: 4 views across two folders plus a duplicate L_CC (the earlier acquisition must win).
spread_uid = new_uid()
write_dicom(root / "2026-11/0012/a/x1.dcm", spread_uid, "R_MLO")
write_dicom(root / "2026-11/0012/b/x2.dcm", spread_uid, "L_CC", acquisition_time="090000")
write_dicom(root / "2026-11/0012-extra/x3.dcm", spread_uid, "R_CC")
write_dicom(root / "2026-11/0012-extra/x4.dcm", spread_uid, "L_MLO")
write_dicom(root / "2026-11/0012-extra/x5.dcm", spread_uid, "L_CC", acquisition_time="110000")

# BROKEN: 4 views at crawl time, but one file is corrupted before the fetch -> INCOMPLETE.
broken_uid = new_uid()
for view in ["L_CC", "L_MLO", "R_CC", "R_MLO"]:
    write_dicom(root / f"2026-11/0013/{view}.dcm", broken_uid, view)

clock = FakeClock()
repo = MemoryRepository(clock=clock)
batch_id = repo.create_batch(str(root))
assert crawl_next_batch(repo, FilesystemSource, worker_id="crawler", lease_seconds=600, secret=TEST_SECRET)
(root / "2026-11/0013/R_MLO.dcm").write_bytes(b"\x00" * 128 + b"DICM" + b"\xff" * 64)

studies = {s["study_uid_hash"]: s for s in repo.studies_of(batch_id)}
spread = studies[hash_uid(spread_uid, TEST_SECRET)]
broken = studies[hash_uid(broken_uid, TEST_SECRET)]


def fetch(max_staged=20):
    return fetch_next_study(repo, FilesystemSource, staging_dir=str(staging), worker_id="fetcher",
                            lease_seconds=600, max_staged=max_staged,
                            secret=TEST_SECRET)


# Backpressure: with a limit of 1, the first READY study blocks further claims.
assert fetch(max_staged=1) is True
assert repo.studies[spread["id"]]["status"] == StudyStatus.READY, repo.studies[spread["id"]]
assert fetch(max_staged=1) is False, "backpressure must stop claiming while staging is full"

# READY: views from headers, files from both folders, duplicates resolved, anonymized, raw gone.
row = repo.studies[spread["id"]]
assert row["study_uid_hash"] == hash_uid(spread_uid, TEST_SECRET)
assert row["anon_id"] and row["anon_id"] != "REAL^NAME"
images = repo.images_of(spread["id"])
selected = {i["view_type"]: i for i in images if i["selected"]}
assert sorted(selected) == ["L_CC", "L_MLO", "R_CC", "R_MLO"], selected
assert selected["L_CC"]["source_path"] == "2026-11/0012/b/x2.dcm", "earliest duplicate view must be kept"
assert len([i for i in images if not i["selected"]]) == 1
assert all(i["view_type"] for i in images), "header views from the crawl must be kept"
for view, img in selected.items():
    ds = pydicom.dcmread(staging / img["staged_path"])
    assert f"{ds.ImageLaterality}_{ds.ViewPosition}" == view
    assert str(ds.PatientName) != "REAL^NAME" and "PatientBirthDate" not in ds
assert not (staging / str(spread["id"]) / "raw").exists(), "raw copies must be deleted after anonymization"

# INCOMPLETE: the corrupted file is dropped (no crash) and the missing view is named.
assert fetch() is True
b = repo.studies[broken["id"]]
assert b["status"] == StudyStatus.INCOMPLETE, b
assert b["error"] == "missing views: R_MLO", b["error"]
assert not (staging / str(broken["id"])).exists(), "INCOMPLETE staging must be cleaned"
assert [(s["folder"], s["file_name"], s["reason"]) for s in repo.skipped_of(batch_id)] == [
    ("2026-11/0013", "R_MLO.dcm", "unreadable DICOM at fetch")], "anonymizer failures are recorded"
assert fetch() is False, "nothing left to fetch"

# A crashed fetch (lease expired) is claimed again; an active lease is not.
batch2 = repo.create_batch(str(root))
assert crawl_next_batch(repo, FilesystemSource, worker_id="crawler", lease_seconds=600, secret=TEST_SECRET)
broken2 = [s for s in repo.studies_of(batch2) if s["study_uid_hash"] == hash_uid(broken_uid, TEST_SECRET)][0]
assert broken2["status"] == StudyStatus.INCOMPLETE, "the corrupted file is now skipped at crawl time"
job = repo.claim_fetch("dead-fetcher", lease_seconds=600, max_staged=20)
assert job is not None
assert fetch() is False, "active lease must not be stolen"
clock.advance(601)
assert fetch() is True
assert repo.studies[job.study_id]["status"] in (StudyStatus.READY, StudyStatus.INCOMPLETE)

print("OK: fetch worker checks passed")
