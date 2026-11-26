"""Standalone checks for ingest.domain (run with: python test_ingest_domain.py). No pytest in this project."""
import hashlib
import hmac

from ingest.domain import (
    MAX_SCORE_ATTEMPTS,
    VIEWS,
    StudyStatus,
    common_folder,
    decide_score_retry,
    hash_uid,
    looks_like_dicom,
    missing_views,
    path_hash,
    pseudonym,
    split_path,
)

# path_hash: sha256 of the root-relative path, normalized so the same file always maps to the same key.
expected = hashlib.sha256(b"PATIENT_X/sub").hexdigest()
assert path_hash("PATIENT_X/sub") == expected
assert path_hash("/PATIENT_X/sub/") == expected, "leading/trailing slashes must not change the key"
assert path_hash("PATIENT_X\\sub") == expected, "windows separators must not change the key"
assert len(path_hash("anything")) == 64

# Folder helpers: common parent of a study's images ("." for the root), folder + file name of a path.
assert common_folder(["2026-11/0012/a/x1.dcm", "2026-11/0012/b/x2.dcm"]) == "2026-11/0012"
assert common_folder(["2026-11/0012/a/x1.dcm", "2026-11/0012-extra/x3.dcm"]) == "2026-11"
assert common_folder(["a/x.dcm"]) == "a"
assert common_folder(["x.dcm", "a/y.dcm"]) == "."
assert common_folder([]) == "."
assert split_path("a/b/c.dcm") == ("a/b", "c.dcm")
assert split_path("c.dcm") == (".", "c.dcm")
assert pseudonym("abcdef0123456789" * 4) == "abcdef012345"

# Study key: HMAC-SHA256(PSEUDONYM_SECRET, "study:" + StudyInstanceUID), never a plain hash of the UID.
secret = "s" * 32
assert hash_uid("1.2.3", secret) == hmac.new(secret.encode(), b"study:1.2.3", hashlib.sha256).hexdigest()
assert hash_uid("1.2.3", secret) != hashlib.sha256(b"1.2.3").hexdigest()
assert hash_uid("1.2.3", secret) != hash_uid("1.2.3", "t" * 32)

# looks_like_dicom: DICM marker at offset 128.
assert looks_like_dicom(b"\x00" * 128 + b"DICM")
assert looks_like_dicom(b"\x00" * 128 + b"DICM" + b"rest")
assert not looks_like_dicom(b"\x00" * 128 + b"NOPE")
assert not looks_like_dicom(b"short")
assert not looks_like_dicom(b"")

assert VIEWS == ("L_CC", "L_MLO", "R_CC", "R_MLO")
assert missing_views(["R_MLO", "L_CC"]) == ["L_MLO", "R_CC"]
assert missing_views(VIEWS) == []

assert {s.value for s in StudyStatus} == {
    "COLLECTING", "DISCOVERED", "FETCHING", "READY", "SCORING", "DONE", "INCOMPLETE", "FAILED",
}

# Retry classification: transient errors back off exponentially until MAX_SCORE_ATTEMPTS, permanent fail at once.
assert MAX_SCORE_ATTEMPTS == 3
d1 = decide_score_retry(attempts=1, transient=True)
d2 = decide_score_retry(attempts=2, transient=True)
d3 = decide_score_retry(attempts=3, transient=True)
assert d1.status == StudyStatus.READY and d2.status == StudyStatus.READY
assert d2.delay_seconds == 2 * d1.delay_seconds > 0, "backoff must be exponential"
assert d3.status == StudyStatus.FAILED
assert decide_score_retry(attempts=1, transient=False).status == StudyStatus.FAILED

print("OK: ingest.domain checks passed")
