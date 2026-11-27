"""Crawler (decision 0003): walks the whole source tree at any depth and groups images by the
StudyInstanceUID read from each file's header, never by folder.

Each image is registered under sha256(StudyInstanceUID); a study becomes claimable as soon as it has
the 4 views. Files that cannot be used are recorded in skipped_file with a reason. Already-registered
paths are skipped without reading them, so a crashed crawl resumes where it stopped. When the walk
ends, studies still missing views become INCOMPLETE (repository.finish_batch).
"""
import logging

from ..dicom_header import read_header
from ..domain import SKIP_READ_ERROR, path_hash, split_path

log = logging.getLogger("ingest.crawler")


def _display(source, path: str) -> str:
    display = getattr(source, "display_path", None)
    return display(path) if display else path


def _register_file(repo, source, batch_id: int, entry, counts: dict, secret: str):
    folder, file_name = split_path(_display(source, entry.path))
    try:
        info = read_header(source, entry.path, entry.size, secret)
    except OSError as exc:
        log.warning("batch %s: unreadable file skipped (%s)", batch_id, type(exc).__name__)
        repo.register_skipped(batch_id, entry.path, SKIP_READ_ERROR, folder, file_name)
        counts["skipped"] += 1
        return
    if info.skip_reason:
        repo.register_skipped(batch_id, entry.path, info.skip_reason, folder, file_name)
        counts["skipped"] += 1
        return
    outcome = repo.register_image(batch_id, info.study_uid_hash, entry.path, entry.size, info.view, folder)
    counts[outcome] = counts.get(outcome, 0) + 1


def crawl_next_batch(repo, source_factory, worker_id: str, lease_seconds: int, *, secret: str) -> bool:
    """Claim one PENDING (or abandoned) batch and crawl it to the end. Returns False if none was claimable.

    `secret` (PSEUDONYM_SECRET) keys the study hash computed from each header."""
    batch = repo.claim_batch(worker_id, lease_seconds)
    if batch is None:
        return False
    source = source_factory(batch.source_root)
    seen = repo.seen_paths(batch.id)
    counts = {"skipped": 0}
    stack = [""]
    while stack:
        current = stack.pop()
        try:
            entries = source.list_dir(current)
        except OSError as exc:
            if current == "":
                raise  # the root itself is unreachable: let the lease expire and retry later
            log.warning("batch %s: unreadable folder skipped (%s)", batch.id, type(exc).__name__)
            continue
        subfolders = []
        for entry in entries:
            if entry.is_dir:
                subfolders.append(entry.path)
            elif path_hash(entry.path) not in seen:
                _register_file(repo, source, batch.id, entry, counts, secret)
        stack.extend(reversed(subfolders))  # reversed so folders are visited in name order
        if not repo.renew_batch_lease(batch.id, worker_id, lease_seconds):
            log.warning("batch %s: lease lost, stopping crawl", batch.id)
            return True
    repo.finish_batch(batch.id, worker_id)
    log.info("batch %s crawled: %s", batch.id, ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return True
