"""Standalone checks for the crawler (run with: python test_ingest_crawler.py). No pytest in this project.

Decision 0003: a study is a StudyInstanceUID read from the headers, never a folder. Uses a temp-dir
FilesystemSource, synthetic DICOMs and the in-memory repository. Covers: (a) original layout,
(b) date folder with numeric patient folders, (c) one study across 2 folders, (d) skipped files,
(e) a study queued while the crawl is still running (+ extra image after DONE), (f) resume without
duplicates, (g) INCOMPLETE at crawl end, (h) header larger than the first ranged read.
"""
import tempfile
from pathlib import Path

from ingest.adapters.filesystem_source import FilesystemSource
from ingest.adapters.memory_repository import FakeClock, MemoryRepository
from ingest.domain import BatchStatus, StudyStatus, hash_uid
from ingest.workers.crawler import crawl_next_batch
from ingest_test_support import TEST_SECRET, new_uid, write_dicom

root = Path(tempfile.mkdtemp(prefix="crawl_test_"))
uid = {name: new_uid() for name in "ABCDEF"}

# (a) original layout, one view per series folder, one file without extension
for i, view in enumerate(["L_CC", "L_MLO", "R_CC", "R_MLO"]):
    name = "image-00000" if view == "L_MLO" else "image-00000.dcm"
    write_dicom(root / f"PATIENT_A_20260101/series-0000{i}/{name}", uid["A"], view)
# (b) a date folder holding 2 patients with numeric folder names, different depths
for i, view in enumerate(["R_MLO", "L_CC", "R_CC", "L_MLO"]):
    write_dicom(root / f"2026-11/0012/x{i}.dcm", uid["B"], view)
    write_dicom(root / f"2026-11/0013/s/1/y{i}.dcm", uid["C"], view)
# (c) one study spread across two folders
write_dicom(root / "split/0020/a/1.dcm", uid["D"], "L_CC")
write_dicom(root / "split/0020/a/2.dcm", uid["D"], "L_MLO")
write_dicom(root / "split/0020-extra/3.dcm", uid["D"], "R_CC")
write_dicom(root / "split/0020-extra/4.dcm", uid["D"], "R_MLO")
# (d) files that must be recorded as skipped
(root / "notes.txt").write_bytes(b"not a dicom at all" * 20)
(root / "junk").mkdir()
(root / "junk/empty.dcm").write_bytes(b"")
(root / "junk/fake.dcm").write_bytes(b"\x00" * 128 + b"DICM" + b"\xff" * 300)
write_dicom(root / "junk/ct.dcm", new_uid(), modality="CT", laterality="L", view_position="CC")
write_dicom(root / "junk/noview.dcm", new_uid(), laterality="L", view_position=None)
# (g) only 3 views
for view in ["L_CC", "L_MLO", "R_CC"]:
    write_dicom(root / f"partial/0030/{view}.dcm", uid["E"], view)
# (h) one image whose header is larger than the first 64 KiB ranged read
for view in ["L_CC", "L_MLO", "R_CC", "R_MLO"]:
    write_dicom(root / f"big/{view}.dcm", uid["F"], view, padding_bytes=100_000 if view == "R_MLO" else 0)
# extra image of study B, found after B is already DONE (listed last: "zzz" sorts after everything)
write_dicom(root / "zzz_later/extra.dcm", uid["B"], "L_CC")

clock = FakeClock()
repo = MemoryRepository(clock=clock)
events = {}


class ObservedSource(FilesystemSource):
    """Runs a hook when the crawler reaches a folder, and counts header reads."""

    def __init__(self, source_root, hook=None, crash_after_opens=None):
        super().__init__(source_root)
        self.hook, self.crash_after_opens, self.opens = hook, crash_after_opens, 0

    def list_dir(self, path=""):
        if self.hook and path == "zzz_later":
            self.hook()
        return super().list_dir(path)

    def open(self, path, byte_range=None):
        self.opens += 1
        if self.crash_after_opens is not None and self.opens > self.crash_after_opens:
            raise RuntimeError("simulated crawler crash")
        return super().open(path, byte_range)


def finish_first_study_while_crawling():
    """(e): while the crawl is still running, study B is already claimable; drive it to DONE."""
    events["batch_status_mid_crawl"] = repo.batches[batch1]["status"]
    job = repo.claim_fetch("fetcher", lease_seconds=600, max_staged=20)
    events["claimed_mid_crawl"] = job.study_id
    repo.mark_ready(job.study_id, "fetcher", job_uid_hash(job), "anon",
                    {i.view_type: (i.id, f"{job.study_id}/anon/{i.id}.dcm") for i in job.images})
    sj = repo.claim_score("scorer", lease_seconds=900)
    repo.mark_done(sj.study_id, "scorer", {f"Year {n}": n / 10 for n in range(1, 6)})


