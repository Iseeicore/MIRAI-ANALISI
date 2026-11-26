"""Standalone checks for pipeline.pseudonymize_dropped (run with: python test_pipeline_pseudonymize.py)."""
import pandas as pd

from pipeline import DROPPED_VIEW_COLUMNS, pseudonymize_dropped, pseudonymize_exams

SECRET = "t" * 40

dropped = pd.DataFrame([
    {
        "PatientID": "ORIG-PATIENT-1",
        "StudyUID": "1.2.840.99999.1",
        "SeriesUID": "1.2.840.99999.1.1",
        "ImageUID": "1.2.840.99999.1.1.1",
        "type": "L_CC",
        "study_date": "20240115",
        "acquisition_date": "20240115",
        "acquisition_time": "101010",
        "Hospital": "Hospital A",
        "Machine": "M1",
        "file_path": "/dicom/x/a.dcm",
    },
])
exams = pd.DataFrame([{"studyuid": "1.2.840.99999.1", "patientid": "ORIG-PATIENT-1"}])

out = pseudonymize_dropped(dropped, SECRET)
flat = " ".join(str(v) for v in out.to_numpy().ravel())
for original in ("ORIG-PATIENT-1", "1.2.840.99999.1"):
    assert original not in flat, f"original identifier leaked: {original}"

# only the user-facing columns survive, so original Series/Image UIDs never reach the UI
assert list(out.columns) == [c for c in DROPPED_VIEW_COLUMNS if c in dropped.columns], list(out.columns)

# same pseudonyms as the exams table, so a dropped view can be matched to its study
pseudo_exams = pseudonymize_exams(exams, SECRET)
assert out.loc[0, "StudyUID"] == pseudo_exams.loc[0, "studyuid"]
assert out.loc[0, "PatientID"] == pseudo_exams.loc[0, "patientid"]

# the input is not mutated
assert dropped.loc[0, "PatientID"] == "ORIG-PATIENT-1"

# empty input stays empty and does not fail
empty = pseudonymize_dropped(dropped.iloc[0:0], SECRET)
assert len(empty) == 0

print("OK: pipeline pseudonymize_dropped checks passed")
