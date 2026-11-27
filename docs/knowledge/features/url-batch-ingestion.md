---
type: feature
title: "URL batch ingestion"
status: in-progress
created: 2026-11-26
tags: [ingestion, workers, mirai, mysql]
related: [../decisions/0001-url-batch-ingestion-mysql-queue.md, ../constraints/patient-data-pseudonymization.md]
source: null
---

# URL batch ingestion

## Purpose

Given a URL to a folder tree of mammography studies, discover every patient folder, assemble the 4 standard views per study, score each complete study with MIRAI, and report progress and results for large batches.

## Where it lives

| Layer | Path |
|-------|------|
| Domain | `MIRAI-ANALITIC/app/ingest/domain/` (Study, View, StudyStatus, assemble_views) |
| Ports | `MIRAI-ANALITIC/app/ingest/ports/` (StudySource, StudyRepository, RiskModel) |
| Adapters | `MIRAI-ANALITIC/app/ingest/adapters/` (filesystem_source, http_index_source, mysql_repository, mirai_client) |
| Workers | `MIRAI-ANALITIC/app/ingest/workers/` (crawler, fetch_worker, score_worker) |
| API | `MIRAI-ANALITIC/app/api.py` (`POST /batches`, `GET /batches/{id}`) |
| Data | MySQL tables `batch`, `study`, `study_image` |

## How to use or extend it

- Study lifecycle: `DISCOVERED -> FETCHING -> READY -> SCORING -> DONE`, or `INCOMPLETE` / `FAILED`.
- Idempotency: `UNIQUE(batch_id, source_key)`, where `source_key` is a hash of the patient folder path. Re-running a URL never duplicates.
- Crawl rule: finish one patient folder completely before moving to the next, so studies are queued as soon as they are discovered. Skip folders already registered in the batch, so the crawl can resume.
- DICOM detection: read the first 132 bytes (HTTP `Range`) and check the `DICM` marker, so extensionless files work without a full download.
- Edge cases: fewer than 4 views goes to `INCOMPLETE` with the missing views; duplicate views go through `select_unique_views`; two StudyInstanceUIDs in one folder become two studies; a corrupt file marks only that image as invalid.
- New source type: implement `StudySource.list_dir` and `StudySource.open`. The crawler does not change.
- Planned env variable names: `MYSQL_URL`, `MIRAI_URL`, `INGEST_ALLOWED_HOSTS`, `STAGING_MAX_STUDIES`, `PSEUDONYM_SECRET`.

## Implementation stages

1. MySQL + tables + `filesystem_source` + one worker of each kind, end to end on a local tree. **Done 2026-11-26**: CLI `python -m ingest.cli create-batch|status`, e2e verified (demo study DONE with the reference scores, 3-view study INCOMPLETE). Open follow-ups: a folder with more than one study is marked INCOMPLETE instead of being split; there is no attempt cap for fetches that crash the worker; error texts can carry staging paths.
2. URL source adapter + `/batches` endpoints + SSRF guards. **Done 2026-11-26**: `ingest/adapters/http_index_source.py` (Apache/nginx/Python autoindex, nginx JSON), `INGEST_ALLOWED_HOSTS` allowlist (empty = no URLs), no redirects, `INGEST_MAX_FILE_BYTES` cap, filesystem roots only under `/dicom`; `POST /batches`, `GET /batches/{id}`. E2E over HTTP verified. Open follow-ups: the allowlist matches hostnames only (no IP pinning); batches have no FAILED state when their root becomes invalid.
2b. **Done 2026-11-26 per decision 0003**: studies grouped by StudyInstanceUID read from 64 KB header ranges at crawl time (any layout or depth); new status COLLECTING until all 4 views are present; `skipped_file` table; `GET /batches/{id}/fallos.txt` and `GET /batches/{id}/report.xlsx` (Resumen, Estudios, Fallos, Leyenda) built by `ingest/reports.py`; Streamlit "Lotes" view in `batches_view.py`. E2E verified with a study spread across two folders. Open follow-ups: `status_report(None)` loads every batch, which will be slow at scale. Original plan:
   Reporting (before stage 3 because it needs folder names): `GET /batches/{id}/fallos.txt` with one line per problem (folder, status, reason), including files skipped as non-DICOM, which are not recorded yet; `GET /batches/{id}/report.xlsx` generated on demand from MySQL with `pipeline.build_excel_report` plus a Fallos sheet; Streamlit live progress view. Open decision: real folder names (PHI, restricted table) or pseudonymous codes in the failures report.
3. Deterministic pseudonymization (see constraint). **Done 2026-11-26**: `PSEUDONYM_SECRET` is required by every service that pseudonymizes.
3b. **Done 2026-11-26 per decision 0004**: private-network source policy with a deny list and IP pinning, `X-API-Key` on `/batches*` (`INGEST_API_KEYS`), and a probe on submit (422 when the host is unreachable or the URL is not a listing). Swagger at `/docs` has an Authorize button.
4. Scale: more MIRAI instances or GPU. Outbox to Kafka only if an external consumer appears.

## Decisions

- [0001 URL batch ingestion pipeline with MySQL as job queue](../decisions/0001-url-batch-ingestion-mysql-queue.md)
