#!/usr/bin/env python3
"""Pseudonymize mammography DICOM files before any metadata scan or MIRAI
submission. Must run first, so collect_dicoms.py, sanitize_dicoms.py and
run_mirai.py only ever see de-identified copies. Single implementation used by
the CLI, the ingest fetch worker, api.py /ingest and the Streamlit zip mode.

Pseudonyms are deterministic, keyed by the PSEUDONYM_SECRET environment
variable (at least 32 characters, never committed). The same input and secret
always give the same output, so studies of one patient stay linkable across
runs and batches; changing the secret breaks that linkage.

- PatientID and PatientName -> HMAC-SHA256(secret, "patient:" + PatientID),
  hex, 32 chars. Empty PatientID falls back to the StudyInstanceUID with the
  prefix "patient-from-study:". Any other text element equal to the original
  PatientID or PatientName (sequences included) gets the same pseudonym.
- Study/Series/SOP instance UIDs (also file meta MediaStorageSOPInstanceUID,
  FrameOfReferenceUID and references to them) -> "2.25." + int of the first
  128 bits of HMAC(secret, kind + ":" + uid): a valid UID of at most 44 chars.
  Other non-registry UIDs get the kind "uid".
- Direct identifiers (birth date, addresses, physician/operator names...) are
  removed at any nesting level, private tags are removed. Pixel data is never
  touched, so the AI analysis is unaffected.

Kept on purpose (not direct identifiers, or required by the pipeline):
InstitutionName, PatientSex, PatientAge, Study/Acquisition dates and times
(duplicate-view selection depends on them), Manufacturer/ManufacturerModelName.

Files are processed one at a time; a file that cannot be read or written is
reported in the result and never aborts the run. The id mapping is never
printed or returned.
"""
import argparse
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.multival import MultiValue

SECRET_ENV = "PSEUDONYM_SECRET"
MIN_SECRET_LENGTH = 32
PATIENT_ID_LENGTH = 32
UID_ROOT = "2.25."
DICOM_REGISTRY_ROOT = "1.2.840.10008."

DIRECT_IDENTIFIER_TAGS = {
    "PatientBirthDate",
    "PatientBirthTime",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "PatientMotherBirthName",
    "OtherPatientIDs",
    "OtherPatientNames",
    "OtherPatientIDsSequence",
    "PatientComments",
    "AdditionalPatientHistory",
    "ReferringPhysicianName",
    "RequestingPhysician",
    "PerformingPhysicianName",
    "OperatorsName",
    "NameOfPhysiciansReadingStudy",
    "DeviceSerialNumber",
}

# UID elements mapped with a fixed kind, so references in other elements map to the same value.
UID_KINDS = {
    "StudyInstanceUID": "study",
    "SeriesInstanceUID": "series",
    "SOPInstanceUID": "sop",
    "MediaStorageSOPInstanceUID": "sop",
    "ReferencedSOPInstanceUID": "sop",
    "ReferencedSOPInstanceUIDInFile": "sop",
    "FrameOfReferenceUID": "frame",
    "ReferencedFrameOfReferenceUID": "frame",
}
# UIDs that identify standards, not instances: never rewritten.
KEEP_UIDS = {
    "SOPClassUID", "MediaStorageSOPClassUID", "ReferencedSOPClassUID", "TransferSyntaxUID",
    "ImplementationClassUID", "ReferencedTransferSyntaxUIDInFile", "ReferencedSOPClassUIDInFile",
}
TEXT_VRS = {"AE", "CS", "LO", "LT", "PN", "SH", "ST", "UC", "UT"}


class MissingSecretError(RuntimeError):
    """PSEUDONYM_SECRET is missing or too short."""


@dataclass
class AnonymizeResult:
    written: int = 0
    failures: list = field(default_factory=list)  # [(input path, reason)]


def load_secret(env=os.environ) -> str:
    secret = env.get(SECRET_ENV, "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise MissingSecretError(
            f"{SECRET_ENV} is missing or shorter than {MIN_SECRET_LENGTH} characters. Generate one with: "
            "python -c \"import secrets;print(secrets.token_hex(32))\" and keep it stable and out of git.")
    return secret


