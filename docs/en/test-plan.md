# PostgreSQL Backup for Kubernetes — test and acceptance plan

Version: 1.1  
Date: 2026-10-09  
Status: acceptance plan; retained local evidence and remaining full-case gates are in [compatibility](compatibility.md).  
Requirements: [English specification](specification.md). [Russian translation](../ru/test-plan.md).

## 1. Test environments and evidence

The test harness provisions disposable PostgreSQL fixtures and their permissions before invoking the product. Source provisioning belongs to the test harness, not the product. No test may use real production data.

Required environments:

- PostgreSQL 14, 15, 16, 17, and 18, with each worker using matching-major tools.
- A generic Kubernetes StatefulSet and an available pinned Bitnami deployment; test artifacts must record chart/image provenance and licensing. If a reproducible distributable Bitnami image cannot be obtained, report the integration as unverified rather than silently substituting another image.
- A restricted non-superuser source fixture that simulates managed-service constraints, plus at least one real managed PostgreSQL service smoke test before claiming provider certification. Record the provider/version tested; emulation is not provider certification.
- An open-source S3-compatible test server for routine CI, and a real Yandex Object Storage bucket for release certification. Tests are isolated under unique disposable prefixes.
- kind or equivalent Kubernetes, testing the declared minimum and a current supported version, plus a cluster with the production-relevant storage class for PVC lifecycle certification.
- TLS fixtures: valid CA, unknown CA, expired certificate, wrong hostname, and optional client certificate.

Fixtures contain at least two databases; multiple schemas; quoted/Unicode database and object names; tables with PK/FK/check constraints; indexes; sequences; views; large objects; partitioned tables; an extension; roles/ACLs; and an RLS-protected table. A physical fixture includes an additional tablespace and WAL on a separate path. A workload generates committed writes during backup and markers before, during, and after it.

Every test report records test ID, requirement IDs, source version, client/WAL-G versions, image digests, chart/Kubernetes versions, S3 provider, fixture seed, run IDs, result, elapsed time, and sanitized logs. Hashes/counts are compared against an expected consistent fixture state, not a changing live source. Concurrent-write tests use invariant checks and transaction markers with documented snapshot semantics.

## 2. Feasibility gate — must pass before M1

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-001 | REQ-004, REQ-010, REQ-011, REQ-012 | Pin distributable tools; generate SBOM/notices; WAL-G raw `st put/get` round-trips binary files byte-for-byte with default transformations explicitly disabled. CLI error exits, custom endpoint, TLS and checksum behavior are documented. |
| T-002 | REQ-005, REQ-007, REQ-017 | With archive mode disabled and existing replication access, create a full physical copy including WAL; disconnect the source and restore. PostgreSQL reaches normal operation without an external WAL archive. Repeat for majors 14–18. |
| T-003 | REQ-006, REQ-008, REQ-016 | Restricted managed-like role dumps an explicitly permitted database without superuser or settings changes. Lack of access to optional metadata is reported as unknown. Insufficient table/RLS permissions fail rather than publish a partial backup. |
| T-004 | REQ-009, REQ-015, REQ-019, REQ-025 | Prove scratch lifecycle, archive validation, bounded-memory upload, Yandex endpoint configuration, and trustworthy remote integrity verification. Record exact SDK/tool APIs and temporary space requirements. |

An M0 failure changes the design through a bilingual architecture decision and updated tests. Do not replace WAL capture with an undocumented assumption or mark unsupported functionality implemented.

