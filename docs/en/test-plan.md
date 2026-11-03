# PostgreSQL Backup for Kubernetes — test and acceptance plan

Version: 1.3\
Date: 2026-11-03\
Status: acceptance plan; retained local evidence and remaining full-case gates are in [compatibility](compatibility.md).  
Requirements: [English specification](specification.md). [Russian translation](../ru/test-plan.md).

The 1.2 amendment adds planned REQ-043–REQ-048 and T-046–T-057. These features/cases are not implemented or passed by virtue of this document update. Existing 0.2 evidence for the earlier contract does not certify the amendment; new cases start as `not-run`.

**1.3 acceptance amendment (planned):** REQ-049–REQ-055 and T-058–T-069 capture the agreed manual verification/status/retention and recovery contract. These cases start as `not-run`; this documentation change does not rerun or upgrade retained 0.2 evidence. First delivery is M5a (PostgreSQL 18/live Yandex/manual verification), not full M5b certification. Earlier 14–18 or scheduled-verification requirements below apply to M5b; run applicable first-delivery portions on PostgreSQL 18. Schema/log amendment cases remain planned and retain their own pending acceptance.

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
| T-001 | REQ-004, REQ-010, REQ-011, REQ-012 | Pin distributable tools; verify AGPL-3.0-only original-code metadata and LICENSE text while preserving dependency licenses; generate SBOM/notices; WAL-G raw `st put/get` round-trips binary files byte-for-byte with default transformations explicitly disabled. CLI error exits, custom endpoint, TLS and checksum behavior are documented. |
| T-002 | REQ-005, REQ-007, REQ-017 | With archive mode disabled and existing replication access, create a full physical copy including WAL; disconnect the source and restore. PostgreSQL reaches normal operation without an external WAL archive. Run PostgreSQL 18 for M5a; repeat for majors 14–18 for M5b. |
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

### 3.2. Schema selection acceptance (planned)

| ID | Requirements | Procedure and acceptance |
|---|---|---|
| T-046 | REQ-043, REQ-045 | Configure independent profiles sharing one target with include-only, exclude-only, overlapping lists and empty defaults. Inspect definitions/data in each archive: only the intended schema scope is present, exclusions win, other profiles are unchanged, and filtered/full labels and configured/resolved metadata are accurate. |
| T-047 | REQ-043, REQ-044 | Test multiple databases with differing schema inventories, unmatched include/exclude names, no effective included schema and an empty but existing schema. Reject invalid types/names/physical filters; deduplicate literal identifiers including case, quotes, dots, spaces, Unicode and pattern characters. No shell injection, unwanted wildcard matching or full-dump fallback. |
| T-048 | REQ-039, REQ-041, REQ-044 | Combine schema lists with manual/query table selection in all/selected modes, including parents/descendants in different schemas. Verify the resolved intersection and archive TOC, exclusion precedence and empty-selected rejection. Assert schema flags cannot be bypassed by --table. Repeat under concurrent schema/table/partition DDL: preserve scope/snapshot or fail. |
| T-049 | REQ-006, REQ-044 | Use a role allowed to export an application schema but unable to SELECT/LOCK tables in an excluded service schema. Backup succeeds without reading/locking excluded tables; catalog discovery remains read-only. Missing rights on an included table still fail with no completion marker. Query/snapshot prerequisites remain explicit. |
| T-050 | REQ-028, REQ-030, REQ-042, REQ-045 | Restore filtered fixtures with cross-schema types/functions/extensions/views/sequences/FKs and inheritance. Document prerequisites; do not silently add excluded data. Test large-object native defaults, explicit none/all, policy conflicts and globals independence. Verify archive scope separately from pre-provisioned restore prerequisites. |
| T-051 | REQ-018, REQ-019, REQ-044, REQ-045 | Repeat schema-only and combined schema/table filters on PostgreSQL 14–18 with custom staged/direct and directory parallel backups. Verify quoting, compression, archive inventories, full hashes, native restore and no publication on scope mismatch; do not rely on unsupported newer flags. |

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

