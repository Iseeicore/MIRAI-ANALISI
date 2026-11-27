---
type: decision
title: "Port layout from the MIT JCLINIC guide"
status: accepted
created: 2026-11-26
tags: [docker, ports, mirai, sybil]
supersedes: null
superseded_by: null
related: []
---

# 0002 Port layout from the MIT JCLINIC guide

## Context

The official MIRAI/Sybil installation guide (MIT JCLINIC, Spanish translation, October 2026) assigns host ports 8885/8886 to MIRAI and 8887/8888 to Sybil. The project's Streamlit panel used 8887, which would clash with Sybil.

## Decision

- `mirai`: `8885 -> 5000` and `8886 -> 8000` (the image exposes both).
- 8887/8888 stay reserved for Sybil.
- Own services live outside that range: `dicom-analyzer-api` on 8889 and the `dicom-analyzer` Streamlit panel on 8890.
- The DICOM folder mounts as `${DICOM_HOST_DIR:-./data}:/dicom:ro` so Windows host paths work.
- The compose subnet is `${COMPOSE_SUBNET:-172.30.0.0/24}` and can be overridden when it overlaps another Docker network.

## Alternatives considered

| Option | Pros | Cons |
|--------|------|------|
| Guide ports, own services moved (chosen) | Matches the official docs; Sybil can be added later | Panel URL changed from 8887 to 8890 |
| Keep panel on 8887 | No URL change | Clashes with Sybil as documented |

## Consequences

- Anyone following the official guide finds MIRAI where the guide says.
- On hosts where 172.30.0.0/24 is taken, set `COMPOSE_SUBNET`.

## Where it applies

- `docker-compose.yml`
- `README.md`
