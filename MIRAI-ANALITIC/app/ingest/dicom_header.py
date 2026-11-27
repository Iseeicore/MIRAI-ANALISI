"""Classify one file from the first bytes of its DICOM header (decision 0003).

The view rules mirror RETROSPECTIVE/collect_dicoms.scan_file: ImageLaterality with Laterality as
fallback, ViewPosition with ML mapped to MLO, and only Modality MG. scan_file itself cannot be
reused here because it opens a filesystem path, while the crawler only has a ranged read.
"""
import io
import warnings
from dataclasses import dataclass
from typing import Optional

import pydicom

from .domain import (
    DICOM_HEAD_LENGTH, HEADER_READ_BYTES, HEADER_RETRY_BYTES, SKIP_MISSING_UID, SKIP_MISSING_VIEW,
    SKIP_NOT_DICOM, SKIP_UNREADABLE_HEADER, VIEWS, hash_uid, looks_like_dicom, skip_not_mg,
    skip_unsupported_view,
)


@dataclass(frozen=True)
class HeaderInfo:
    study_uid_hash: Optional[str] = None
    view: Optional[str] = None
    skip_reason: Optional[str] = None
    maybe_truncated: bool = False  # the outcome could change with more bytes


def _skip(reason, maybe_truncated=False) -> HeaderInfo:
    return HeaderInfo(skip_reason=reason, maybe_truncated=maybe_truncated)


def classify_header(data: bytes, secret: str) -> HeaderInfo:
    """The original StudyInstanceUID only lives in memory here: only its keyed hash leaves."""
    if not looks_like_dicom(data):
        return _skip(SKIP_NOT_DICOM)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True, force=False)
            modality = ds.get("Modality")
            uid = ds.get("StudyInstanceUID")
            laterality = ds.get("ImageLaterality") or ds.get("Laterality")
            view_position = ds.get("ViewPosition")
    except Exception:
        return _skip(SKIP_UNREADABLE_HEADER, maybe_truncated=True)
    if modality is None and uid is None:  # parsed, but nothing a DICOM image always carries
        return _skip(SKIP_UNREADABLE_HEADER, maybe_truncated=True)
    if modality != "MG":
        return _skip(skip_not_mg(modality), maybe_truncated=modality is None)
    if not uid:
        return _skip(SKIP_MISSING_UID, maybe_truncated=True)
    if not laterality or not view_position:
        return _skip(SKIP_MISSING_VIEW, maybe_truncated=True)
    if view_position == "ML":
        view_position = "MLO"
    view = f"{laterality}_{view_position}"
    if view not in VIEWS:
        return _skip(skip_unsupported_view(view))
    return HeaderInfo(study_uid_hash=hash_uid(str(uid).strip(), secret), view=view)


def _read(source, path: str, length: int) -> bytes:
    with source.open(path, byte_range=(0, length - 1)) as fh:
        return fh.read(length)


def read_header(source, path: str, size: Optional[int], secret: str) -> HeaderInfo:
    """Ranged read of the first 64 KiB; one retry with 1 MiB when the header may not have fit.
    Raises OSError when the source cannot be read."""
    if size is not None and size < DICOM_HEAD_LENGTH:
        return _skip(SKIP_NOT_DICOM)
    data = _read(source, path, HEADER_READ_BYTES)
    info = classify_header(data, secret)
    if info.maybe_truncated and len(data) >= HEADER_READ_BYTES:
        info = classify_header(_read(source, path, HEADER_RETRY_BYTES), secret)
    return info
