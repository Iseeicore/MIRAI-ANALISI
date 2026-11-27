"""Fetch worker: stages one study, pseudonymizes it and assembles the 4 views from DICOM headers.

Layout per study: <staging>/<study_id>/raw (deleted right after anonymization) and
<staging>/<study_id>/anon (kept until scoring finishes). Outcome: READY with the selected view
files, or INCOMPLETE (staging cleaned) with a PHI-free reason.
"""
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

import pipeline  # also puts RETROSPECTIVE/ on sys.path
from anonymize_dicoms import anonymize_folder

from ..domain import SKIP_UNREADABLE_AT_FETCH, VIEWS, missing_views, split_path

log = logging.getLogger("ingest.fetch")

COPY_CHUNK_BYTES = 1024 * 1024
ERROR_NO_READABLE = "no readable DICOM images"


@dataclass
class FetchOutcome:
    ready: bool
    error: Optional[str] = None
    anon_id: Optional[str] = None
    selections: dict = field(default_factory=dict)  # view -> (image_id, staged path relative to staging dir)
    failed_images: list = field(default_factory=list)  # StudyImage that the anonymizer could not read


def _incomplete(error, failed=None) -> FetchOutcome:
    return FetchOutcome(ready=False, error=error, failed_images=failed or [])


def _copy_images(job, source, raw_dir: Path) -> int:
    copied = 0
    for image in job.images:
        target = raw_dir / f"{image.id}.dcm"  # named by image id so anonymizer failures map back
        try:
            with source.open(image.source_path) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, COPY_CHUNK_BYTES)
            copied += 1
        except OSError as exc:
            log.warning("study %s image %s: copy failed (%s)", job.study_id, image.id, type(exc).__name__)
            target.unlink(missing_ok=True)
    return copied


def _failed_images(job, failures) -> list:
    """Map anonymizer failures (staged raw/<image_id>.dcm paths) back to the study's images."""
    by_id = {image.id: image for image in job.images}
    failed = []
    for failed_path, _reason in failures:
        stem = Path(failed_path).stem
        if stem.isdigit() and int(stem) in by_id:
            failed.append(by_id[int(stem)])
    return failed


def assemble_study(job, source, staging_dir: Path, secret: str) -> FetchOutcome:
    study_dir = staging_dir / str(job.study_id)
    raw_dir, anon_dir = study_dir / "raw", study_dir / "anon"
    shutil.rmtree(study_dir, ignore_errors=True)  # leftovers from a crashed attempt
    raw_dir.mkdir(parents=True)

    if job.images and _copy_images(job, source, raw_dir) == 0:
        # Every download failed: the source is unreachable, not the study incomplete.
        raise OSError("no image could be copied from the source")

    try:
        result = anonymize_folder(raw_dir, anon_dir, secret=secret)  # file by file, failures collected
    except FileNotFoundError:
        return _incomplete(ERROR_NO_READABLE)
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)
    failed = _failed_images(job, result.failures)
    if failed:
        log.warning("study %s: %d unreadable image(s) ignored", job.study_id, len(failed))

    scanned = pipeline.scan_folder(str(anon_dir), max_workers=4)
    mammo = scanned[(scanned["Modality"] == "MG") & scanned["type"].isin(VIEWS)]
    if mammo.empty:
        return _incomplete(f"missing views: {', '.join(VIEWS)}", failed=failed)

    exams, _dropped, _stats = pipeline.sanitize(scanned)
    exam = exams.iloc[0]
    missing = missing_views(v for v in VIEWS if pd.notna(exam[v]))
    if missing:
        return _incomplete(f"missing views: {', '.join(missing)}", failed=failed)

    selections = {}
    for view in VIEWS:
        staged = Path(exam[view])
        selections[view] = (int(staged.stem), staged.relative_to(staging_dir).as_posix())
    return FetchOutcome(ready=True, anon_id=str(exam["patientid"]), selections=selections, failed_images=failed)


def _record_failed_images(repo, source, job, failed):
    display = getattr(source, "display_path", None)
    for image in failed:
        folder, file_name = split_path(display(image.source_path) if display else image.source_path)
        repo.register_skipped(job.batch_id, image.source_path, SKIP_UNREADABLE_AT_FETCH, folder, file_name)


def fetch_next_study(repo, source_factory, staging_dir: str, worker_id: str, lease_seconds: int,
                     max_staged: int, *, secret: str) -> bool:
    """Claim and process one study. Returns False when nothing was claimable (or staging is full).
    `secret` (PSEUDONYM_SECRET) keys the deterministic pseudonyms written into the staged files."""
    job = repo.claim_fetch(worker_id, lease_seconds, max_staged)
    if job is None:
        return False
    staging = Path(staging_dir)
    study_dir = staging / str(job.study_id)
    try:
        source = source_factory(job.source_root)
        outcome = assemble_study(job, source, staging, secret)
    except Exception as exc:
        log.error("study %s: fetch failed (%s)", job.study_id, type(exc).__name__)
        shutil.rmtree(study_dir, ignore_errors=True)
        repo.mark_failed(job.study_id, worker_id, f"fetch failed: {type(exc).__name__}")
        return True

    _record_failed_images(repo, source, job, outcome.failed_images)
    if outcome.ready:
        saved = repo.mark_ready(job.study_id, worker_id, None, outcome.anon_id, outcome.selections)
        log.info("study %s: READY", job.study_id)
    else:
        shutil.rmtree(study_dir, ignore_errors=True)
        saved = repo.mark_incomplete(job.study_id, worker_id, outcome.error)
        log.info("study %s: INCOMPLETE (%s)", job.study_id, outcome.error)
    if not saved:
        log.warning("study %s: lease lost before saving the outcome", job.study_id)
    return True
