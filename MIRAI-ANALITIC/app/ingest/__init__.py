"""Batch ingestion of mammography studies: crawl a source, stage and pseudonymize, score with MIRAI.

Ports and adapters layout: `domain` (pure rules), `ports` (interfaces), `adapters` (filesystem,
MySQL, MIRAI HTTP), `workers` (crawl / fetch / score use cases), entry points `worker` and `cli`.
"""
