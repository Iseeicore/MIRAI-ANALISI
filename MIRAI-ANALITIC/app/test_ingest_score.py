"""Standalone checks for the score worker and the MIRAI client (run with: python test_ingest_score.py).

No pytest in this project. Uses the in-memory repository, a fake RiskModel and a fake HTTP post.
"""
import tempfile
from pathlib import Path

import requests

from ingest.adapters.memory_repository import FakeClock, MemoryRepository
from ingest.adapters.mirai_client import MiraiClient
from ingest_test_support import TEST_SECRET
from ingest.domain import VIEWS, PermanentError, StudyStatus, TransientError, hash_uid
from ingest.workers.score_worker import score_next_study

SCORES = {"Year 1": 0.01, "Year 2": 0.02, "Year 3": 0.03, "Year 4": 0.04, "Year 5": 0.05}


class FakeRiskModel:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def score(self, dicom_paths):
        self.calls.append(list(dicom_paths))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


staging = Path(tempfile.mkdtemp(prefix="score_test_staging_"))
clock = FakeClock()
repo = MemoryRepository(clock=clock)
batch_id = repo.create_batch("/dicom/fake")


def make_ready_study(name):
    """Drive a study to READY through the public repository API, with fake staged files."""
    for view in VIEWS:
        repo.register_image(batch_id, hash_uid(name, TEST_SECRET), f"{name}/{view}.dcm", 10, view)
    job = repo.claim_fetch("fetcher", lease_seconds=600, max_staged=100)
    selections = {}
    for view, image in zip(VIEWS, job.images):
        rel = f"{job.study_id}/anon/{image.id}.dcm"
        (staging / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging / rel).write_bytes(b"x")
        selections[view] = (image.id, rel)
    repo.mark_ready(job.study_id, "fetcher", study_uid_hash="h" * 64, anon_id="anon", selections=selections)
    return job.study_id


def score(model):
    return score_next_study(repo, model, staging_dir=str(staging), worker_id="scorer", lease_seconds=900)


# Success: DONE, scores stored, views sent in L_CC, L_MLO, R_CC, R_MLO order, staging removed.
ok_id = make_ready_study("OK")
model = FakeRiskModel([SCORES])
assert score(model) is True
row = repo.studies[ok_id]
assert row["status"] == StudyStatus.DONE, row
assert [row[f"score_year_{n}"] for n in range(1, 6)] == [0.01, 0.02, 0.03, 0.04, 0.05]
assert [Path(p).stem for p in model.calls[0]] == [str(i["id"]) for i in sorted(
    (i for i in repo.images_of(ok_id) if i["selected"]), key=lambda i: VIEWS.index(i["view_type"]))]
assert not (staging / str(ok_id)).exists(), "staging must be deleted after DONE"
assert score(FakeRiskModel([])) is False, "nothing left to score"

# Transient errors: exponential backoff, not claimable before next_attempt_at, FAILED after 3 attempts.
retry_id = make_ready_study("RETRY")
model = FakeRiskModel([TransientError("timeout"), TransientError("502"), TransientError("conn reset")])
assert score(model) is True
row = repo.studies[retry_id]
assert row["status"] == StudyStatus.READY and row["attempts"] == 1
first_delay = row["next_attempt_at"] - clock.now()
assert first_delay > 0
assert score(model) is False, "must not retry before next_attempt_at"
clock.advance(first_delay + 1)
assert score(model) is True
row = repo.studies[retry_id]
assert row["status"] == StudyStatus.READY and row["attempts"] == 2
assert abs((row["next_attempt_at"] - clock.now()) - 2 * first_delay) < 1e-6, "backoff must double"
clock.advance(2 * first_delay + 1)
assert score(model) is True
row = repo.studies[retry_id]
assert row["status"] == StudyStatus.FAILED and row["attempts"] == 3, row
assert "TransientError" in row["error"]
assert not (staging / str(retry_id)).exists(), "staging must be deleted after FAILED"

# Permanent error: FAILED at once.
perm_id = make_ready_study("PERM")
assert score(FakeRiskModel([PermanentError("HTTP 400")])) is True
assert repo.studies[perm_id]["status"] == StudyStatus.FAILED
assert repo.studies[perm_id]["attempts"] == 1

# Crashed scorer: SCORING lease expires and another worker resumes.
crash_id = make_ready_study("CRASH")
assert repo.claim_score("dead-scorer", lease_seconds=900).study_id == crash_id
assert score(FakeRiskModel([SCORES])) is False, "active lease must not be stolen"
clock.advance(901)
assert score(FakeRiskModel([SCORES])) is True
assert repo.studies[crash_id]["status"] == StudyStatus.DONE

# Staged files gone (e.g. staging volume wiped): send the study back to fetch instead of failing it.
lost_id = make_ready_study("LOST")
import shutil  # noqa: E402
shutil.rmtree(staging / str(lost_id))
assert score(FakeRiskModel([])) is True
assert repo.studies[lost_id]["status"] == StudyStatus.DISCOVERED

# MiraiClient: same multipart format as pipeline, errors classified transient vs permanent.
dcm_paths = []
for i in range(4):
    p = staging / f"client_{i}.dcm"
    p.write_bytes(b"x")
    dcm_paths.append(str(p))


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def fake_post(result):
    captured = {}

    def post(url, data=None, files=None, timeout=None):
        captured.update(url=url, data=data, fields=[f[0] for f in files], timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result

    return post, captured


post, captured = fake_post(FakeResponse(200, {"data": {"predictions": SCORES}}))
assert MiraiClient("http://mirai/dicom/files", post=post).score(dcm_paths) == SCORES
assert captured["fields"] == ["dicom"] * 4 and "metadata" in captured["data"]
assert captured["timeout"] == (120, 600)


def raises(exc_type, result):
    post, _ = fake_post(result)
    try:
        MiraiClient("http://mirai/dicom/files", post=post).score(dcm_paths)
    except exc_type:
        return True
    return False


assert raises(TransientError, requests.Timeout("slow"))
assert raises(TransientError, requests.ConnectionError("refused"))
assert raises(TransientError, FakeResponse(503))
assert raises(PermanentError, FakeResponse(400))
assert raises(PermanentError, FakeResponse(200, {"data": {}}))

print("OK: score worker and MIRAI client checks passed")
