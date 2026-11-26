"""Configuration from environment variables only. Fails fast when MySQL settings are missing."""
import os
from dataclasses import dataclass

REQUIRED_MYSQL_VARS = ("MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")


@dataclass(frozen=True)
class Settings:
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_user: str
    mysql_password: str
    mirai_url: str
    staging_dir: str
    staging_max_studies: int
    poll_interval: float


def load_settings(env=os.environ) -> Settings:
    missing = [name for name in REQUIRED_MYSQL_VARS if not env.get(name)]
    if missing:
        raise SystemExit(f"ingest: missing required environment variable(s): {', '.join(missing)}")
    return Settings(
        mysql_host=env["MYSQL_HOST"],
        mysql_port=int(env.get("MYSQL_PORT", "3306")),
        mysql_database=env["MYSQL_DATABASE"],
        mysql_user=env["MYSQL_USER"],
        mysql_password=env["MYSQL_PASSWORD"],
        mirai_url=env.get("MIRAI_URL", "http://mirai:5000/dicom/files"),
        staging_dir=env.get("STAGING_DIR", "/staging"),
        staging_max_studies=int(env.get("STAGING_MAX_STUDIES", "20")),
        poll_interval=float(env.get("POLL_INTERVAL", "5")),
    )


def load_source_policy(env=os.environ):
    """INGEST_ALLOWED_HOSTS (comma-separated host or host:port; empty = no URL source allowed) and
    INGEST_MAX_FILE_BYTES (default 100 MB)."""
    from .sources import DEFAULT_MAX_FILE_BYTES, SourcePolicy, parse_allowed_hosts

    return SourcePolicy(
        allowed_hosts=parse_allowed_hosts(env.get("INGEST_ALLOWED_HOSTS", "")),
        max_file_bytes=int(env.get("INGEST_MAX_FILE_BYTES") or DEFAULT_MAX_FILE_BYTES),
    )


def build_repository(settings: Settings):
    from .adapters.mysql_repository import MySQLRepository

    return MySQLRepository(settings.mysql_host, settings.mysql_port, settings.mysql_user,
                           settings.mysql_password, settings.mysql_database)
