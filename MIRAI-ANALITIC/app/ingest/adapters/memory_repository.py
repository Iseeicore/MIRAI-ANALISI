"""In-memory StudyRepository with an injectable clock. Used by unit tests; mirrors the MySQL
adapter's rules (studies keyed by StudyInstanceUID hash, idempotent image registration, leases,
backpressure, retry gating) without a database."""
import itertools
import time
from typing import Optional

from ..domain import (
    SCORE_YEARS, SKIP_EXTRA_AFTER_COMPLETED, VIEWS, BatchJob, BatchStatus, FetchJob, ScoreJob,
    StudyImage, StudyStatus, common_parent, missing_views, path_hash, pseudonym, split_path,
)

CLOSED_FOR_NEW_IMAGES = (StudyStatus.DONE, StudyStatus.SCORING)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float):
        self._now += seconds


class _SystemClock:
    @staticmethod
    def now() -> float:
        return time.time()


class MemoryRepository:
    def __init__(self, clock=None):
        self.clock = clock or _SystemClock()
        self.batches = {}
        self.studies = {}
        self.images = {}
        self.skipped = []
        self._ids = itertools.count(1)

    # helpers -------------------------------------------------------------
    def images_of(self, study_id: int) -> list:
        return [i for i in self.images.values() if i["study_id"] == study_id]

    def studies_of(self, batch_id: int) -> list:
        return [s for s in self.studies.values() if s["batch_id"] == batch_id]

    def skipped_of(self, batch_id: int) -> list:
        return [s for s in self.skipped if s["batch_id"] == batch_id]

    def _lease_expired(self, row) -> bool:
        return row["lease_until"] is None or row["lease_until"] < self.clock.now()

    def _owned(self, study_id: int, worker_id: str, status: StudyStatus):
        row = self.studies.get(study_id)
        if row and row["status"] == status and row["locked_by"] == worker_id:
            return row
        return None

    def _release(self, row, status: StudyStatus, error: Optional[str]):
        row.update(status=status, error=error, locked_by=None, lease_until=None)

    # batches -------------------------------------------------------------
    def create_batch(self, source_root: str) -> int:
        batch_id = next(self._ids)
        self.batches[batch_id] = dict(id=batch_id, source_root=source_root, status=BatchStatus.PENDING,
                                      locked_by=None, lease_until=None, total_studies=0, total_images=0,
                                      total_skipped=0)
        return batch_id

    def claim_batch(self, worker_id: str, lease_seconds: int) -> Optional[BatchJob]:
        for b in sorted(self.batches.values(), key=lambda b: b["id"]):
            if b["status"] == BatchStatus.PENDING or (
                    b["status"] == BatchStatus.CRAWLING and self._lease_expired(b)):
                b.update(status=BatchStatus.CRAWLING, locked_by=worker_id,
                         lease_until=self.clock.now() + lease_seconds)
                return BatchJob(b["id"], b["source_root"])
        return None

    def renew_batch_lease(self, batch_id: int, worker_id: str, lease_seconds: int) -> bool:
        b = self.batches[batch_id]
        if b["status"] != BatchStatus.CRAWLING or b["locked_by"] != worker_id:
            return False
        b["lease_until"] = self.clock.now() + lease_seconds
        return True

    def seen_paths(self, batch_id: int) -> set:
        return ({i["path_hash"] for i in self.images.values() if i["batch_id"] == batch_id}
                | {s["path_hash"] for s in self.skipped_of(batch_id)})

    def register_skipped(self, batch_id, source_path, reason, folder=None, file_name=None) -> bool:
        key = path_hash(source_path)
        if any(s["path_hash"] == key for s in self.skipped_of(batch_id)):
            return False
        default_folder, default_name = split_path(source_path)
        self.skipped.append(dict(batch_id=batch_id, path_hash=key, folder=folder or default_folder,
                                 file_name=file_name or default_name, reason=reason))
        return True

    def register_image(self, batch_id, study_uid_hash, source_path, size, view, folder=None) -> str:
        """Returns "added", "queued" (4th distinct view: now claimable), "duplicate" or "extra"."""
        key = path_hash(source_path)
        if key in self.seen_paths(batch_id):
            return "duplicate"
        folder = folder or split_path(source_path)[0]
        study = next((s for s in self.studies_of(batch_id) if s["study_uid_hash"] == study_uid_hash), None)
        if study is None:
            study_id = next(self._ids)
            study = self.studies[study_id] = dict(
                id=study_id, batch_id=batch_id, study_uid_hash=study_uid_hash, folder=folder, anon_id=None,
                status=StudyStatus.COLLECTING, error=None, attempts=0, locked_by=None, lease_until=None,
                next_attempt_at=None, **{f"score_year_{n}": None for n in range(1, 6)})
        elif study["status"] in CLOSED_FOR_NEW_IMAGES:
            self.register_skipped(batch_id, source_path, SKIP_EXTRA_AFTER_COMPLETED, folder, split_path(source_path)[1])
            return "extra"
        else:
            study["folder"] = common_parent([study["folder"], folder])
        image_id = next(self._ids)
        self.images[image_id] = dict(id=image_id, study_id=study["id"], batch_id=batch_id, source_path=source_path,
                                     path_hash=key, size=size, view_type=view, staged_path=None, selected=False)
        if study["status"] == StudyStatus.COLLECTING and not missing_views(
                i["view_type"] for i in self.images_of(study["id"])):
            study["status"] = StudyStatus.DISCOVERED
            return "queued"
        return "added"

    def finish_batch(self, batch_id: int, worker_id: str) -> bool:
        b = self.batches[batch_id]
        if b["locked_by"] != worker_id:
            return False
        for s in self.studies_of(batch_id):
            if s["status"] == StudyStatus.COLLECTING:
                missing = missing_views(i["view_type"] for i in self.images_of(s["id"]))
                s.update(status=StudyStatus.INCOMPLETE, error=f"missing views: {', '.join(missing)}")
        study_ids = [s["id"] for s in self.studies_of(batch_id)]
        b.update(status=BatchStatus.CRAWLED, locked_by=None, lease_until=None, total_studies=len(study_ids),
                 total_images=sum(len(self.images_of(sid)) for sid in study_ids),
                 total_skipped=len(self.skipped_of(batch_id)))
        return True

    # fetch ---------------------------------------------------------------
    def _staged_count(self) -> int:
        return sum(1 for s in self.studies.values()
                   if s["status"] in (StudyStatus.READY, StudyStatus.SCORING)
                   or (s["status"] == StudyStatus.FETCHING and not self._lease_expired(s)))

    def claim_fetch(self, worker_id: str, lease_seconds: int, max_staged: int) -> Optional[FetchJob]:
        if self._staged_count() >= max_staged:
            return None
        for s in sorted(self.studies.values(), key=lambda s: s["id"]):
            if s["status"] == StudyStatus.DISCOVERED or (
                    s["status"] == StudyStatus.FETCHING and self._lease_expired(s)):
                s.update(status=StudyStatus.FETCHING, locked_by=worker_id,
                         lease_until=self.clock.now() + lease_seconds)
                images = [StudyImage(i["id"], i["source_path"], i["size"], i["view_type"])
                          for i in sorted(self.images_of(s["id"]), key=lambda i: i["id"])]
                return FetchJob(s["id"], s["batch_id"], self.batches[s["batch_id"]]["source_root"], images)
        return None

    def mark_ready(self, study_id, worker_id, study_uid_hash, anon_id, selections) -> bool:
        row = self._owned(study_id, worker_id, StudyStatus.FETCHING)
        if not row:
            return False
        for img in self.images_of(study_id):
            img.update(staged_path=None, selected=False)
        for view, (image_id, staged_path) in selections.items():
            self.images[image_id].update(view_type=view, staged_path=staged_path, selected=True)
        row.update(anon_id=anon_id, attempts=0, next_attempt_at=None)  # the UID hash is the study key: never rewritten
        self._release(row, StudyStatus.READY, None)
        return True

    def mark_incomplete(self, study_id, worker_id, error, study_uid_hash=None) -> bool:
        row = self._owned(study_id, worker_id, StudyStatus.FETCHING)
        if not row:
            return False
        self._release(row, StudyStatus.INCOMPLETE, error)
        return True

    # score ---------------------------------------------------------------
    def claim_score(self, worker_id: str, lease_seconds: int) -> Optional[ScoreJob]:
        now = self.clock.now()
        for s in sorted(self.studies.values(), key=lambda s: s["id"]):
            ready = s["status"] == StudyStatus.READY and (s["next_attempt_at"] is None or s["next_attempt_at"] <= now)
            stale = s["status"] == StudyStatus.SCORING and self._lease_expired(s)
            if ready or stale:
                s.update(status=StudyStatus.SCORING, locked_by=worker_id, lease_until=now + lease_seconds,
                         attempts=s["attempts"] + 1)
                selected = {i["view_type"]: i["staged_path"] for i in self.images_of(s["id"]) if i["selected"]}
                return ScoreJob(s["id"], s["attempts"], {v: selected.get(v) for v in VIEWS})
        return None

    def mark_done(self, study_id, worker_id, scores) -> bool:
        row = self._owned(study_id, worker_id, StudyStatus.SCORING)
        if not row:
            return False
        for n, year in enumerate(SCORE_YEARS, start=1):
            row[f"score_year_{n}"] = scores.get(year)
        self._release(row, StudyStatus.DONE, None)
        return True

    def schedule_retry(self, study_id, worker_id, delay_seconds, error) -> bool:
        row = self._owned(study_id, worker_id, StudyStatus.SCORING)
        if not row:
            return False
        row["next_attempt_at"] = self.clock.now() + delay_seconds
        self._release(row, StudyStatus.READY, error)
        return True

    def requeue_fetch(self, study_id, worker_id, error) -> bool:
        row = self._owned(study_id, worker_id, StudyStatus.SCORING)
        if not row:
            return False
        row.update(attempts=0, next_attempt_at=None)
        self._release(row, StudyStatus.DISCOVERED, error)
        return True

    def mark_failed(self, study_id, worker_id, error) -> bool:
        row = self.studies.get(study_id)
        if not row or row["locked_by"] != worker_id:
            return False
        self._release(row, StudyStatus.FAILED, error)
        return True

    # reporting -----------------------------------------------------------
    @staticmethod
    def _scores(s) -> dict:
        return {y: s[f"score_year_{n}"] for n, y in enumerate(SCORE_YEARS, 1)}

    def _batch_summary(self, b) -> dict:
        return dict(batch_id=b["id"], status=BatchStatus(b["status"]).value, total_studies=b["total_studies"],
                    total_images=b["total_images"], total_skipped=b["total_skipped"])

    def _counts(self, rows) -> dict:
        counts = {status.value: 0 for status in StudyStatus}
        for s in rows:
            counts[StudyStatus(s["status"]).value] += 1
        return counts

    def status_report(self, batch_id: Optional[int] = None) -> dict:
        rows = sorted((s for s in self.studies.values() if batch_id is None or s["batch_id"] == batch_id),
                      key=lambda s: s["id"])
        done = [dict(study_id=s["id"], pseudonym=pseudonym(s["study_uid_hash"]), folder=s["folder"],
                     scores=self._scores(s))
                for s in rows if s["status"] == StudyStatus.DONE]
        problems = [dict(study_id=s["id"], pseudonym=pseudonym(s["study_uid_hash"]), folder=s["folder"],
                         status=StudyStatus(s["status"]).value, error=s["error"])
                    for s in rows if s["status"] in (StudyStatus.INCOMPLETE, StudyStatus.FAILED)]
        batches = [self._batch_summary(b) for b in sorted(self.batches.values(), key=lambda b: b["id"])
                   if batch_id is None or b["id"] == batch_id]
        return dict(counts=self._counts(rows), done=done, problems=problems, batches=batches)

    def report_data(self, batch_id: int) -> Optional[dict]:
        b = self.batches.get(batch_id)
        if b is None:
            return None
        rows = sorted(self.studies_of(batch_id), key=lambda s: s["id"])
        studies = [dict(study_id=s["id"], pseudonym=pseudonym(s["study_uid_hash"]), folder=s["folder"],
                        status=StudyStatus(s["status"]).value, scores=self._scores(s), error=s["error"])
                   for s in rows]
        skipped = [dict(folder=s["folder"], file_name=s["file_name"], reason=s["reason"])
                   for s in self.skipped_of(batch_id)]
        summary = self._batch_summary(b)
        summary["total_images"] = sum(len(self.images_of(s["id"])) for s in rows)  # live, also while crawling
        summary["total_skipped"] = len(skipped)
        summary["total_studies"] = len(rows)
        return dict(batch=summary, counts=self._counts(rows), studies=studies, skipped=skipped)
