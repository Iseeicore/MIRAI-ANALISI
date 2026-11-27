"""Integration checks for MySQLRepository (run with: python test_ingest_mysql.py).

Runs only when MYSQL_HOST is set. Uses a throwaway database (MYSQL_TEST_DATABASE, default
mirai_ingest_test) that it creates and drops, so the real queue is never touched. The MySQL
user needs CREATE/DROP on that database (root works).
"""
import os
import sys

import pymysql

from ingest.adapters.mysql_repository import MySQLRepository
from ingest_test_support import TEST_SECRET
from ingest.domain import VIEWS, BatchStatus, hash_uid

if not os.environ.get("MYSQL_HOST"):
    print("SKIP: MYSQL_HOST not set, MySQL integration checks not run")
    sys.exit(0)

conn_args = dict(
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
)
test_db = os.environ.get("MYSQL_TEST_DATABASE", "mirai_ingest_test")
assert test_db.endswith("_test"), "refusing to run against a non-test database"

admin = pymysql.connect(**conn_args, autocommit=True)
with admin.cursor() as cur:
    cur.execute(f"DROP DATABASE IF EXISTS `{test_db}`")
    cur.execute(f"CREATE DATABASE `{test_db}` CHARACTER SET utf8mb4")

try:
    repo = MySQLRepository(**conn_args, database=test_db)
    repo.apply_schema()
    repo.apply_schema()  # idempotent

    raw = pymysql.connect(**conn_args, database=test_db, autocommit=False)

    def sql(query, args=None):
        with raw.cursor() as cur:
            cur.execute(query, args)
            rows = cur.fetchall()
        raw.commit()
        return rows

    def study_row(uid_hash):
        return sql("SELECT id, status, folder, error FROM study WHERE study_uid_hash=%s", (uid_hash,))[0]

    # Batch claim + lease expiry.
    batch_id = repo.create_batch("/dicom/test")
    batch = repo.claim_batch("crawler-1", lease_seconds=600)
    assert batch.id == batch_id and batch.source_root == "/dicom/test"
    assert repo.claim_batch("crawler-2", lease_seconds=600) is None, "active batch lease must not be stolen"
    sql("UPDATE batch SET lease_until = NOW(6) - INTERVAL 1 SECOND WHERE id=%s", (batch_id,))
    assert repo.claim_batch("crawler-2", lease_seconds=600).id == batch_id

    # Images register under their StudyInstanceUID hash, idempotently; the 4th distinct view queues the study.
    h = {k: hash_uid(f"1.2.840.{k}", TEST_SECRET) for k in ("p0", "p1", "p2", "inc")}
    assert repo.register_image(batch_id, h["p0"], "2026-11/0012/a/1.dcm", 10, "L_CC") == "added"
    assert repo.register_image(batch_id, h["p0"], "2026-11/0012/a/1.dcm", 10, "L_CC") == "duplicate"
    assert repo.register_image(batch_id, h["p0"], "2026-11/0012/a/2.dcm", None, "L_CC") == "added"
    assert repo.register_image(batch_id, h["p0"], "2026-11/0012/b/3.dcm", 10, "L_MLO") == "added"
    assert repo.register_image(batch_id, h["p0"], "2026-11/0012-extra/4.dcm", 10, "R_CC") == "added"
    assert study_row(h["p0"])[1] == "COLLECTING"
    assert repo.register_image(batch_id, h["p0"], "2026-11/0012-extra/5.dcm", 10, "R_MLO") == "queued"
    assert study_row(h["p0"])[1:3] == ("DISCOVERED", "2026-11")
    for k in ("p1", "p2"):
        for v in VIEWS:
            repo.register_image(batch_id, h[k], f"{k}/{v}.dcm", 1, v)
    repo.register_image(batch_id, h["inc"], "partial/x.dcm", 1, "L_CC")
    assert repo.register_skipped(batch_id, "junk/leeme.txt", "not DICOM") is True
    assert repo.register_skipped(batch_id, "junk/leeme.txt", "not DICOM") is False
    assert len(repo.seen_paths(batch_id)) == 5 + 8 + 1 + 1

    ids = [r[0] for r in sql("SELECT id FROM study WHERE status='DISCOVERED' ORDER BY id")]
    assert len(ids) == 3

    # SKIP LOCKED: a row locked by another open transaction is skipped, not waited on.
    with raw.cursor() as cur:
        cur.execute("SELECT id FROM study WHERE id=%s FOR UPDATE", (ids[0],))
        job = repo.claim_fetch("fetch-1", lease_seconds=600, max_staged=20)
    raw.rollback()
    assert job is not None and job.study_id == ids[1], f"expected SKIP LOCKED to skip {ids[0]}, got {job}"
    assert [i.source_path for i in job.images] == [f"p1/{v}.dcm" for v in VIEWS]
    assert [i.view_type for i in job.images] == list(VIEWS)
    assert job.source_root == "/dicom/test"

    job0 = repo.claim_fetch("fetch-2", lease_seconds=600, max_staged=20)
    assert job0.study_id == ids[0] and len(job0.images) == 5

    # Backpressure: FETCHING (active lease) + READY + SCORING counted against max_staged.
    assert repo.claim_fetch("fetch-3", lease_seconds=600, max_staged=2) is None
    job2 = repo.claim_fetch("fetch-3", lease_seconds=600, max_staged=20)
    assert job2.study_id == ids[2]
    assert repo.claim_fetch("fetch-3", lease_seconds=600, max_staged=20) is None, "nothing claimable"

    # Lease expiry: a crashed fetcher's study is reclaimed, and the old owner can no longer finish it.
    sql("UPDATE study SET lease_until = NOW(6) - INTERVAL 1 SECOND WHERE id=%s", (ids[2],))
    again = repo.claim_fetch("fetch-4", lease_seconds=600, max_staged=20)
    assert again.study_id == ids[2]
    assert repo.mark_incomplete(ids[2], "fetch-3", "missing views: L_CC") is False, "stale owner must not win"
    assert repo.mark_incomplete(ids[2], "fetch-4", "missing views: L_CC") is True

    # READY keeps the crawl-time views; score claim, retry gating, attempts, lease expiry.
    selections = {i.view_type: (i.id, f"{job.study_id}/anon/{i.id}.dcm") for i in job.images}
    assert repo.mark_ready(job.study_id, "fetch-1", h["p1"], "anon-1", selections) is True
    assert sql("SELECT COUNT(*) FROM study_image WHERE study_id=%s AND view_type IS NOT NULL", (job.study_id,))[0][0] == 4
    sj = repo.claim_score("score-1", lease_seconds=900)
    assert sj.study_id == job.study_id and sj.attempts == 1 and list(sj.view_paths) == list(VIEWS)
    assert repo.claim_score("score-2", lease_seconds=900) is None
    assert repo.schedule_retry(job.study_id, "score-1", delay_seconds=3600, error="TransientError") is True
    assert repo.claim_score("score-1", lease_seconds=900) is None, "next_attempt_at in the future"
    sql("UPDATE study SET next_attempt_at = NOW(6) - INTERVAL 1 SECOND WHERE id=%s", (job.study_id,))
    assert repo.claim_score("score-1", lease_seconds=900).attempts == 2
    sql("UPDATE study SET lease_until = NOW(6) - INTERVAL 1 SECOND WHERE id=%s", (job.study_id,))
    sj = repo.claim_score("score-2", lease_seconds=900)
    assert sj.study_id == job.study_id and sj.attempts == 3
    scores = {"Year 1": 0.1, "Year 2": 0.2, "Year 3": 0.3, "Year 4": 0.4, "Year 5": 0.5}
    assert repo.mark_done(job.study_id, "score-2", scores) is True

    # A DONE study is never reopened: a later image of it is recorded as skipped.
    assert repo.register_image(batch_id, h["p1"], "late/extra.dcm", 1, "L_CC") == "extra"
    assert sql("SELECT status FROM study WHERE id=%s", (job.study_id,))[0][0] == "DONE"

    # Crawl end: studies still missing views become INCOMPLETE; totals include skipped files.
    assert repo.finish_batch(batch_id, "crawler-2") is True
    assert study_row(h["inc"])[1:] == ("INCOMPLETE", "partial", "missing views: L_MLO, R_CC, R_MLO")
    assert sql("SELECT status, total_studies, total_images, total_skipped FROM batch WHERE id=%s", (batch_id,))[0] == (
        BatchStatus.CRAWLED.value, 4, 14, 2)

    report = repo.status_report(batch_id)
    assert report["counts"]["DONE"] == 1 and report["counts"]["INCOMPLETE"] == 2
    assert report["done"] == [{"study_id": job.study_id, "pseudonym": h["p1"][:12], "folder": "p1", "scores": scores}]
    assert {p["pseudonym"] for p in report["problems"]} == {h["p2"][:12], h["inc"][:12]}

    data = repo.report_data(batch_id)
    assert data["batch"]["total_skipped"] == 2 and len(data["studies"]) == 4
    assert sorted((s["folder"], s["file_name"], s["reason"]) for s in data["skipped"]) == [
        ("junk", "leeme.txt", "not DICOM"), ("late", "extra.dcm", "extra image after study completed")]
    assert repo.report_data(999999) is None
    raw.close()
finally:
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{test_db}`")
    admin.close()

print("OK: MySQL repository claim semantics passed")