### 4.1. Live logs and future UI contract acceptance (planned)

| ID | Requirements | Procedure and acceptance |
|---|---|---|
| T-052 | REQ-031, REQ-046, REQ-047 | Follow a running scheduled/manual Job with ordinary kubectl logs and the planned CLI, without debug Jobs/exec. Observe prompt phase/tool/progress/heartbeat records and terminal outcome. Inject a pg_dump permission failure: sanitized database/object/reason/tool/exit context is available, not just TOOL_FAILED. Unknown totals remain null and progress does not imply success. |
| T-053 | REQ-018, REQ-021, REQ-023, REQ-046 | Generate heavy parallel stderr, split UTF-8, very long lines and nonterminated messages while streaming an archive and renewing Leases. Stall/disconnect readers, cancel and fail the uploader/tool. Memory stays bounded, no pipe deadlock or missed terminal error occurs, leases/cancellation remain timely, archive bytes/hash stay unchanged and errors never produce completion. |
| T-054 | REQ-022, REQ-032, REQ-047 | Verify versioned NDJSON IDs/order/timestamps, cursor replay, snapshot-to-follow handoff, tail/since and duplicate/gap detection. Read live and incrementally persisted successful/failed/cancelled logs, including while a run is incomplete and after Pod/Job TTL. Validate log references/indexes, retention/pins and absence of backup completion markers for log-only records. |
| T-055 | REQ-014, REQ-046, REQ-047 | Inject passwords, tokens, DSNs, SQL literals, row values, multiline/split secrets and terminal/HTML-like object names into all tool paths. Neither live output nor persisted logs leak them or execute markup; useful safe diagnostics survive, unknown/redacted/truncated messages are explicit. Unauthorized cross-profile/run and pods/log access is denied. |
| T-056 | REQ-022, REQ-023, REQ-032, REQ-047 | Interrupt S3 log persistence, worker/node connectivity and Pod lifecycle. Local live logs remain usable without blocking the backup/control loop; recovered chunks, completeness flags and cursor gaps are honest. Document unrecoverable tail loss after node/Pod loss; diagnostic/log failure does not counterfeit backup success or modify completed backup validity. |
| T-057 | REQ-040, REQ-043, REQ-044, REQ-047, REQ-048 | Exercise structured discovery/preview and log consumers as a future UI adapter: pagination, authorization, identical resolver rules/config digest, query safeguards, schema/table provenance and unknown attribution. View filters never change backup selection/cursors/storage. Configuration edits affect future Jobs only; changing source objects, ad hoc command injection and unauthorized profile edits are rejected. |

## 5. Restore, monitoring, and release acceptance

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-030 | REQ-028 | Restore a logical archive into an explicitly empty separate database. Portable mode ignores source roles/ACLs; preservation mode requires existing roles. Under the default policy, a nonempty destination and configured/detected source identity are rejected; the explicit filtered-restore exception is covered in T-045. A failed restore is reported as partial, never successful. |
| T-031 | REQ-029 | Physical restore refuses nonempty/source/active PVCs, wrong major, incompatible environment and archive traversal. Recovery settings cannot connect to or execute archival commands against the original source. Normal startup succeeds in isolation. |
| T-032 | REQ-030, REQ-050, REQ-054 | Execute manual copy-ID drills for each backup kind in M5a; scheduled drills belong to M5b. Expected fixtures/assertions pass. Required assertion/startup failure gives verification-failed with report/history, without changing immutable artifact completion or deleting the copy; optional checks report separately. |
| T-033 | REQ-022, REQ-030, REQ-032 | Monitoring detects stale/missing successful backups, failed verification and failed cleanup even after Job TTL expiry. S3 outage produces an observable Kubernetes error; catalog recovers once access returns. |
| T-034 | REQ-012, REQ-034 | Benchmark representative 1 GiB and at least 20 GiB synthetic datasets for both kinds; publish backup/restore time, peak memory, scratch peak, transferred bytes and environment. Memory must not scale linearly with total artifact size; test configured deadlines/limits. No unmeasured RPO/RTO claim. |
| T-035 | REQ-001, REQ-002, REQ-007, REQ-025 | Complete the PostgreSQL 14–18 matrix on generic sources, a pinned Bitnami smoke test, restricted managed-like matrix, a live managed-provider logical smoke test, and Yandex backup/download/restore/retention. Mark each provider/version separately; live-test absence blocks its certification. |
| T-036 | REQ-004, REQ-031, REQ-035, REQ-036 | Package CLI/images/chart, SBOM/notices and documentation. Check English-only interface strings, complete EN/RU file pairing, equal requirement/test IDs, identical code examples, valid local links and no real credentials. Follow quickstart/restore guides in a fresh environment. |
| T-037 | REQ-037 | Review evidence for every requirement and release gate. All mandatory cases pass; any exclusions match the specification. Downloaded backups restore with the original source unavailable. Upload success alone never satisfies the gate. |

