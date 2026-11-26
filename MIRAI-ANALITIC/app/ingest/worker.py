"""Worker entry point: python -m ingest.worker --role crawl|fetch|score

Polls the queue every POLL_INTERVAL seconds when idle. SIGTERM/SIGINT finish the current job and
exit; a job cut short by a hard kill is resumed by another worker once its lease expires.
"""
import argparse
import logging
import os
import signal
import socket
import threading
import time

from .config import build_repository, load_settings, load_source_policy
from .sources import make_source

CRAWL_LEASE_SECONDS = 600
FETCH_LEASE_SECONDS = 600
SCORE_LEASE_SECONDS = 900
DB_RETRY_SECONDS = 10

log = logging.getLogger("ingest.worker")


def _load_secret() -> str:
    """PSEUDONYM_SECRET, validated at startup so a misconfigured worker fails fast."""
    import pipeline  # noqa: F401  (puts RETROSPECTIVE/ on sys.path)
    from anonymize_dicoms import MissingSecretError, load_secret

    try:
        return load_secret()
    except MissingSecretError as exc:
        raise SystemExit(f"ingest: {exc}")


def _build_step(role, settings, repo, worker_id):
    policy = load_source_policy()

    def source_factory(source_root):
        return make_source(source_root, policy)

    if role == "crawl":
        from .workers.crawler import crawl_next_batch

        secret = _load_secret()
        return lambda: crawl_next_batch(repo, source_factory, worker_id, CRAWL_LEASE_SECONDS, secret=secret)
    if role == "fetch":
        from .workers.fetch_worker import fetch_next_study

        secret = _load_secret()
        return lambda: fetch_next_study(repo, source_factory, settings.staging_dir, worker_id,
                                        FETCH_LEASE_SECONDS, settings.staging_max_studies, secret=secret)
    from .adapters.mirai_client import MiraiClient
    from .workers.score_worker import score_next_study

    model = MiraiClient(settings.mirai_url)
    return lambda: score_next_study(repo, model, settings.staging_dir, worker_id, SCORE_LEASE_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch ingestion worker")
    parser.add_argument("--role", choices=["crawl", "fetch", "score"], required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    worker_id = f"{args.role}@{socket.gethostname()}:{os.getpid()}"
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    repo = build_repository(settings)
    while not stop.is_set():
        try:
            repo.apply_schema()
            break
        except Exception as exc:
            log.warning("database not ready (%s), retrying in %ss", type(exc).__name__, DB_RETRY_SECONDS)
            stop.wait(DB_RETRY_SECONDS)

    step = _build_step(args.role, settings, repo, worker_id)
    log.info("worker %s started", worker_id)
    while not stop.is_set():
        try:
            worked = step()
        except Exception:
            log.exception("unexpected error in %s step", args.role)
            worked = False
        if not worked:
            stop.wait(settings.poll_interval)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
