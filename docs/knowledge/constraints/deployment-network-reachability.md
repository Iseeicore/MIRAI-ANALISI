---
type: constraint
title: "Deployment network reachability"
status: active
created: 2026-11-26
tags: [deployment, network, firewall, windows]
related: [../decisions/0004-url-source-network-policy-and-api-key.md, ../features/url-batch-ingestion.md]
---

# Deployment network reachability

## Restriction

Clients (for example, a supervisor sending `POST /batches`) can only reach the API if they share a local network with the machine that runs `docker compose`. A developer PC connected to the hospital through a client VPN (SonicWall NetExtender) and to the internet through a mobile router has a private address behind NAT. Hospital machines cannot open connections to it; the VPN only carries outbound traffic, which is enough for image downloads but not for incoming requests. Production use therefore requires running the stack on a machine inside the hospital network.

## Windows host checklist (verified 2026-11-26)

- Docker publishes the API on `0.0.0.0:8889` and the panel on `0.0.0.0:8890`. MySQL stays on `127.0.0.1:3307`.
- An inbound firewall rule for TCP 8889 and 8890 is required. If the network is classified **Public** (common on Wi-Fi and mobile routers), the rule needs `-Profile Any`: `Set-NetFirewallRule -DisplayName "MIRAI API" -Profile Any`. Prefer widening that one rule over reclassifying the whole network as Private.
- A test from a container to the host's LAN IP returns no answer even when real LAN clients can connect (the WSL/Hyper-V firewall filters that path). Verify from a real device on the same network instead: with the rule applied, a phone on the same Wi-Fi got `200` from `/health`.

## Where it applies

- `README.md` section "Probar desde otra máquina de la red (paso a paso)"
- `docker-compose.yml` (published ports)
