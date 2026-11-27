"""StudyRepository on MySQL 8 (PyMySQL). The study table is the job queue: workers claim rows with
SELECT ... FOR UPDATE SKIP LOCKED plus a lease (locked_by, lease_until). All lease and retry times
use the database clock (NOW(6)) so worker clocks never matter. Studies are keyed by
(batch_id, study_uid_hash) and files by (batch_id, path_hash), so a resumed crawl never duplicates."""
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import pymysql

from ..domain import (
    SCORE_YEARS, SKIP_EXTRA_AFTER_COMPLETED, VIEWS, BatchJob, FetchJob, ScoreJob, StudyImage, StudyStatus,
    common_parent, missing_views, path_hash, split_path,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
SCORE_COLUMNS = "score_year_1, score_year_2, score_year_3, score_year_4, score_year_5"
PSEUDONYM_SQL = "LEFT(study_uid_hash, 12)"


class OutdatedSchemaError(RuntimeError):
    pass


def _schema_statements() -> list:
    lines = [ln for ln in SCHEMA_PATH.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def _scores(values) -> dict:
    return dict(zip(SCORE_YEARS, values))


class MySQLRepository:
    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        self._conn_args = dict(host=host, port=int(port), user=user, password=password, database=database,
                               charset="utf8mb4", autocommit=False, connect_timeout=10)
        self._conn = None

    # plumbing ------------------------------------------------------------
    def _connection(self):
        if self._conn is None:
            self._conn = pymysql.connect(**self._conn_args)
        else:
            self._conn.ping(reconnect=True)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _tx(self):
        conn = self._connection()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def apply_schema(self):
        with self._tx() as cur:
            for stmt in _schema_statements():
                cur.execute(stmt)
            cur.execute("SELECT COUNT(*) FROM information_schema.columns"
                        " WHERE table_schema = DATABASE() AND table_name = 'study' AND column_name = 'folder'")
            if cur.fetchone()[0] == 0:
                raise OutdatedSchemaError("the ingest database has a pre-0003 schema; reset the ingest_mysql volume")

    # batches -------------------------------------------------------------
    def create_batch(self, source_root: str) -> int:
        with self._tx() as cur:
            cur.execute("INSERT INTO batch (source_root) VALUES (%s)", (source_root,))
            return cur.lastrowid

    def claim_batch(self, worker_id: str, lease_seconds: int) -> Optional[BatchJob]:
        with self._tx() as cur:
            cur.execute(
                "SELECT id, source_root FROM batch"
                " WHERE status = 'PENDING' OR (status = 'CRAWLING' AND lease_until < NOW(6))"
                " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE batch SET status = 'CRAWLING', locked_by = %s,"
                " lease_until = NOW(6) + INTERVAL %s SECOND WHERE id = %s",
                (worker_id, int(lease_seconds), row[0]))
            return BatchJob(row[0], row[1])

    def renew_batch_lease(self, batch_id: int, worker_id: str, lease_seconds: int) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE batch SET lease_until = NOW(6) + INTERVAL %s SECOND"
                " WHERE id = %s AND status = 'CRAWLING' AND locked_by = %s",
                (int(lease_seconds), batch_id, worker_id))
            return cur.rowcount == 1

    def seen_paths(self, batch_id: int) -> set:
        with self._tx() as cur:
            cur.execute("SELECT path_hash FROM study_image WHERE batch_id = %s"
                        " UNION SELECT path_hash FROM skipped_file WHERE batch_id = %s", (batch_id, batch_id))
            return {r[0] for r in cur.fetchall()}

    @staticmethod
    def _insert_skipped(cur, batch_id, key, folder, file_name, reason) -> bool:
        cur.execute("INSERT IGNORE INTO skipped_file (batch_id, path_hash, folder, file_name, reason)"
                    " VALUES (%s, %s, %s, %s, %s)", (batch_id, key, folder, file_name, reason))
        return cur.rowcount == 1

    def register_skipped(self, batch_id, source_path, reason, folder=None, file_name=None) -> bool:
        default_folder, default_name = split_path(source_path)
        with self._tx() as cur:
            return self._insert_skipped(cur, batch_id, path_hash(source_path), folder or default_folder,
                                        file_name or default_name, reason)

    def register_image(self, batch_id, study_uid_hash, source_path, size, view, folder=None) -> str:
        """One transaction per image. Returns "added", "queued" (4th distinct view), "duplicate" or
        "extra" (study already SCORING/DONE: recorded in skipped_file instead)."""
        key = path_hash(source_path)
        folder = folder or split_path(source_path)[0]
        with self._tx() as cur:
            cur.execute("SELECT 1 FROM study_image WHERE batch_id = %s AND path_hash = %s"
                        " UNION ALL SELECT 1 FROM skipped_file WHERE batch_id = %s AND path_hash = %s LIMIT 1",
                        (batch_id, key, batch_id, key))
            if cur.fetchone():
                return "duplicate"
            cur.execute("INSERT INTO study (batch_id, study_uid_hash, folder) VALUES (%s, %s, %s)"
                        " ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)", (batch_id, study_uid_hash, folder))
            study_id = cur.lastrowid
            cur.execute("SELECT status, folder FROM study WHERE id = %s FOR UPDATE", (study_id,))
            status, current_folder = cur.fetchone()
            if status in (StudyStatus.DONE.value, StudyStatus.SCORING.value):
                self._insert_skipped(cur, batch_id, key, folder, split_path(source_path)[1],
                                     SKIP_EXTRA_AFTER_COMPLETED)
                return "extra"
            cur.execute("INSERT INTO study_image (study_id, batch_id, source_path, path_hash, size, view_type)"
                        " VALUES (%s, %s, %s, %s, %s, %s)", (study_id, batch_id, source_path, key, size or 0, view))
            new_folder = common_parent([current_folder, folder])
            if new_folder != current_folder:
                cur.execute("UPDATE study SET folder = %s WHERE id = %s", (new_folder, study_id))
            if status != StudyStatus.COLLECTING.value:
                return "added"
            cur.execute("SELECT DISTINCT view_type FROM study_image WHERE study_id = %s", (study_id,))
            if missing_views(r[0] for r in cur.fetchall()):
                return "added"
            cur.execute("UPDATE study SET status = 'DISCOVERED' WHERE id = %s", (study_id,))
            return "queued"

    def finish_batch(self, batch_id: int, worker_id: str) -> bool:
        with self._tx() as cur:
            cur.execute("SELECT id FROM batch WHERE id = %s AND locked_by = %s FOR UPDATE", (batch_id, worker_id))
            if cur.fetchone() is None:
                return False
            cur.execute(
                "SELECT s.id, GROUP_CONCAT(DISTINCT si.view_type) FROM study s"
                " LEFT JOIN study_image si ON si.study_id = s.id"
                " WHERE s.batch_id = %s AND s.status = 'COLLECTING' GROUP BY s.id", (batch_id,))
            for study_id, views in cur.fetchall():
                missing = missing_views((views or "").split(","))
                cur.execute("UPDATE study SET status = 'INCOMPLETE', error = %s WHERE id = %s AND status = 'COLLECTING'",
                            (f"missing views: {', '.join(missing)}", study_id))
            cur.execute(
                "UPDATE batch b SET status = 'CRAWLED', locked_by = NULL, lease_until = NULL,"
                " total_studies = (SELECT COUNT(*) FROM study s WHERE s.batch_id = b.id),"
                " total_images = (SELECT COUNT(*) FROM study_image si WHERE si.batch_id = b.id),"
                " total_skipped = (SELECT COUNT(*) FROM skipped_file sf WHERE sf.batch_id = b.id)"
                " WHERE b.id = %s", (batch_id,))
            return True

    # fetch ---------------------------------------------------------------
    def claim_fetch(self, worker_id: str, lease_seconds: int, max_staged: int) -> Optional[FetchJob]:
        with self._tx() as cur:
            # Backpressure: studies holding (or about to hold) files in staging.
            cur.execute(
                "SELECT COUNT(*) FROM study WHERE status IN ('READY', 'SCORING')"
                " OR (status = 'FETCHING' AND lease_until >= NOW(6))")
            if cur.fetchone()[0] >= max_staged:
                return None
            cur.execute(
                "SELECT s.id, s.batch_id, b.source_root FROM study s JOIN batch b ON b.id = s.batch_id"
                " WHERE s.status = 'DISCOVERED' OR (s.status = 'FETCHING' AND s.lease_until < NOW(6))"
                " ORDER BY s.id LIMIT 1 FOR UPDATE OF s SKIP LOCKED")
            row = cur.fetchone()
            if row is None:
                return None
            study_id, batch_id, source_root = row
            cur.execute(
                "UPDATE study SET status = 'FETCHING', locked_by = %s,"
                " lease_until = NOW(6) + INTERVAL %s SECOND WHERE id = %s",
                (worker_id, int(lease_seconds), study_id))
            cur.execute("SELECT id, source_path, size, view_type FROM study_image WHERE study_id = %s ORDER BY id",
                        (study_id,))
            images = [StudyImage(*r) for r in cur.fetchall()]
            return FetchJob(study_id, batch_id, source_root, images)

    def mark_ready(self, study_id, worker_id, study_uid_hash, anon_id, selections) -> bool:
        """study_uid_hash is accepted for interface symmetry but never rewritten: it is the study key."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE study SET status = 'READY', anon_id = %s, attempts = 0,"
                " next_attempt_at = NULL, error = NULL, locked_by = NULL, lease_until = NULL"
                " WHERE id = %s AND status = 'FETCHING' AND locked_by = %s",
                (anon_id, study_id, worker_id))
            if cur.rowcount != 1:
                return False
            cur.execute("UPDATE study_image SET staged_path = NULL, selected = FALSE WHERE study_id = %s", (study_id,))
            cur.executemany(
                "UPDATE study_image SET view_type = %s, staged_path = %s, selected = TRUE"
                " WHERE id = %s AND study_id = %s",
                [(view, staged, image_id, study_id) for view, (image_id, staged) in selections.items()])
            return True

    def mark_incomplete(self, study_id, worker_id, error, study_uid_hash=None) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE study SET status = 'INCOMPLETE', error = %s, locked_by = NULL, lease_until = NULL"
                " WHERE id = %s AND status = 'FETCHING' AND locked_by = %s",
                (error, study_id, worker_id))
            return cur.rowcount == 1

    # score ---------------------------------------------------------------
    def claim_score(self, worker_id: str, lease_seconds: int) -> Optional[ScoreJob]:
        with self._tx() as cur:
            cur.execute(
                "SELECT id FROM study"
                " WHERE (status = 'READY' AND (next_attempt_at IS NULL OR next_attempt_at <= NOW(6)))"
                "    OR (status = 'SCORING' AND lease_until < NOW(6))"
                " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED")
            row = cur.fetchone()
            if row is None:
                return None
            study_id = row[0]
            cur.execute(
                "UPDATE study SET status = 'SCORING', attempts = attempts + 1, locked_by = %s,"
                " lease_until = NOW(6) + INTERVAL %s SECOND WHERE id = %s",
                (worker_id, int(lease_seconds), study_id))
            cur.execute("SELECT attempts FROM study WHERE id = %s", (study_id,))
            attempts = cur.fetchone()[0]
            cur.execute("SELECT view_type, staged_path FROM study_image WHERE study_id = %s AND selected",
                        (study_id,))
            selected = dict(cur.fetchall())
            return ScoreJob(study_id, attempts, {v: selected.get(v) for v in VIEWS})

    def mark_done(self, study_id, worker_id, scores) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE study SET status = 'DONE', error = NULL, locked_by = NULL, lease_until = NULL,"
                " score_year_1 = %s, score_year_2 = %s, score_year_3 = %s, score_year_4 = %s, score_year_5 = %s"
                " WHERE id = %s AND status = 'SCORING' AND locked_by = %s",
                (*[scores.get(y) for y in SCORE_YEARS], study_id, worker_id))
            return cur.rowcount == 1

    def schedule_retry(self, study_id, worker_id, delay_seconds, error) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE study SET status = 'READY', error = %s, locked_by = NULL, lease_until = NULL,"
                " next_attempt_at = NOW(6) + INTERVAL %s SECOND"
                " WHERE id = %s AND status = 'SCORING' AND locked_by = %s",
                (error, int(round(delay_seconds)), study_id, worker_id))
            return cur.rowcount == 1

    def requeue_fetch(self, study_id, worker_id, error) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE study SET status = 'DISCOVERED', error = %s, attempts = 0, next_attempt_at = NULL,"
                " locked_by = NULL, lease_until = NULL"
                " WHERE id = %s AND status = 'SCORING' AND locked_by = %s",
                (error, study_id, worker_id))
            return cur.rowcount == 1

    def mark_failed(self, study_id, worker_id, error) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE study SET status = 'FAILED', error = %s, locked_by = NULL, lease_until = NULL"
                " WHERE id = %s AND status IN ('FETCHING', 'SCORING') AND locked_by = %s",
                (error, study_id, worker_id))
            return cur.rowcount == 1

    # reporting -----------------------------------------------------------
    @staticmethod
    def _counts(cur, where, args) -> dict:
        cur.execute(f"SELECT status, COUNT(*) FROM study {where} GROUP BY status", args)
        counts = {status.value: 0 for status in StudyStatus}
        counts.update({status: n for status, n in cur.fetchall()})
        return counts

    def status_report(self, batch_id: Optional[int] = None) -> dict:
        where, args = ("WHERE batch_id = %s", (batch_id,)) if batch_id is not None else ("", ())
        conj = "AND" if where else "WHERE"
        with self._tx() as cur:
            counts = self._counts(cur, where, args)
            cur.execute(f"SELECT id, {PSEUDONYM_SQL}, folder, {SCORE_COLUMNS} FROM study"
                        f" {where} {conj} status = 'DONE' ORDER BY id", args)
            done = [dict(study_id=r[0], pseudonym=r[1], folder=r[2], scores=_scores(r[3:])) for r in cur.fetchall()]
            cur.execute(f"SELECT id, {PSEUDONYM_SQL}, folder, status, error FROM study"
                        f" {where} {conj} status IN ('INCOMPLETE', 'FAILED') ORDER BY id", args)
            problems = [dict(study_id=r[0], pseudonym=r[1], folder=r[2], status=r[3], error=r[4])
                        for r in cur.fetchall()]
            cur.execute("SELECT id, status, total_studies, total_images, total_skipped FROM batch"
                        f" {'WHERE id = %s' if batch_id is not None else ''} ORDER BY id", args)
            batches = [dict(batch_id=r[0], status=r[1], total_studies=r[2], total_images=r[3], total_skipped=r[4])
                       for r in cur.fetchall()]
            return dict(counts=counts, done=done, problems=problems, batches=batches)

    def report_data(self, batch_id: int) -> Optional[dict]:
        """Everything the reports need, with live totals (also while the batch is still crawling)."""
        with self._tx() as cur:
            cur.execute("SELECT id, status FROM batch WHERE id = %s", (batch_id,))
            row = cur.fetchone()
            if row is None:
                return None
            counts = self._counts(cur, "WHERE batch_id = %s", (batch_id,))
            cur.execute(f"SELECT id, {PSEUDONYM_SQL}, folder, status, {SCORE_COLUMNS}, error FROM study"
                        " WHERE batch_id = %s ORDER BY id", (batch_id,))
            studies = [dict(study_id=r[0], pseudonym=r[1], folder=r[2], status=r[3], scores=_scores(r[4:9]),
                            error=r[9]) for r in cur.fetchall()]
            cur.execute("SELECT folder, file_name, reason FROM skipped_file WHERE batch_id = %s ORDER BY id",
                        (batch_id,))
            skipped = [dict(folder=r[0], file_name=r[1], reason=r[2]) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM study_image WHERE batch_id = %s", (batch_id,))
            total_images = cur.fetchone()[0]
            batch = dict(batch_id=row[0], status=row[1], total_studies=len(studies), total_images=total_images,
                         total_skipped=len(skipped))
            return dict(batch=batch, counts=counts, studies=studies, skipped=skipped)
