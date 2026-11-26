"""Ports: the interfaces the workers depend on. Adapters implement them."""
from typing import BinaryIO, Optional, Protocol, Tuple

from .domain import BatchJob, FetchJob, ScoreJob

ByteRange = Tuple[int, int]  # inclusive (start, end), like an HTTP Range header


class StudySource(Protocol):
    """A tree of patient folders. Paths are relative to the source root ("" is the root)."""

    def list_dir(self, path: str) -> list:  # list[Entry]
        ...

    def open(self, path: str, byte_range: Optional[ByteRange] = None) -> BinaryIO:
        ...

    # Optional: `display_path(path) -> str` gives a human-readable path for reports (e.g. percent-
    # decoded for HTTP). When absent, the path itself is shown.


class RiskModel(Protocol):
    def score(self, dicom_paths: list) -> dict:
        """Score one exam (4 views in L_CC, L_MLO, R_CC, R_MLO order); returns {"Year 1": p, ..., "Year 5": p}.

        Raises domain.TransientError or domain.PermanentError.
        """
        ...


class StudyRepository(Protocol):
    """State store and job queue. Every state change after a claim checks the lease owner and
    returns False when the caller no longer owns the row."""

    # batches
    def create_batch(self, source_root: str) -> int: ...
    def claim_batch(self, worker_id: str, lease_seconds: int) -> Optional[BatchJob]: ...
    def renew_batch_lease(self, batch_id: int, worker_id: str, lease_seconds: int) -> bool: ...
    def seen_paths(self, batch_id: int) -> set: ...  # path hashes of registered images + skipped files
    def register_image(self, batch_id: int, study_uid_hash: str, source_path: str, size: Optional[int],
                       view: str, folder: Optional[str] = None) -> str: ...  # added|queued|duplicate|extra
    def register_skipped(self, batch_id: int, source_path: str, reason: str, folder: Optional[str] = None,
                         file_name: Optional[str] = None) -> bool: ...
    def finish_batch(self, batch_id: int, worker_id: str) -> bool: ...  # COLLECTING -> INCOMPLETE

    # fetch stage
    def claim_fetch(self, worker_id: str, lease_seconds: int, max_staged: int) -> Optional[FetchJob]: ...
    def mark_ready(self, study_id: int, worker_id: str, study_uid_hash: str, anon_id: str,
                   selections: dict) -> bool: ...
    def mark_incomplete(self, study_id: int, worker_id: str, error: str,
                        study_uid_hash: Optional[str] = None) -> bool: ...

    # score stage
    def claim_score(self, worker_id: str, lease_seconds: int) -> Optional[ScoreJob]: ...
    def mark_done(self, study_id: int, worker_id: str, scores: dict) -> bool: ...
    def schedule_retry(self, study_id: int, worker_id: str, delay_seconds: float, error: str) -> bool: ...
    def requeue_fetch(self, study_id: int, worker_id: str, error: str) -> bool: ...

    # any stage
    def mark_failed(self, study_id: int, worker_id: str, error: str) -> bool: ...
    def status_report(self, batch_id: Optional[int] = None) -> dict: ...
    def report_data(self, batch_id: int) -> Optional[dict]: ...  # every study + skipped file, for reports
