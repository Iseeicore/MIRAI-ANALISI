"""Standalone checks for db.py (run with: python3 test_db.py). No pytest in this project."""
import os
import tempfile

import pandas as pd

from db import init_db, record_exams, fetch_all

db_path = os.path.join(tempfile.mkdtemp(prefix="db_test_"), "analisis.db")

exams_df = pd.DataFrame([
    {
        "studyuid": "1.2.3",
        "patientid": "anon-uuid-aaa",
        "study_date": "20240115",
        "hospital": "Hospital A",
        "full_exam": 1,
        "Year 1": 0.01, "Year 2": 0.02, "Year 3": 0.03, "Year 4": 0.04, "Year 5": 0.05,
        "mirai_error": None,
    },
    {
        "studyuid": "4.5.6",
        "patientid": "anon-uuid-bbb",
        "study_date": "20230310",
        "hospital": "Hospital B",
        "full_exam": 0,
        "Year 1": None, "Year 2": None, "Year 3": None, "Year 4": None, "Year 5": None,
        "mirai_error": None,
    },
])

init_db(db_path)
record_exams(exams_df, db_path)

rows = fetch_all(db_path)
assert len(rows) == 2, f"esperaba 2 filas, hay {len(rows)}"

by_uid = {r["study_uid"]: r for r in rows}
assert by_uid["1.2.3"]["anonymization_id"] == "anon-uuid-aaa"
assert by_uid["1.2.3"]["hospital"] == "Hospital A"
assert by_uid["1.2.3"]["study_year"] == "2024"
assert by_uid["1.2.3"]["score_year_1"] == 0.01
assert by_uid["1.2.3"]["score_year_5"] == 0.05
assert by_uid["4.5.6"]["study_year"] == "2023"
assert by_uid["4.5.6"]["score_year_1"] is None

# re-registrar el mismo estudio (ej. se reanaliza) actualiza, no duplica
updated_df = exams_df.copy()
updated_df.loc[updated_df["studyuid"] == "1.2.3", "Year 1"] = 0.99
record_exams(updated_df, db_path)
rows = fetch_all(db_path)
assert len(rows) == 2, f"se duplico en vez de actualizar: {len(rows)} filas"
by_uid = {r["study_uid"]: r for r in rows}
assert by_uid["1.2.3"]["score_year_1"] == 0.99

print("OK: todos los checks de db.py pasaron")
