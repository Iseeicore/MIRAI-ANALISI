---
type: decision
title: "URL source access by private network ranges, API key, and probe on submit"
status: accepted
created: 2026-11-26
tags: [ingestion, security, ssrf, api]
supersedes: null
superseded_by: null
related: [0003-study-identity-from-dicom-headers.md, ../features/url-batch-ingestion.md]
---

# 0004 URL source access by private network ranges, API key, and probe on submit

## Context

Decisions 0001 and 0003 required every URL host to be listed in `INGEST_ALLOWED_HOSTS` beforehand. In practice the operator hands a ready-made `curl` to a supervisor, who fills in the URL of an image server on the same hospital network. That server is not known in advance, so a fixed host allowlist would reject the request. The SSRF risk remains: anyone who can reach the API could point the backend at internal services, cloud metadata, or the Docker network (MySQL, MIRAI).

This decision amends the host-allowlist consequence carried over from 0001 into 0003. The rest of 0003 stays.

## Decision

1. **Network-range policy instead of a fixed host list.** `INGEST_ALLOWED_NETWORKS` (CIDR list) defaults to the private ranges `10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16`. Always denied, even inside allowed ranges:
   - loopback, link-local (`169.254.0.0/16`, `fe80::/10`), `0.0.0.0/8`, multicast and broadcast;
   - the compose network (`COMPOSE_SUBNET`) and the Docker default bridge (`172.17.0.0/16`).

   Public IPs are denied unless explicitly added. The hostname is resolved and **every resolved IP** must pass. For `http`, the request connects to the validated IP and sends the original `Host` header (IP pinning against DNS rebinding). Redirects stay refused. `INGEST_ALLOWED_HOSTS` remains as an explicit override for named hosts; it is used by the e2e test, whose server lives on the compose network.
2. **API key.** The batch endpoints (`POST /batches`, `GET /batches/...`, and the reports, which contain folder paths) require the `X-API-Key` header, matching one of `INGEST_API_KEYS` (comma-separated, at least 32 chars each, no default). `/health` stays open. Keys are compared in constant time. A missing or wrong key gets 401.
3. **Probe on submit.** `POST /batches` lists the root before creating the batch. If the host is unreachable, the response is not a directory listing, the listing is empty, or the policy rejects the URL, the request fails at once with a 4xx and a clear reason, and no batch is created.

## Alternatives considered

| Option | Pros | Cons |
|--------|------|------|
| Private ranges + deny list + API key + probe (chosen) | Works with unknown servers on the LAN; blocks the dangerous targets; immediate feedback | Still trusts the whole private network; needs key management |
| Fixed host allowlist (0001/0003) | Tightest scope | Rejects the real workflow (server not known in advance) |
| No restriction | Simplest | Open SSRF from anyone who reaches the API |

## Consequences

- The supervisor's `curl` works with any image server on the private network, with no reconfiguration.
- Losing an API key means anyone with it can start batches. Rotate it by editing `INGEST_API_KEYS`.
- If the hospital network uses public IP space internally, add it to `INGEST_ALLOWED_NETWORKS`.

## Where it applies

- `MIRAI-ANALITIC/app/ingest/sources.py`, `ingest/adapters/http_index_source.py`, `ingest/service.py`
- `MIRAI-ANALITIC/app/api.py`
- `docker-compose.yml`, `README.md`
