"""Local SQLite registry of analyzed exams: anonymization id, hospital, year and MIRAI scores."""
import datetime as dt
import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/analisis.db")

SCORE_COLUMNS = ["score_year_1", "score_year_2", "score_year_3", "score_year_4", "score_year_5"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS estudios_analizados (
    study_uid TEXT PRIMARY KEY,
    anonymization_id TEXT,
    hospital TEXT,
    study_year TEXT,
    study_date TEXT,
    score_year_1 REAL,
    score_year_2 REAL,
    score_year_3 REAL,
    score_year_4 REAL,
    score_year_5 REAL,
    mirai_error TEXT,
    analyzed_at TEXT NOT NULL
)
"""


def init_db(db_path: str = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_exams(exams_df, db_path: str = DB_PATH) -> None:
    """Upsert one row per exam (by study_uid): re-analyzing the same study updates it, never duplicates."""
    init_db(db_path)
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        for _, row in exams_df.iterrows():
            study_date = row.get("study_date")
            study_year = str(study_date)[:4] if study_date else None
            conn.execute(
                """INSERT INTO estudios_analizados
                   (study_uid, anonymization_id, hospital, study_year, study_date,
                    score_year_1, score_year_2, score_year_3, score_year_4, score_year_5,
                    mirai_error, analyzed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(study_uid) DO UPDATE SET
                     anonymization_id=excluded.anonymization_id,
                     hospital=excluded.hospital,
                     study_year=excluded.study_year,
                     study_date=excluded.study_date,
                     score_year_1=excluded.score_year_1,
                     score_year_2=excluded.score_year_2,
                     score_year_3=excluded.score_year_3,
                     score_year_4=excluded.score_year_4,
                     score_year_5=excluded.score_year_5,
                     mirai_error=excluded.mirai_error,
                     analyzed_at=excluded.analyzed_at
                """,
                (
                    row.get("studyuid"),
                    row.get("patientid"),
                    row.get("hospital"),
                    study_year,
                    study_date,
                    row.get("Year 1"),
                    row.get("Year 2"),
                    row.get("Year 3"),
                    row.get("Year 4"),
                    row.get("Year 5"),
                    row.get("mirai_error"),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def fetch_all(db_path: str = DB_PATH) -> list[dict]:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM estudios_analizados ORDER BY analyzed_at DESC")]
    finally:
        conn.close()
