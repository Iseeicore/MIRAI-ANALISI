"""Standalone checks for RETROSPECTIVE/anonymize_dicoms.py (run with: python test_anonymize.py).

No pytest in this project. Deterministic HMAC pseudonyms keyed by PSEUDONYM_SECRET, file-by-file
processing with collected failures, no original identifier left anywhere (sequences included).
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

import pipeline  # puts RETROSPECTIVE/ on sys.path
from anonymize_dicoms import MissingSecretError, anonymize_folder, load_secret, pseudo_patient_id, pseudo_uid
from ingest_test_support import TEST_SECRET, new_uid, write_dicom

OTHER_SECRET = "another-secret-" + "f" * 40
UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")

src = Path(tempfile.mkdtemp(prefix="anon_src_"))
study_1, study_2 = new_uid(), new_uid()
frame_uid = new_uid()

# Same patient, two studies; plus a third file with an empty PatientID.
originals = []
for name, study, view in [("a/1.dcm", study_1, "L_CC"), ("a/2.dcm", study_1, "R_CC"), ("b/3.dcm", study_2, "L_CC")]:
    path = write_dicom(src / name, study, view, patient_name="DOE^JANE")
    ds = pydicom.dcmread(path)
    ds.PatientID = "PID-12345"
    ds.FrameOfReferenceUID = frame_uid
    ref = Dataset()
    ref.ReferencedSOPClassUID = ds.SOPClassUID
    ref.ReferencedSOPInstanceUID = ds.SOPInstanceUID
    ref.StudyInstanceUID = study
    ds.ReferencedImageSequence = Sequence([ref])
    nested = Dataset()
    nested.RequestedProcedureID = "PID-12345"
    nested.PatientBirthDate = "19700101"
    ds.RequestAttributesSequence = Sequence([nested])
    ds.save_as(path)
    originals.append((path, ds.SOPInstanceUID, ds.SeriesInstanceUID, ds.PixelData))
empty_pid = write_dicom(src / "c/4.dcm", new_uid(), "R_MLO", patient_name="")
ds = pydicom.dcmread(empty_pid)
ds.PatientID = ""
ds.save_as(empty_pid)
(src / "c/corrupt.dcm").write_bytes(b"\x00" * 128 + b"DICM" + b"\xff" * 64)

FORBIDDEN = {"PID-12345", "DOE^JANE", "19700101", study_1, study_2, frame_uid}
FORBIDDEN |= {o[1] for o in originals} | {o[2] for o in originals}


def walk_values(ds):
    """Every element value in the dataset (file meta and nested sequences included), as strings."""
    for elem in ds.iterall():
        if elem.VR != "SQ":
            yield elem.keyword, str(elem.value)


def run(secret, name):
    out = Path(tempfile.mkdtemp(prefix=f"anon_out_{name}_"))
    result = anonymize_folder(src, out, secret=secret)
    return out, result


out1, result1 = run(TEST_SECRET, "1")
out2, result2 = run(TEST_SECRET, "2")
out3, _ = run(OTHER_SECRET, "3")

# A corrupt file is reported without aborting the others; the mapping is never returned.
assert [Path(p).name for p, _reason in result1.failures] == ["corrupt.dcm"], result1.failures
assert result1.written == 4, result1


def read(out, rel):
    return pydicom.dcmread(out / rel)


a1, a2, b3 = read(out1, "a/1.dcm"), read(out1, "a/2.dcm"), read(out1, "b/3.dcm")

# Same patient across two studies -> same pseudo PatientID; PatientName follows it.
assert a1.PatientID == a2.PatientID == b3.PatientID
assert str(a1.PatientName) == a1.PatientID
assert len(a1.PatientID) == 32 and re.fullmatch(r"[0-9a-f]{32}", a1.PatientID)
assert a1.StudyInstanceUID == a2.StudyInstanceUID != b3.StudyInstanceUID
assert a1.PatientID == pseudo_patient_id(TEST_SECRET, "PID-12345", study_1)
assert a1.StudyInstanceUID == pseudo_uid(TEST_SECRET, "study", study_1)

# Empty PatientID falls back to the StudyInstanceUID (different prefix), never left empty.
c4 = read(out1, "c/4.dcm")
assert c4.PatientID and c4.PatientID != a1.PatientID and len(c4.PatientID) == 32

# Deterministic across runs with the same secret; different with another secret.
for rel in ["a/1.dcm", "a/2.dcm", "b/3.dcm", "c/4.dcm"]:
    x, y, z = read(out1, rel), read(out2, rel), read(out3, rel)
    for kw in ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"]:
        assert x.get(kw) == y.get(kw), (rel, kw)
        assert x.get(kw) != z.get(kw), (rel, kw)
    assert x.file_meta.MediaStorageSOPInstanceUID == x.SOPInstanceUID
    if not rel.startswith("c/"):
        assert x.ReferencedImageSequence[0].ReferencedSOPInstanceUID == x.SOPInstanceUID, "references stay consistent"
        assert x.ReferencedImageSequence[0].StudyInstanceUID == x.StudyInstanceUID
        assert x.FrameOfReferenceUID == read(out1, "a/1.dcm").FrameOfReferenceUID != frame_uid

# No original identifier anywhere (sequences and file meta included); UIDs valid.
for path in out1.rglob("*.dcm"):
    ds = pydicom.dcmread(path)
    assert "PatientBirthDate" not in ds
    for container in (ds.file_meta, ds):
        for keyword, value in walk_values(container):
            assert not any(f in value for f in FORBIDDEN), (path.name, keyword, value)
    for container in (ds.file_meta, ds):
        for elem in container.iterall():
            if elem.VR == "UI" and elem.value:
                uid = str(elem.value)
                assert len(uid) <= 64 and UID_RE.match(uid), (elem.keyword, uid)
                assert all(part == "0" or not part.startswith("0") for part in uid.split(".")), uid

# Pixel data untouched.
for path, _sop, _series, pixels in originals:
    assert read(out1, path.relative_to(src)).PixelData == pixels

# pipeline.pseudonymize_exams (for modes that scan without anonymizing) yields the same values.
exams = pd.DataFrame([{"studyuid": study_1, "patientid": "PID-12345"}, {"studyuid": study_2, "patientid": None}])
pseudo = pipeline.pseudonymize_exams(exams, TEST_SECRET)
assert list(pseudo["studyuid"]) == [a1.StudyInstanceUID, b3.StudyInstanceUID]
assert pseudo["patientid"].iloc[0] == a1.PatientID and pseudo["patientid"].iloc[1]
assert exams["studyuid"].iloc[0] == study_1, "input frame must not be modified"

# Missing or short secret fails fast, before touching any file.
os.environ.pop("PSEUDONYM_SECRET", None)
for env_value in (None, "too-short"):
    if env_value is not None:
        os.environ["PSEUDONYM_SECRET"] = env_value
    for call in (lambda: load_secret(), lambda: anonymize_folder(src, Path(tempfile.mkdtemp()))):
        try:
            call()
            raise AssertionError("missing/short secret accepted")
        except MissingSecretError as exc:
            assert "PSEUDONYM_SECRET" in str(exc)
os.environ["PSEUDONYM_SECRET"] = TEST_SECRET
assert anonymize_folder(src, Path(tempfile.mkdtemp())).written == 4, "secret read from the environment"

for d in (out1, out2, out3):
    shutil.rmtree(d, ignore_errors=True)
print("OK: deterministic pseudonymization checks passed")
