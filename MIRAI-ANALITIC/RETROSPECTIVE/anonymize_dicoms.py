#!/usr/bin/env python3
"""Pseudonymize mammography DICOM files before any metadata scan or MIRAI
submission. Must run first, so collect_dicoms.py, sanitize_dicoms.py and
run_mirai.py only ever see de-identified copies.

For each study (grouped by StudyInstanceUID), generates one random UUID and
uses it as PatientName/PatientID for every view of that study, so the
viewer-rendered patient label becomes the UUID instead of the real name.
Direct identifiers (birth date, addresses, physician/operator names) are
removed. Pixel data is copied untouched so the AI analysis is unaffected.

Kept on purpose (not PHI, or required by the existing pipeline):
- InstitutionName (explicit product decision; not scrubbed)
- PatientSex, PatientAge (clinically meaningful, not direct identifiers)
- StudyDate/AcquisitionDate/Time (sanitize_dicoms.py's duplicate-view
  selection depends on them; not shifted, so treat as a known limitation,
  not a full HIPAA Safe Harbor de-identification)
- StudyInstanceUID/SeriesInstanceUID/SOPInstanceUID (grouping keys, not
  human-readable)
- Manufacturer/ManufacturerModelName (collect_dicoms.py reports these as
  "Machine" stats)
"""
import argparse
import uuid
from pathlib import Path

import pydicom

DIRECT_IDENTIFIER_TAGS = [
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
]


def anonymize_study(datasets, pseudo_id):
    """Scrub identifying tags in place, sharing one pseudo_id across every
    view of the same study."""
    for ds in datasets:
        ds.PatientName = pseudo_id
        ds.PatientID = pseudo_id
        for tag in DIRECT_IDENTIFIER_TAGS:
            if tag in ds:
                delattr(ds, tag)
        ds.remove_private_tags()


def anonymize_folder(input_dir: Path, output_dir: Path):
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)

    dicom_files = sorted(input_dir.rglob("*.dcm"))
    if not dicom_files:
        raise FileNotFoundError(f"No .dcm files found under {input_dir}")

    by_study = {}
    for path in dicom_files:
        ds = pydicom.dcmread(path)
        study_uid = ds.get("StudyInstanceUID", None) or "NO_STUDY_UID"
        by_study.setdefault(study_uid, []).append((path, ds))

    mapping = {}
    for study_uid, items in by_study.items():
        pseudo_id = uuid.uuid4().hex
        mapping[study_uid] = pseudo_id

        datasets = [ds for _, ds in items]
        anonymize_study(datasets, pseudo_id)

        for path, ds in items:
            rel_path = path.relative_to(input_dir)
            out_path = output_dir / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(out_path)

    return mapping


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path, help="Folder with raw DICOM files")
    parser.add_argument("output_dir", type=Path, help="Folder to write pseudonymized copies")
    args = parser.parse_args()

    result_mapping = anonymize_folder(args.input_dir, args.output_dir)

    print(f"Anonymized {len(result_mapping)} study(ies):")
    for study_uid, pseudo_id in result_mapping.items():
        print(f"  {study_uid} -> {pseudo_id}")
