"""Use cases shared by the CLI and the HTTP API, so both validate and report the same way."""
import os
from typing import Optional

from .sources import SourcePolicy, SourceRejected, is_url, validate_source


def create_batch(repo, source: str, policy: SourcePolicy) -> int:
    """Validate the source (allowlist / scheme / dicom root) and queue it. Raises SourceRejected."""
    root = validate_source(source, policy)
    if not is_url(root) and not os.path.isdir(root):
        raise SourceRejected(f"not a directory: {root}")
    return repo.create_batch(root)


def batch_status(repo, batch_id: int) -> Optional[dict]:
    """Batch progress without PHI or source paths: status, counts per study status, DONE scores and
    INCOMPLETE/FAILED reasons by internal study id. None when the batch does not exist."""
    report = repo.status_report(batch_id)
    if not report["batches"]:
        return None
    batch = report["batches"][0]
    return dict(
        batch_id=batch["batch_id"],
        status=batch["status"],
        total_studies=batch["total_studies"],
        total_images=batch["total_images"],
        total_skipped=batch["total_skipped"],
        counts=report["counts"],
        done=report["done"],
        problems=report["problems"],
    )
