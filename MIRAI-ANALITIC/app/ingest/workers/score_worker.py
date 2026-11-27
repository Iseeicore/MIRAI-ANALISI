"""Score worker: sends one READY study to the RiskModel and records the outcome.

DONE (scores stored, staging deleted); transient error -> READY again with exponential backoff, up
to MAX_SCORE_ATTEMPTS, then FAILED; permanent error -> FAILED. Staging is deleted on FAILED too.
If the staged files are gone (e.g. the staging volume was wiped) the study goes back to DISCOVERED.
"""
import logging
import shutil
from pathlib import Path

from ..domain import VIEWS, PermanentError, StudyStatus, decide_score_retry

log = logging.getLogger("ingest.score")

MAX_ERROR_LENGTH = 500


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


def score_next_study(repo, model, staging_dir: str, worker_id: str, lease_seconds: int) -> bool:
    """Claim and score one study. Returns False when nothing was claimable."""
    job = repo.claim_score(worker_id, lease_seconds)
    if job is None:
        return False
    staging = Path(staging_dir)
    study_dir = staging / str(job.study_id)
    paths = [staging / job.view_paths[v] if job.view_paths.get(v) else None for v in VIEWS]

    if any(p is None or not p.is_file() for p in paths):
        repo.requeue_fetch(job.study_id, worker_id, "staged files missing, re-fetching")
        log.warning("study %s: staged files missing, sent back to fetch", job.study_id)
        return True

    try:
        scores = model.score([str(p) for p in paths])
    except Exception as exc:  # transient unless the adapter says it is permanent
        decision = decide_score_retry(job.attempts, transient=not isinstance(exc, PermanentError))
        if decision.status == StudyStatus.FAILED:
            repo.mark_failed(job.study_id, worker_id, _error_text(exc))
            shutil.rmtree(study_dir, ignore_errors=True)
            log.error("study %s: FAILED after %d attempt(s) (%s)", job.study_id, job.attempts, type(exc).__name__)
        else:
            repo.schedule_retry(job.study_id, worker_id, decision.delay_seconds, _error_text(exc))
            log.warning("study %s: attempt %d failed (%s), retry in %ss",
                        job.study_id, job.attempts, type(exc).__name__, decision.delay_seconds)
        return True

    if repo.mark_done(job.study_id, worker_id, scores):
        shutil.rmtree(study_dir, ignore_errors=True)
        log.info("study %s: DONE", job.study_id)
    else:
        log.warning("study %s: lease lost before saving scores", job.study_id)
    return True
