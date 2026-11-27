---
type: index
title: "MIRAI-ANALISI knowledge"
status: active
created: 2026-11-26
description: "Map of features, decisions, and constraints of MIRAI-ANALISI."
---

# MIRAI-ANALISI

**Stack:** Python 3.11 (FastAPI, Streamlit, pandas, pydicom), Docker Compose, official `mitjclinic/mirai` image (model 0.14.1); MySQL 8 planned.
**Architecture:** Docker services around the MIRAI model; new ingestion code follows ports and adapters.

## Features

- [URL batch ingestion](features/url-batch-ingestion.md) — walk a remote DICOM folder tree, assemble 4-view studies, score them with MIRAI in batches

## Decisions

- [0001 URL batch ingestion pipeline with MySQL as job queue](decisions/0001-url-batch-ingestion-mysql-queue.md) — superseded by 0003
- [0003 Study identity from DICOM headers at crawl time, and failure reports](decisions/0003-study-identity-from-dicom-headers.md) — accepted
- [0004 URL source access by private network ranges, API key, and probe on submit](decisions/0004-url-source-network-policy-and-api-key.md) — accepted
- [0002 Port layout from the MIT JCLINIC guide](decisions/0002-port-layout-mit-jclinic-guide.md) — accepted

## Constraints

- [Patient data pseudonymization](constraints/patient-data-pseudonymization.md) — active
- [Deployment network reachability](constraints/deployment-network-reachability.md) — active

## History

See [log](log.md).
