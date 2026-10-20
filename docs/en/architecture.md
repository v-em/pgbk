# Implementation decision — ADR-001

Version: 0.2.0. Date: 2026-10-20.

The development runner uses Python 3.11+ rather than the proposed Go implementation. REQ-035 permits a language decision in M0 through a bilingual architecture decision. Python provides the upstream PostgreSQL AST parser (pglast), psycopg snapshot coordination, the official Kubernetes client and boto3. Native backup utilities remain subprocesses; artifact transfers and hashes stream from disk. No Python code buffers an entire backup. This decision is provisional until all M0 and release gates pass; it does not weaken the source access, artifact or concurrency contracts.

The repository contains implementation and executable tests. Local feasibility evidence uses PostgreSQL and WAL-G in Linux amd64 containers and an Apache-2.0 Moto S3 emulator. Emulator results establish adapter behavior only; Yandex certification is performed separately by the operator. Production readiness additionally requires real S3/managed-provider, Kubernetes/PVC, TLS, failover, failure-injection and capacity evidence from the acceptance plan. Unavailable environments remain `not-run` or `blocked`.

Scratch uses a bounded emptyDir, a Kubernetes generic ephemeral PVC owned by the Pod, or an explicitly allowlisted dedicated claim. Leases are pre-created and never stolen, even after expiration. An orphan holder requires operator fencing before explicit removal. A repository lease serializes publication, download, restore, verification and cleanup; the holder is a durable pin. A completion marker references a canonical immutable manifest and is the last published object. Full-object verification always re-downloads and hashes, making the contract independent of ETags and optional provider checksums.

Physical output consists of upstream base/WAL/tablespace tar files and the upstream manifest. Validation uses an isolated extraction; physical restore rebuilds only explicitly mapped tablespace links and replaces source PostgreSQL configuration with safe isolated defaults. No source PVC or source container is used by the product.

Logical 0.2 supports custom/direct SDK multipart and custom-or-directory/staged WAL-G transfer. Directory archives are bundled flat regular files, safely extracted by restore. See [logical modes](logical-modes.md). The future optional UI uses the existing profile launch boundary; see [roadmap](roadmap.md).
