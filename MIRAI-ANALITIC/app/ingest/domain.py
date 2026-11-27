"""Pure domain types and rules for batch ingestion. No I/O here."""
import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

VIEWS = ("L_CC", "L_MLO", "R_CC", "R_MLO")
SCORE_YEARS = ("Year 1", "Year 2", "Year 3", "Year 4", "Year 5")

DICOM_MARKER = b"DICM"
DICOM_MARKER_OFFSET = 128
DICOM_HEAD_LENGTH = DICOM_MARKER_OFFSET + len(DICOM_MARKER)

MAX_SCORE_ATTEMPTS = 3
RETRY_BASE_SECONDS = 60

HEADER_READ_BYTES = 64 * 1024  # first ranged read of every file during the crawl
HEADER_RETRY_BYTES = 1024 * 1024  # one retry when the header did not fit in the first read
PSEUDONYM_LENGTH = 12

# Reasons a file is recorded in skipped_file (English, shown as-is in reports).
SKIP_NOT_DICOM = "not DICOM"
SKIP_UNREADABLE_HEADER = "unreadable header"
SKIP_MISSING_UID = "missing StudyInstanceUID"
SKIP_MISSING_VIEW = "missing view tags"
SKIP_READ_ERROR = "read error"
SKIP_EXTRA_AFTER_COMPLETED = "extra image after study completed"
SKIP_UNREADABLE_AT_FETCH = "unreadable DICOM at fetch"


def skip_not_mg(modality) -> str:
    return f"not MG (Modality={modality or 'missing'})"


def skip_unsupported_view(view) -> str:
    return f"unsupported view {view}"


class StudyStatus(str, Enum):
    COLLECTING = "COLLECTING"  # crawl still looking for its 4 views; not claimable yet
    DISCOVERED = "DISCOVERED"
    FETCHING = "FETCHING"
    READY = "READY"
    SCORING = "SCORING"
    DONE = "DONE"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


class BatchStatus(str, Enum):
    PENDING = "PENDING"
    CRAWLING = "CRAWLING"
    CRAWLED = "CRAWLED"


class TransientError(Exception):
    """A failure worth retrying later (timeout, connection error, server 5xx)."""


class PermanentError(Exception):
    """A failure that will not go away by retrying (client 4xx, malformed response)."""


@dataclass(frozen=True)
class Entry:
    """One item listed by a StudySource. `path` is relative to the source root, POSIX style.
    `size` is None when the source does not report it (e.g. HTML autoindex pages)."""
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None


@dataclass(frozen=True)
class BatchJob:
    id: int
    source_root: str


@dataclass(frozen=True)
class StudyImage:
    id: int
    source_path: str
    size: Optional[int]
    view_type: Optional[str] = None  # view read from the header at crawl time


@dataclass(frozen=True)
class FetchJob:
    study_id: int
    batch_id: int
    source_root: str
    images: list = field(default_factory=list)  # list[StudyImage]


@dataclass(frozen=True)
class ScoreJob:
    study_id: int
    attempts: int
    view_paths: dict  # view -> staged path relative to the staging dir, in VIEWS order


@dataclass(frozen=True)
class RetryDecision:
    status: StudyStatus
    delay_seconds: Optional[float] = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_rel_path(rel_path: str) -> str:
    return rel_path.replace("\\", "/").strip("/")


def path_hash(rel_path: str) -> str:
    """Stable key for a file within a batch (idempotent registration): sha256 of its root-relative path."""
    return _sha256(normalize_rel_path(rel_path))


def split_path(rel_path: str) -> tuple:
    """(folder, file name) of a root-relative path; the root folder is "."."""
    folder, _, name = normalize_rel_path(rel_path).rpartition("/")
    return (folder or ".", name)


def common_folder(rel_paths: Iterable[str]) -> str:
    """Common parent folder of a study's images, relative to the root ("." for the root itself)."""
    return common_parent(split_path(p)[0] for p in rel_paths)


def common_parent(folders: Iterable[str]) -> str:
    """Longest common folder of root-relative folders ("." for the root)."""
    folders = [f for f in folders if f is not None]
    if not folders or "." in folders:
        return "."
    parts = [f.split("/") for f in folders]
    common = []
    for segments in zip(*parts):
        if len(set(segments)) != 1:
            break
        common.append(segments[0])
    return "/".join(common) or "."


def pseudonym(study_uid_hash: str) -> str:
    """Short, non-reversible label for a study in reports: the first 12 hex chars of its UID hash."""
    return (study_uid_hash or "")[:PSEUDONYM_LENGTH]


def hash_uid(uid: str, secret: str) -> str:
    """Study key: HMAC-SHA256(PSEUDONYM_SECRET, "study:" + StudyInstanceUID), hex. Keyed, so the
    original UID cannot be confirmed by hashing a guess; stable for a given secret."""
    return hmac.new(secret.encode("utf-8"), f"study:{str(uid).strip()}".encode("utf-8"), hashlib.sha256).hexdigest()


def looks_like_dicom(head: bytes) -> bool:
    """True when the bytes carry the DICOM Part 10 marker ("DICM" after the 128-byte preamble)."""
    return len(head) >= DICOM_HEAD_LENGTH and head[DICOM_MARKER_OFFSET:DICOM_HEAD_LENGTH] == DICOM_MARKER


def missing_views(present: Iterable[str]) -> list:
    present = set(present)
    return [v for v in VIEWS if v not in present]


def decide_score_retry(attempts: int, transient: bool) -> RetryDecision:
    """What to do after a failed scoring attempt. `attempts` already counts the attempt that failed."""
    if not transient or attempts >= MAX_SCORE_ATTEMPTS:
        return RetryDecision(StudyStatus.FAILED)
    return RetryDecision(StudyStatus.READY, RETRY_BASE_SECONDS * 2 ** (attempts - 1))
