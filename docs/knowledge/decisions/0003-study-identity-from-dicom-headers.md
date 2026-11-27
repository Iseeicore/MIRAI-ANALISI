---
type: decision
title: "Study identity from DICOM headers at crawl time, and failure reports"
status: accepted
created: 2026-11-26
tags: [ingestion, crawler, reporting, mysql, workers]
supersedes: 0001-url-batch-ingestion-mysql-queue.md
superseded_by: null
related: [../features/url-batch-ingestion.md, ../constraints/patient-data-pseudonymization.md]
---

# 0003 Study identity from DICOM headers at crawl time, and failure reports

## Context

Decision 0001 used the top-level folder under the root as the unit of work. The user clarified that the source layout is not guaranteed. Folder names can be codes (`0012`), dates or patient names, and nesting depth varies (for example `root/2026-11/<patient>/series/...`). With 0001, a date folder holding many patients would be marked INCOMPLETE ("multiple studies in folder"). The user also needs a downloadable failures report (folder + reason) and an Excel report that always reflects current progress. The folder name exists only to locate where something failed; the content of the analysis is what matters.

## Decision

Everything in 0001 that is not listed here stays: the three stages (crawler, fetch worker, score worker), MySQL 8 as both state store and job queue with `SKIP LOCKED` + leases, the `StudySource` port, retries and backoff, bounded staging, one score worker per MIRAI instance, and Kafka only through a future outbox.

Changed:

1. **A study is a StudyInstanceUID, not a folder.** The crawler walks the whole tree at any depth. For every file it reads only the first 64 KB (byte range) and parses the header with pydicom (`stop_before_pixels`). If the header is truncated, it retries once with a larger range. From the header it takes the StudyInstanceUID (stored only as `study_uid_hash`), the view (laterality + ViewPosition, same rules as `collect_dicoms.scan_file`) and Modality.
2. **A study is queued as soon as it has the 4 views** (L_CC, L_MLO, R_CC, R_MLO). When the batch finishes crawling, studies still missing views become INCOMPLETE with the missing views listed. Duplicate views are still resolved at fetch time with `select_unique_views`.
3. **Skipped files are recorded**: files that are not DICOM, that have an unreadable header, or that are not MG. Each is stored with its relative folder, file name and reason.
4. **Reports pair folder with pseudonym.** Each study shows its folder (the common parent of its images, relative to the root) together with a short pseudonym (the first 12 hex characters of `study_uid_hash`). As the user decided, folder paths are kept so failures can be located.
5. **Reports are generated on demand from MySQL**, never stored, so they are always current:
   - `GET /batches/{id}/fallos.txt`: one line per problem with folder, pseudonym, status and reason, including skipped files.
   - `GET /batches/{id}/report.xlsx`: sheets Resumen, Estudios, Fallos and Leyenda. Cells are sanitized against formula injection, and the styling helpers come from `pipeline.py`.
   - The Streamlit panel gets a live batch progress view.

## Alternatives considered

| Option | Pros | Cons |
|--------|------|------|
| Group by StudyInstanceUID at crawl time (chosen) | Works with any layout or depth; queues a study when it is complete; no per-folder assumptions | One small ranged read per image during crawl |
| Top-level folder as unit (0001) | No reads during crawl | Breaks when folders are dates or codes, or nesting varies |
| Group at fetch time after downloading whole folders | No ranged reads | Downloads unrelated studies together; staging explodes on date folders |

## Consequences

- The crawler needs byte-range reads from every source. Filesystem and HTTP already support them; a server that ignores `Range` is cut off after the requested bytes.
- A study spread across several folders is assembled correctly.
- Folder paths stay in the database, so access to MySQL and to the reports must be restricted.

## Where it applies

- `MIRAI-ANALITIC/app/ingest/workers/crawler.py`, `ingest/workers/fetch_worker.py`, `ingest/schema.sql`, `ingest/service.py`
- `MIRAI-ANALITIC/app/api.py` (report endpoints)
- `MIRAI-ANALITIC/app/streamlit_app.py` (live progress)
