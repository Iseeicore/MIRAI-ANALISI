---
type: constraint
title: "Patient data pseudonymization"
status: active
created: 2026-11-26
tags: [phi, anonymization, privacy]
related: [../decisions/0001-url-batch-ingestion-mysql-queue.md, ../features/url-batch-ingestion.md]
---

# Patient data pseudonymization

## Restriction

No direct patient identifier may be stored in the database or in reports. Batch ingestion must not start on real data until the current gaps are fixed.

## Known gaps in `MIRAI-ANALITIC/RETROSPECTIVE/anonymize_dicoms.py`

- StudyInstanceUID, SeriesInstanceUID and SOPInstanceUID are kept unchanged (`:18-19`). `estudios_analizados.study_uid` stores the original StudyInstanceUID.
- The pseudo-id is a random `uuid4` per study (`:77`): the same patient gets a different id on every run, so studies cannot be linked.
- The id mapping is only printed to stdout (`:100-102`).
- The whole tree is loaded into memory before writing (`:69-73`), and there is no error handling, so one bad file aborts the run.

## Status 2026-11-26 (stage 3 done)

The four gaps above are fixed in `anonymize_dicoms.py`, which is now the single anonymizer for the workers, `/ingest` and the Streamlit zip mode. Pseudonyms are deterministic HMAC values keyed by `PSEUDONYM_SECRET` (required, at least 32 chars, never committed; changing it breaks linkage across batches). UIDs are rewritten as `2.25.<int>`, files are processed one at a time, and corrupt files are reported without aborting. Verified: two batches of the same data produce identical pseudonyms, and a MySQL dump contains no original StudyInstanceUID or PatientID.

Closed the same day:
- The Streamlit "server path" and "upload" modes now pseudonymize exams and duplicate views (`pipeline.pseudonymize_dropped`) before anything is shown, exported or stored, and refuse to show results without the secret. Checked: the Excel built from the demo study contains no original StudyInstanceUID or PatientID.
- The leftover test rows (MySQL batch 1 and the SQLite row with the original demo UID) were deleted.

Still applies: folder paths are kept by user decision (decision 0003), so access to MySQL and to the reports must be restricted.

## Required behavior

- Pseudonyms are deterministic: `HMAC-SHA256(PSEUDONYM_SECRET, PatientID)`, and the same for UIDs. The secret comes from an environment variable and is never committed.
- Source folder names contain patient names (for example `SURNAME_SURNAME_NAME_DATE`): store only a hash as `source_key`.
- Pseudonymize per study inside the fetch worker, never a whole tree at once.
