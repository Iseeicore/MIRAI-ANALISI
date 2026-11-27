"""Use cases shared by the CLI and the HTTP API, so both validate and report the same way."""
import os
from typing import Optional
from urllib.parse import urlsplit

from .sources import SourcePolicy, SourceUnavailable, is_url, make_source, validate_source

PROBE_TIMEOUT_SECONDS = 10  # same as the connect timeout: the submit request never hangs longer
NOT_A_LISTING = "the URL is not a folder listing or is empty"


def _host_port(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


def probe_source(root: str, policy: SourcePolicy):
    """List the root through the same policy as the workers before any batch exists (decision 0004).

    Raises SourceRejected (policy, 400) or SourceUnavailable (unreachable, not a listing, empty: 422).
    """
    if not is_url(root):
        if not os.path.isdir(root):
            raise SourceUnavailable(f"folder does not exist: {root}")
        with os.scandir(root) as entries:
            if next(entries, None) is None:
                raise SourceUnavailable(f"folder is empty: {root}")
        return
    from .adapters.http_index_source import SourceConnectionError, SourceError

    try:
        entries = make_source(root, policy, read_timeout=PROBE_TIMEOUT_SECONDS).list_dir("")
    except SourceConnectionError:
        raise SourceUnavailable(f"could not connect to {_host_port(root)}") from None
    except SourceError:
        raise SourceUnavailable(NOT_A_LISTING) from None
    if not entries:
        raise SourceUnavailable(NOT_A_LISTING)


def create_batch(repo, source: str, policy: SourcePolicy) -> int:
    """Validate (allowlist / networks / scheme / dicom root), probe the root, then queue it.
    Raises SourceRejected or SourceUnavailable; no batch row is created on failure."""
    root = validate_source(source, policy)
    probe_source(root, policy)
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