## 3. Functional and integrity acceptance

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-005 | REQ-001, REQ-002, REQ-013 | Configure several targets and both profile kinds; each runs against its declared target and writes only to its own catalog scope. One failing target does not invalidate other targets' successful runs. |
| T-006 | REQ-003, REQ-013, REQ-015 | Unknown keys, missing fields, invalid references/schedules/time zones, wrong backup kind, incremental/PITR settings, negative retention, and traversal IDs fail validation before source access. |
| T-007 | REQ-005, REQ-033 | Compare source Kubernetes specs, settings, grants, installed extensions and permanent slots before/after success, failure and cancellation; no administrative mutations. Use audit logs/RBAC denials to confirm no Pod exec or source-volume attachment. Temporary slots disappear when connections end. |
| T-008 | REQ-006, REQ-016 | Backup two named databases, restore both, compare schema/data/large-object/sequence results. Per-database snapshot semantics are reported. Permission failure in the second database prevents the overall completion marker. |
| T-009 | REQ-006, REQ-016, REQ-028 | Default mode performs no globals export. Explicit globals succeeds with adequate rights and excludes role passwords; inadequate rights fail the run. Restore does not automatically execute globals. |
| T-010 | REQ-007, REQ-008, REQ-012 | SQL-only credentials, unavailable replication, insufficient WAL senders, pooled physical endpoint, unsupported source major and unexpected configured major are rejected with actionable reasons; no configuration repair/fallback occurs. |
| T-011 | REQ-017, REQ-018, REQ-029 | Physical restore includes both databases, global roles, required WAL and correctly mapped tablespaces. Source is unreachable throughout recovery. Test compatible extension installation and clear failure when it is missing. |
| T-012 | REQ-018, REQ-024 | Change physical source identity or trigger promotion during backup; no completed inconsistent run. Old completed runs stay visible. Reject an unacknowledged new system identifier. |
| T-013 | REQ-019, REQ-024 | Corrupt/truncate artifacts or manifest; listing/download/restore refuses invalid completion. Exercise both trustworthy S3 checksum and download-and-hash verification paths, including multipart uploads. ETag is not accepted as SHA-256 evidence. |
| T-014 | REQ-024 | Kill worker before artifacts finish, after manifest upload, and immediately after marker PUT. Only a valid final marker exposes a completed run; lost PUT response is reconciled without overwriting or deleting the completed run. |
| T-015 | REQ-019, REQ-027 | Download a complete run and a single logical archive; verify exact bytes and native restore without the source cluster/CLI. Reject unsafe paths and conflicting local files; retain original artifact filenames through the manifest mapping. |
| T-016 | REQ-026 | With a controlled clock, verify retention deletes only backups satisfying BOTH age and minimum-count rules. Cover fewer than three copies, all copies older than 30 days, boundary timestamps, ties, and separate profiles. |
| T-017 | REQ-021, REQ-026 | Retention cannot delete active, pinned, verified, restored or downloaded copies. Interrupted cleanup resumes; denied deletion/Object Lock reports cleanup failure. No sibling prefix or other installation is touched. |
| T-018 | REQ-024, REQ-026 | Old incomplete uploads are removed only after grace and confirmed writer termination. Never reap a slow active writer. Uninstall leaves all completed remote copies discoverable by a fresh installation with the same catalog identity. |

### 3.1. Table selection acceptance

