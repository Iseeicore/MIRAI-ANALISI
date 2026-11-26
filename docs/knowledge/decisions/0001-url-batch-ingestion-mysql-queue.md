---
type: decision
title: "URL batch ingestion pipeline with MySQL as job queue"
status: superseded
created: 2026-11-26
tags: [ingestion, queue, mysql, workers, mirai]
supersedes: null
superseded_by: 0003-study-identity-from-dicom-headers.md
related: [../features/url-batch-ingestion.md, ../constraints/patient-data-pseudonymization.md]
---

# 0001 URL batch ingestion pipeline with MySQL as job queue

## Context

Studies arrive as a folder tree behind a URL: `PATIENT_DATE/series-NNNNN/image-NNNNN.dcm`, with 4 series (one per view, about 26 MB each) per patient folder. How the data will be served (HTTP index, S3, network share, DICOMweb) is not known yet. Volumes are in the thousands of studies.

MIRAI is the bottleneck: about 45 s per study on CPU, one request at a time on a host with about 7.6 GB for Docker (a concurrent request was OOM-killed). That is about 80 studies per hour per MIRAI instance.

## Decision

Three decoupled stages share one MySQL 8 database that acts as both state store and job queue:

1. **Crawler**: walks the source one patient folder at a time, fully (depth-first), and registers the study and its image list in one transaction (`DISCOVERED`).
2. **Fetch worker**: downloads one study's images to bounded local staging, reads headers, pseudonymizes, assembles L_CC/L_MLO/R_CC/R_MLO from DICOM headers (never from folder order), and marks the study `READY` or `INCOMPLETE`.
3. **Score worker**: sends `READY` studies to MIRAI, stores the scores (`DONE`) or retries with backoff (max 3), then deletes staging.

Workers claim rows with `SELECT ... FOR UPDATE SKIP LOCKED` plus a lease (`locked_by`, `lease_until`). A crashed worker's lease expires and another worker resumes the study. The source sits behind a `StudySource` port (`list_dir`, `open` with an optional byte range), so a new source type only adds an adapter.

## Alternatives considered

| Option | Pros | Cons |
|--------|------|------|
| MySQL 8 as queue (chosen) | One component; queue and state are transactional; easy resume and SQL progress reports; far above the needed throughput | Polling; not meant for fan-out to many consumers |
| Kafka | Fan-out, replay, very high throughput | About 80 msg/h does not need it; long tasks fight consumer poll timeouts; retries and DLQ need extra topics; a DB is still needed for state; more infra to run |
| Redis + RQ/Celery | Simple workers | Extra component; state split between Redis and the DB |

## Consequences

- Throughput scales by adding MIRAI instances (each needs 6 GB of RAM or more) or a GPU, with one score worker per instance. The queue is never the limit.
- Staging disk must be bounded (for example 20 `READY` studies, about 2 GB) for backpressure.
- If another system later needs "study analyzed" events, add a transactional outbox table and a publisher to Kafka. Workers stay unchanged.
- `POST /batches {url}` takes external URLs: it must enforce a host allowlist, stay under the root URL, and cap file size and file count (SSRF protection).

## Where it applies

- `MIRAI-ANALITIC/app/ingest/` (planned: `domain/`, `ports/`, `adapters/`, `workers/`)
- `MIRAI-ANALITIC/app/api.py` (planned `/batches` endpoints)
- `docker-compose.yml` (planned `mysql`, `worker-fetch`, `worker-score` services)
- Reuses `MIRAI-ANALITIC/RETROSPECTIVE/collect_dicoms.py` (`scan_file`) and `MIRAI-ANALITIC/RETROSPECTIVE/sanitize_dicoms.py` (`select_unique_views`)