def job_uid_hash(job):
    return repo.studies[job.study_id]["study_uid_hash"]


batch1 = repo.create_batch(str(root))
assert crawl_next_batch(repo, lambda r: ObservedSource(r, hook=finish_first_study_while_crawling), "crawler", 600, secret=TEST_SECRET)
assert repo.batches[batch1]["status"] == BatchStatus.CRAWLED

studies = {s["study_uid_hash"]: s for s in repo.studies_of(batch1)}
assert set(studies) == {hash_uid(uid[k], TEST_SECRET) for k in "ABCDEF"}, "one study per StudyInstanceUID, not per folder"
by = {k: studies[hash_uid(uid[k], TEST_SECRET)] for k in "ABCDEF"}

# (e) queued as soon as the 4th view was seen, while the batch was still CRAWLING
assert events["batch_status_mid_crawl"] == BatchStatus.CRAWLING
assert events["claimed_mid_crawl"] == by["B"]["id"]
assert by["B"]["status"] == StudyStatus.DONE, "a DONE study must not be reopened by later images"
assert len(repo.images_of(by["B"]["id"])) == 4

for k in "ACDF":
    assert by[k]["status"] == StudyStatus.DISCOVERED, (k, by[k])
    assert sorted(i["view_type"] for i in repo.images_of(by[k]["id"])) == ["L_CC", "L_MLO", "R_CC", "R_MLO"]

# folder = common parent of the study's images; pseudonym = first 12 hex chars of the hash
assert by["A"]["folder"] == "PATIENT_A_20260101"
assert by["B"]["folder"] == "2026-11/0012"
assert by["C"]["folder"] == "2026-11/0013/s/1"
assert by["D"]["folder"] == "split", by["D"]["folder"]
assert by["F"]["folder"] == "big"
assert sorted(i["source_path"] for i in repo.images_of(by["A"]["id"])) == [
    "PATIENT_A_20260101/series-00000/image-00000.dcm", "PATIENT_A_20260101/series-00001/image-00000",
    "PATIENT_A_20260101/series-00002/image-00000.dcm", "PATIENT_A_20260101/series-00003/image-00000.dcm"]

# (g) missing views at crawl end
assert by["E"]["status"] == StudyStatus.INCOMPLETE
assert by["E"]["error"] == "missing views: R_MLO", by["E"]["error"]
assert by["E"]["folder"] == "partial/0030"

# (d) skipped files with folder, file name and reason
skipped = {(s["folder"], s["file_name"]): s["reason"] for s in repo.skipped_of(batch1)}
assert skipped == {
    (".", "notes.txt"): "not DICOM",
    ("junk", "empty.dcm"): "not DICOM",
    ("junk", "fake.dcm"): "unreadable header",
    ("junk", "ct.dcm"): "not MG (Modality=CT)",
    ("junk", "noview.dcm"): "missing view tags",
    ("zzz_later", "extra.dcm"): "extra image after study completed",
}, skipped
assert repo.batches[batch1]["total_skipped"] == 6
assert repo.batches[batch1]["total_studies"] == 6

# No StudyInstanceUID in clear anywhere in study rows.
for s in repo.studies.values():
    for value in s.values():
        assert not any(u in str(value) for u in uid.values()), value

# (f) a crawl that crashes midway and resumes registers each file exactly once
batch2 = repo.create_batch(str(root))
crashing = ObservedSource(str(root), crash_after_opens=12)
try:
    crawl_next_batch(repo, lambda r: crashing, "crawler-1", 600, secret=TEST_SECRET)
    raise AssertionError("expected the simulated crash")
except RuntimeError:
    pass
assert repo.batches[batch2]["status"] == BatchStatus.CRAWLING
registered_before = len(repo.seen_paths(batch2))
assert 0 < registered_before < 29
clock.advance(601)
resumed = ObservedSource(str(root))
assert crawl_next_batch(repo, lambda r: resumed, "crawler-2", 600, secret=TEST_SECRET)
assert repo.batches[batch2]["status"] == BatchStatus.CRAWLED
images2 = [i for s in repo.studies_of(batch2) for i in repo.images_of(s["id"])]
assert len(images2) == len({i["source_path"] for i in images2}) == 24, len(images2)  # 23 study images + extra.dcm of B
assert len(repo.skipped_of(batch2)) == 5, repo.skipped_of(batch2)  # no DONE study in batch 2
assert len(repo.studies_of(batch2)) == 6
assert resumed.opens < 29, "files already registered must not be read again"
assert crawl_next_batch(repo, lambda r: ObservedSource(r), "crawler-2", 600, secret=TEST_SECRET) is False

print("OK: crawler checks passed")