Run these cases against PostgreSQL 14–18; include a restricted managed-like source. Extend fixtures with a reporting schema, a selection-control table, multi-level partitions/inheritance, external FK/type/function dependencies, and identifier names containing dots, quotes, spaces, Unicode and literal wildcard characters.

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-038 | REQ-016, REQ-038, REQ-039 | In `mode: all`, exclude manually named tables and inspect the archive: their definitions/data are absent, other tables and ordinary non-table objects retain normal pg_dump behavior. An unconfigured database in the same profile still receives an ordinary full dump. Reject filters on physical profiles and nonempty include in `all` mode. |
| T-039 | REQ-038, REQ-039, REQ-042 | In `selected` mode, back up only a manual table list. The archive contains exactly the selected table data and upstream-dependent objects, without unrelated table data. Deduplicate names; exclusions override inclusions. Verify `filtered-database` reporting and native restore with required prerequisites. |
| T-040 | REQ-039, REQ-040, REQ-041 | Build include/exclude lists using catalog queries and a query to a user-maintained selection table. Test query-only, manual-only and mixed sources; each query runs once per configured database per attempt. Manual/query union and exclusion precedence match expected sets. A new attempt reflects changed query results; its own frozen selection is recorded. |
| T-041 | REQ-005, REQ-014, REQ-040 | Reject extra/wrong columns, wrong types, NULL/empty names, nonexistent relations, SQL errors, missing query privileges, multi-statements, writing CTEs, SELECT INTO and DDL/DML. Query timeout/row overflow fails before pg_dump. Confirm read-only transaction behavior, bounded memory and no source changes/query-text leakage. Do not claim to sandbox side-effecting functions. |
| T-042 | REQ-039, REQ-040 | Empty selected result, absent include source or an exclusion that removes all inclusions fails without starting a full dump or publishing completion. Empty exclusion results are valid; duplicate rows count toward the raw row limit. Explicitly missing names fail; queries returning zero rows are handled according to their role. |
| T-043 | REQ-039, REQ-041 | Test exact identifier quoting, literal pattern characters, parent/descendant expansion, child exclusion under an included parent, and a selected leaf. No unintended sibling/ancestor data or command execution. Unknown object kinds fail explicitly; repeat without relying on pg_dump flags absent from PG14. |
| T-044 | REQ-018, REQ-024, REQ-041 | Change selection-control rows and issue concurrent create/rename/drop/attach-partition DDL during filtered backup. Data/list resolution respect the exported snapshot and frozen set or the run fails; no unintended table is silently included. Test unavailable snapshot support and lock timeouts. Validate manifest lists, provenance, query digests, archive scope and CLI labels. |
| T-045 | REQ-028, REQ-030, REQ-042 | Restore filtered fixtures with external schema/type/extension/FK/parent prerequisites: default-empty mode reports missing prerequisites; explicit prerequisites-only destination succeeds without overwriting existing selected relations. Test owned sequences, indexes and constraints. Large objects are omitted by default for selected mode, explicitly all included when requested, and retain ordinary behavior in all mode. Automated drills validate the filtered scope; full-database restore tests still pass. |

## 4. Kubernetes execution and failure injection

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-019 | REQ-009, REQ-020, REQ-031 | Install chart, run CLI backup, run native `kubectl create job`, and observe a scheduled backup. All use the same immutable worker configuration and produce compatible reports. Suspended schedule permits manual execution. |
| T-020 | REQ-020 | Verify cron/time-zone rendering, missed deadline, suspension/resumption and duplicate scheduling. Evaluate daylight-saving transitions with a controllable scheduler test rather than waiting for calendar events. Duplicates use distinct artifact IDs. |
| T-021 | REQ-013, REQ-021 | Race two manual Jobs, manual vs scheduled, and two profiles/aliases for one source. Only one starts source work; other runs return `SOURCE_BUSY`. Different sources can progress concurrently. |
| T-022 | REQ-021, REQ-023 | Lose API connectivity/Lease renewal, kill a worker, partition its node, and attempt another run. Existing child operations stop on lease loss; a successor does not start while a predecessor might still run. Ambiguous orphan ownership requires explicit recovery. |
| T-023 | REQ-022, REQ-023 | Cancel during dump, verification and upload; enforce deadline; kill Pod/node. No completion marker is published for incomplete data; process state, reason and cleanup outcome remain inspectable. |
| T-024 | REQ-019, REQ-023, REQ-025 | Inject S3 throttling, timeout, dropped final response, expired credentials and access denial. Transient faults retry within bounds; authorization faults fail clearly. Incomplete multipart data never appears as a complete backup. |
| T-025 | REQ-015, REQ-017, REQ-034 | Exhaust scratch, simulate PVC provisioning failure/read-only mount and tool checksum error. Run fails without source mutation or completion marker; error identifies the failing phase and resource. |
| T-026 | REQ-009, REQ-015, REQ-033 | Test bounded emptyDir, dynamic PVC and existing scratch PVC; cleanup success/failure TTL and uninstall behavior. Reject source data PVCs and unsafe shared scratch use. Capture behavior on RWO storage and node rescheduling. |
| T-027 | REQ-012, REQ-014, REQ-033 | Upgrade Helm config/images and rotate Secrets during an active run. Running config remains fixed; new Jobs use the new config/Secret. Rollback leaves existing artifacts valid. No implicit backup/restore hooks. |
| T-028 | REQ-014, REQ-033 | Valid TLS passes; invalid CA/expiry/hostname fails; explicit test-only non-TLS source works. Verify least-privilege RBAC and NetworkPolicy options. Worker cannot read arbitrary Secrets or mutate source workloads. |
| T-029 | REQ-014, REQ-031, REQ-032 | Inject distinctive secrets and command-like database names. Search logs, argv, ConfigMaps, manifests and reports for leakage; no command injection or raw secrets. Errors remain actionable and JSON/exit codes match contract. |

