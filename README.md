# pgbk — PostgreSQL backups for Kubernetes

Development implementation 0.2.0: Python CLI/worker, Helm chart and matching-major Linux amd64 images for PostgreSQL 14–18. Logical custom/directory archives and self-contained physical copies are stored in an immutable S3 catalog; direct logical streams use the S3 SDK and staged files use WAL-G raw transfer, with SHA-256 verification, renewable coordination, scoped retention, download, isolated restore and restore drills.

The normal deployment uses existing namespaces, ServiceAccounts, RBAC permissions, Leases, sources, Secrets and buckets. It creates no source workload and changes no source settings. Start with the [quickstart](docs/en/quickstart.md), [configuration](docs/en/configuration.md) and [operations/RBAC](docs/en/operations.md). [Russian documentation](README.ru.md) is maintained alongside English.

The PostgreSQL 14–18 local real-tool matrix and Kubernetes API/RBAC smoke reports are in [tests/reports](tests/reports). [Compatibility and release gates](docs/en/compatibility.md) distinguish completed local subcases from full certification. Live Yandex testing is performed separately by the operator; amd64 Kubernetes/PVC and TLS/fault-injection release gates remain open. This distribution is not labeled production-ready.

- [Specification v1.1](docs/en/specification.md) and [acceptance plan](docs/en/test-plan.md).
- [Architecture decision](docs/en/architecture.md), [image pins](docs/compatibility.json), [acceptance ledger](tests/reports/acceptance.json).
- [Example values](examples/values.yaml), [existing accounts](examples/existing-service-accounts.yaml), [deployer rights](examples/deployer-rbac.yaml).
- [CLI source](src/pgbk), [Helm chart](charts/pgbk), [worker build](images/Dockerfile), [CI](.github/workflows/test.yaml).

Version 1 excludes incremental backups, continuous WAL archival, PITR, HA management, an operator and source provisioning. Filtered logical archives record their exact scope and restore dependencies. Artifacts are project format v1, not a WAL-G backup-push repository; use pg_restore or verified physical extraction, never backup-fetch.

Original code is Apache-2.0; see [LICENSE](LICENSE), [NOTICE](NOTICE) and [third-party notices](THIRD-PARTY-NOTICES.txt). Package/SBOM/checksum outputs are generated under dist using Makefile and release scripts.

New in 0.2: [direct S3 / parallel staged backup, compression and two sources](docs/en/logical-modes.md); [future UI roadmap](docs/en/roadmap.md). This directory is the public repository. Keep environment overlays and private development materials in a separate sibling `pgbk-private` repository; credentials remain outside Git. Install with `uv sync --frozen`, test with `uv run --frozen pytest`, build with `uv build`.