### 5.1. Agreed verification and delivery acceptance (planned)

| Test ID | Requirements | Procedure and pass condition |
|---|---|---|
| T-058 | REQ-001, REQ-007, REQ-010, REQ-055 | On live Yandex PostgreSQL 18, back up managed and Kubernetes logical sources to Object Storage; download and restore into operator-prepared isolated PostgreSQL 18, including managed-to-Kubernetes/ordinary-host and supported reverse destinations. Reject managed physical mode without fallback. Self-hosted physical uses pg_basebackup plus raw WAL-G transfer and starts without source/external WAL. Record provider/deployment versions and do not certify unavailable destinations. |
| T-059 | REQ-015, REQ-030, REQ-049, REQ-053 | Manually verify an explicit copy ID using a named PostgreSQL 18 environment profile for each backup kind. Capture Job/attempt IDs, required namespace/image digest/resources/PVC/deadline and optional SQL assertions. No verification schedule/automatic queue is needed. Invalid/missing settings fail before restore; references never expose secret values. |
| T-060 | REQ-022, REQ-024, REQ-050, REQ-053 | A successful integrity-valid creation returns exit 0 and status successful, including mandatory pending verification; compliance remains false until passed. Required passing/failing checks give verification-successful/verification-failed; optional checks report separately without invalidating backup success. Marker/manifest/artifact hashes remain unchanged; failed checks do not hide downloadable intact artifacts. Later check failure does not rewrite the earlier command exit code. |
| T-061 | REQ-030, REQ-049, REQ-052, REQ-053 | Verify startup, error-free restore, expected schema/table/index/constraint/sequence/large-object inventory and fixture data in full/filtered scope. Inject PVC/image/startup/environment/integrity/restore/assertion faults; mandatory verification fails with distinct sanitized reasons. Source is untouched, reports survive, no application-correctness guarantee is inferred from inventory alone. |
| T-062 | REQ-013, REQ-049, REQ-051 | Edit backup and verification profiles and rotate referenced Secrets after creating a copy. Captured nonsecret policy/environment/mandatory flag/deadline remain fixed; no secret values are stored. An explicit alternative verification environment is recorded without changing the obligation. New copies use new settings. |
| T-063 | REQ-021, REQ-051, REQ-052 | Fail, retry, pass, and retry-fail the same copy; immutable ordered reports preserve every result and latest completed attempt controls required status. An active retry does not clear failure. Late retry can pass without resetting original due time. Duplicate concurrent checks return busy with active attempt ID; different copies obey shared limits. Stale reconciliation cannot overwrite a newer pass. |
| T-064 | REQ-026, REQ-051, REQ-052 | With controlled time, pending mandatory checks are protected until due; absence of manual launch and a check still running at due become verification-failed/overdue. Service CronJob and read-time derivation agree, even with the CronJob unavailable. Expiry releases only waiting protection; active-operation and minimum-set protection remain. A later pass rehabilitates the copy without deadline reset. |
| T-065 | REQ-026, REQ-050 | Test BOTH N-copy retention minima by backup completion time and deterministic run-ID tie-breaks: zero/fewer-than-N verified, all failed/overdue, three newer failed copies with older verified copies, optional verification failure, age boundaries and distinct profiles. Preserve newest N integrity-confirmed and newest N profile-compliant copies. Verification timestamp never reorders the minima. |
| T-066 | REQ-021, REQ-026, REQ-049, REQ-053 | Race manual verification start against retention. Pin creation/deletion coordination prevents either checking a deleting/deleted copy or deleting an active check. Optional unstarted checks add no protection; overdue retry pins only its active operation. Verify normal cleanup, bounded failure-debug TTL and recovery after Job/node loss; never delete operator-provided instances/PVCs. |
| T-067 | REQ-032, REQ-034, REQ-050, REQ-055 | Monitor latest integrity-valid and latest profile-compliant copies separately. Verify an old copy today: its data age does not reset. Multiple databases expose individual snapshot times and conservative earliest time; unknown times remain labelled. Alert on mandatory failure/overdue even with older verified coverage. RPO/RTO are not required config fields or promised universal bounds. |
| T-068 | REQ-027, REQ-028, REQ-029, REQ-055 | Remove access to the original source AND original executor Kubernetes cluster. With surviving S3/catalog and credentials/keys, use a newly prepared executor/destination to download and restore logical and self-hosted physical copies. Native downloaded-artifact restore also works without the original catalog service. Do not treat a provider-internal backup as downloadable pgbk output or claim cloud-account-loss resilience. |
| T-069 | REQ-002, REQ-035, REQ-037, REQ-054, REQ-055 | Gate the first limited delivery on live PostgreSQL 18/Yandex and manual verification cases, clearly labelled. Later full release executes the 14–18 matrix, declared integrations and automatic every-copy/periodic-selective checks: exact selected IDs, bounded queue/concurrency, overdue failure and no silent copy substitution. These later portions and schema/log cases remain not-run until executed; old 0.2 evidence does not certify new status/retention behavior. |