## 5. Restore, monitoring, and release acceptance

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-030 | REQ-028 | Restore a logical archive into an explicitly empty separate database. Portable mode ignores source roles/ACLs; preservation mode requires existing roles. Under the default policy, a nonempty destination and configured/detected source identity are rejected; the explicit filtered-restore exception is covered in T-045. A failed restore is reported as partial, never successful. |
| T-031 | REQ-029 | Physical restore refuses nonempty/source/active PVCs, wrong major, incompatible environment and archive traversal. Recovery settings cannot connect to or execute archival commands against the original source. Normal startup succeeds in isolation. |
| T-032 | REQ-030 | Execute manual and scheduled restore drills for each backup kind. Expected fixtures/assertions pass. Inject an assertion/startup failure; separate verification status and report persist without changing backup completion or deleting it. |
| T-033 | REQ-022, REQ-030, REQ-032 | Monitoring detects stale/missing successful backups, failed verification and failed cleanup even after Job TTL expiry. S3 outage produces an observable Kubernetes error; catalog recovers once access returns. |
| T-034 | REQ-012, REQ-034 | Benchmark representative 1 GiB and at least 20 GiB synthetic datasets for both kinds; publish backup/restore time, peak memory, scratch peak, transferred bytes and environment. Memory must not scale linearly with total artifact size; test configured deadlines/limits. No unmeasured RPO/RTO claim. |
| T-035 | REQ-001, REQ-002, REQ-007, REQ-025 | Complete the PostgreSQL 14–18 matrix on generic sources, a pinned Bitnami smoke test, restricted managed-like matrix, a live managed-provider logical smoke test, and Yandex backup/download/restore/retention. Mark each provider/version separately; live-test absence blocks its certification. |
| T-036 | REQ-004, REQ-031, REQ-035, REQ-036 | Package CLI/images/chart, SBOM/notices and documentation. Check English-only interface strings, complete EN/RU file pairing, equal requirement/test IDs, identical code examples, valid local links and no real credentials. Follow quickstart/restore guides in a fresh environment. |
| T-037 | REQ-037 | Review evidence for every requirement and release gate. All mandatory cases pass; any exclusions match the specification. Downloaded backups restore with the original source unavailable. Upload success alone never satisfies the gate. |

## 6. Test layers and execution policy

1. **Every change:** unit tests for schema, retention, manifest/marker state, reason codes, lock state machine and subprocess argument construction; chart schema/lint/render checks; bilingual documentation consistency.
2. **Pull request integration:** real PostgreSQL logical and physical round-trips, S3 transfer checks, TLS/permissions, representative restore, and failure injection for changed components. Fake subprocesses alone do not establish backup correctness.
3. **Nightly/pre-release:** PostgreSQL 14–18 matrix, kind lifecycle/concurrency tests, failover, larger datasets, complete restore drills, and broader security/error cases.
4. **Release candidate:** all mandatory test cases plus separately authorized live-provider tests, including Yandex and managed PostgreSQL. Tests use pre-provisioned resources or explicitly approved disposable resources with cleanup and spending limits; access credentials are supplied by the environment.

Do not mark tests passed when prerequisites are absent. Distinguish `passed`, `failed`, `blocked` and `not-run`, retain diagnostics, and explain skipped provider certification. This document defines future tests; checking Markdown or matching IDs does not mean the application has passed them.

## 7. Release checklist

- M0 choices are documented with exact versions and evidence.
- All REQ-001 through REQ-042 are covered; all T-001 through T-045 have a recorded outcome.
- Restore succeeds for both kinds using only S3 artifacts plus a compatible destination environment.
- No source administrative mutation, secret leakage, unprotected publication, or deletion outside the configured scope is observed.
- Helm package, worker images, CLI artifacts and checksums are reproducible and versioned.
- English and Russian user documents match the shipped configuration and command surface.
- Any outstanding issue that can corrupt a backup, expose an incomplete backup as usable, lose protected copies, or modify a source blocks release.