def _hmac_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def pseudo_patient_id(secret: str, patient_id, study_uid) -> str:
    patient_id = str(patient_id or "").strip()
    if patient_id:
        return _hmac_hex(secret, "patient:" + patient_id)[:PATIENT_ID_LENGTH]
    return _hmac_hex(secret, "patient-from-study:" + str(study_uid or "").strip())[:PATIENT_ID_LENGTH]


def pseudo_uid(secret: str, kind: str, uid) -> str:
    return UID_ROOT + str(int(_hmac_hex(secret, f"{kind}:{str(uid).strip()}")[:32], 16))


def _map_uid(secret, kind, value):
    if isinstance(value, (list, tuple, MultiValue)):
        return [pseudo_uid(secret, kind, v) if v else v for v in value]
    return pseudo_uid(secret, kind, value) if value else value


def _scrub(ds, secret, pseudo_pid, identity_values):
    """Recursive pass over one dataset level (sequence items handled by recursion)."""
    for tag in list(ds.keys()):
        elem = ds[tag]
        keyword = elem.keyword
        if keyword in DIRECT_IDENTIFIER_TAGS:
            del ds[tag]
        elif elem.VR == "SQ":
            for item in elem.value or []:
                _scrub(item, secret, pseudo_pid, identity_values)
        elif keyword in UID_KINDS:
            elem.value = _map_uid(secret, UID_KINDS[keyword], elem.value)
        elif elem.VR == "UI" and keyword not in KEEP_UIDS and elem.value:
            if not str(elem.value).startswith(DICOM_REGISTRY_ROOT):
                elem.value = _map_uid(secret, "uid", elem.value)
        elif elem.VR in TEXT_VRS and elem.value is not None and str(elem.value).strip() in identity_values:
            elem.value = pseudo_pid


def anonymize_dataset(ds, secret: str):
    """Pseudonymize one dataset in place (file meta included). Pixel data is not touched."""
    original_pid = str(ds.get("PatientID") or "").strip()
    original_name = str(ds.get("PatientName") or "").strip()
    pseudo_pid = pseudo_patient_id(secret, original_pid, ds.get("StudyInstanceUID"))
    identity_values = {v for v in (original_pid, original_name) if v}
    _scrub(ds, secret, pseudo_pid, identity_values)
    ds.PatientID = pseudo_pid
    ds.PatientName = pseudo_pid
    ds.remove_private_tags()
    meta = getattr(ds, "file_meta", None)
    if meta is not None and meta.get("MediaStorageSOPInstanceUID"):
        meta.MediaStorageSOPInstanceUID = pseudo_uid(secret, "sop", meta.MediaStorageSOPInstanceUID)


def anonymize_folder(input_dir: Path, output_dir: Path, secret: str = None) -> AnonymizeResult:
    """Pseudonymize every file under input_dir into output_dir (same relative paths), one at a time.

    Unreadable or non-DICOM files are collected in result.failures. Raises MissingSecretError before
    touching any file, and FileNotFoundError when no file could be written.
    """
    secret = secret if secret is not None else load_secret()
    if len(secret) < MIN_SECRET_LENGTH:
        raise MissingSecretError(f"{SECRET_ENV} is shorter than {MIN_SECRET_LENGTH} characters")
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    result = AnonymizeResult()
    for path in sorted(p for p in input_dir.rglob("*") if p.is_file()):
        try:
            ds = pydicom.dcmread(path)
            if "StudyInstanceUID" not in ds:
                raise ValueError("not an image DICOM (no StudyInstanceUID)")
            anonymize_dataset(ds, secret)
            out_path = output_dir / path.relative_to(input_dir)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(out_path)
            result.written += 1
        except Exception as exc:  # one bad file never aborts the run
            result.failures.append((str(path), type(exc).__name__))
    if result.written == 0:
        raise FileNotFoundError(f"No readable DICOM files found under {input_dir}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, help="Folder with raw DICOM files")
    parser.add_argument("output_dir", type=Path, help="Folder to write pseudonymized copies")
    args = parser.parse_args()

    try:
        outcome = anonymize_folder(args.input_dir, args.output_dir)
    except MissingSecretError as exc:
        raise SystemExit(str(exc))
    print(f"Pseudonymized {outcome.written} file(s); {len(outcome.failures)} failed.")
    for failed_path, reason in outcome.failures:
        print(f"  failed: {failed_path} ({reason})")