## 6. Test layers and execution policy

1. **Every change:** unit tests for schema, retention, manifest/marker state, reason codes, lock state machine and subprocess argument construction; chart schema/lint/render checks; bilingual documentation consistency.
2. **Pull request integration:** real PostgreSQL logical and physical round-trips, S3 transfer checks, TLS/permissions, representative restore, and failure injection for changed components. Fake subprocesses alone do not establish backup correctness.
3. **Nightly/pre-release:** PostgreSQL 14–18 matrix, kind lifecycle/concurrency tests, failover, larger datasets, complete restore drills, and broader security/error cases.
4. **Release candidate:** all mandatory cases for the declared delivery stage plus separately authorized live-provider tests, including Yandex and managed PostgreSQL. Tests use pre-provisioned resources or explicitly approved disposable resources with cleanup and spending limits; access credentials are supplied by the environment.

Do not mark tests passed when prerequisites are absent. Distinguish `passed`, `failed`, `blocked` and `not-run`, retain diagnostics, and explain skipped provider certification. This document defines future tests; checking Markdown or matching IDs does not mean the application has passed them.

## 7. Release checklist

- M0 choices are documented with exact versions and evidence.
- All REQ-001 through REQ-055 are mapped; all T-001 through T-069 have a recorded outcome and delivery-stage label. M5a requires applicable PostgreSQL 18/manual cases including T-058–T-068 and the M5a portion of T-069; M5b also requires the full matrix and automated cases. Planned amendment cases remain `not-run` until executed; earlier evidence must not imply support for the new contract.
- Restore succeeds for both kinds using only S3 artifacts plus a compatible destination environment.
- No source administrative mutation, secret leakage, unprotected publication, or deletion outside the configured scope is observed.
- Helm package, worker images, CLI artifacts and checksums are reproducible and versioned.
- English and Russian user documents match the shipped configuration and command surface.
- Any outstanding issue that can corrupt a backup, expose an incomplete backup as usable, lose protected copies, or modify a source blocks release.
