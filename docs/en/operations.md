# Operations, prerequisites and recovery

Version: 0.2.0. Start with [quickstart](quickstart.md) and [configuration](configuration.md). Acceptance evidence and untested gates are in [compatibility](compatibility.md).

## Existing source prerequisites

The product accesses PostgreSQL over TCP, without changing source settings, grants, deployments or PVCs. An administrator provisions a login role with CONNECT on each declared database, USAGE on required schemas, SELECT on exported tables/sequences and appropriate access to large objects. SQL selection additionally needs read access to selection-control tables. RLS/table permission failures terminate the run; the product never disables RLS or repairs privileges. Optional version/encoding/extension/system-identifier metadata can remain unknown for a restricted role. Globals are disabled by default; enabling them requires upstream pg_dumpall permissions and does not make a multi-database logical profile a physical instance backup.

For generic Kubernetes or Bitnami sources, use their existing network Service and existing credentials/TLS configuration; do not attach their data volumes or exec into their Pods. Physical-full requires the intended direct primary endpoint, existing replication authentication, sufficient WAL senders for basebackup plus streamed WAL, readable source files and expectedSystemIdentifier. A SQL pooler/managed logical endpoint is not a physical endpoint. Existing WAL/replication configuration must already permit pg_basebackup; archive_mode need not be enabled. No permanent slot is created. Physical copies contain every database, globals and WAL, including the configured tablespaces; source external configuration files and extension binaries are separate restore prerequisites.

Managed services are logical targets subject to provider privileges, restricted objects and export capacity. Provider-specific and live managed certification are pending. No fallback to physical or a privileged role occurs. Connect through verified TLS with hostname/CA matching the actual endpoint; weakening TLS requires an explicit configuration change. Credential/CA/client certificate files are projected from existing Secrets, never placed in values, argv or logs. Physical archives can contain sensitive source configuration: restrict bucket/download access accordingly.

## Deployment RBAC and existing identities

The chart creates no Namespace. Create operation/coordination namespaces beforehand, or explicitly use Helm --create-namespace for the release namespace. To reuse ServiceAccounts set `profiles.<id>.serviceAccounts.create: false` and provide existing operation account names; [overlay example](../../examples/existing-service-accounts.yaml). Accounts live in the namespace of the respective Jobs. Separate identities keep source coordination apart from repository-only work; verification/restore can use identities in their designated namespaces.

The normal values disable RBAC, ServiceAccount and Lease creation. Helm needs namespaced get/list/watch/create/update/patch/delete for ConfigMaps, CronJobs and optional NetworkPolicies where they are rendered. Role/RoleBinding, ServiceAccount and Lease management is needed only for explicitly enabled administrator bootstrap. Workers must already have the runtime rights on pre-created named Leases; disabled RBAC management does not bypass authorization. Its default release state uses Secrets in the release namespace. Manual operators additionally need CronJob get, Job create/get/patch (cancel), Pod/log get and PVC get/Pod list for selected claims. Workers receive get/update on explicit Lease resourceNames, backup/repository status get on Jobs, and only required allowlisted PVC get/Pod list. They receive no Secret API reads, source workload mutation, Pod exec, cluster-wide grants or create-Job rights. Kubelet Secret projection does not require worker Secret API permissions.

[Normal deployer Role](../../examples/deployer-rbac.yaml) excludes identity/RBAC/Lease management. NetworkPolicy permissions are omitted because it is disabled by default; grant them separately only when enabling it. Secrets rights here are for Helm release storage. Read/PVC rights and Lease get/update for direct catalog commands are separate operator permissions. [Administrator installer Role example](../../examples/installer-rbac.yaml) assumes one pre-existing backups namespace. An administrator bootstraps its RoleBinding and replaces the subject. Omit serviceaccounts from that installer Role if all accounts are pre-existing. For multiple namespaces grant the corresponding namespaced subset in each; do not use this example as a ClusterRoleBinding. Kubernetes accepts Role/RoleBinding creation only if the installer already has the contained rights or explicit bind/escalate authority; the example grants contained rights instead of bind/escalate. See [official RBAC documentation](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#restrictions-on-role-creation-or-update).

```sh
kubectl auth can-i create cronjobs.batch -n backups
kubectl auth can-i update leases.coordination.k8s.io -n backups
kubectl auth can-i create rolebindings.rbac.authorization.k8s.io -n backups
helm upgrade --install pgbk charts/pgbk --namespace backups -f values.local.yaml -f examples/existing-service-accounts.yaml
```

## S3 and Yandex

Provision a bucket, endpoint, access keys and policy externally. Configure Yandex endpoint `https://storage.yandexcloud.net`, region `ru-central1`, path addressing and existing access-key Secret. This maps directly to the [Yandex S3 API](https://yandex.cloud/en/docs/storage/s3/); live testing is performed by the operator. Scope list/read/write/delete and multipart upload/abort permissions to the required bucket/catalog prefix; test exact provider policy behavior. Bucket encryption may be managed externally; provider-specific encryption headers are not configured by this version.

WAL-G st uploads/downloads raw bytes with compression/encryption transformations explicitly disabled. boto3 stores metadata and independently re-reads every artifact for SHA-256. ETag is never an integrity proof. Metadata/transient transfer retries are bounded and deadline-limited; failed dumps are not resumed. Configure an external AbortIncompleteMultipartUpload bucket lifecycle rule for orphan multipart data after node/process loss; the product does not alter bucket policy/lifecycle. Archive bytes are not WAL-G backup-push format and cannot be restored with backup-fetch.

## Manual and scheduled work

```sh
pgbk --config values.local.yaml --namespace backups backup run --profile example-logical
kubectl -n backups create job example-manual --from=cronjob/pgbk-example-logical-backup
kubectl -n backups logs job/example-manual
pgbk --config values.local.yaml --namespace backups backup cancel --profile example-logical --job JOB_NAME
pgbk --config values.local.yaml --namespace backups retention run --profile example-logical
```

Schedules use an explicit IANA timezone, default UTC. Kubernetes scheduling is best effort; daylight-saving gaps/repeated times can skip/repeat runs. startingDeadlineSeconds bounds missed starts; there is no catch-up/exactly-once guarantee. Every attempt has a new random run ID. Source Lease covers manual/scheduled/alias profiles. A repository Lease serializes publication/download/restore/drill/cleanup and pins the entire operation; long operations may make catalog polling return busy. API renewal failure stops tools and blocking SQL; expired holders are never stolen.

For an orphan Lease, first establish termination of the worker, all children and disposable server, including fencing a partitioned node/process. A deleted Pod/Job API object or expired time is insufficient. Record the incident, read the holder/resourceVersion and clear only that expected holder through a Kubernetes compare-and-swap update. Never blanket-delete Leases during cleanup or a Helm upgrade. Shared sources/repositories require consistent coordination namespace/keys across installations; independent cross-cluster writers are unsupported.

## Download and offline restore

```sh
pgbk --config values.local.yaml --namespace backups backup download --profile example-logical --run-id RUN_ID --destination /tmp/isolated-download --database application
kubectl -n backups label pvc download-output pgbk.io/installation=example-cluster pgbk.io/purpose=download
pgbk --config values.local.yaml --namespace backups backup download --profile example-logical --run-id RUN_ID --destination /destination/RUN_ID --destination-pvc download-output --acknowledge-dedicated-pvc
```

Use a dedicated empty destination; local downloads must have no symlink parents. A download PVC must be unused and labelled for this installation/purpose, never source PGDATA. Download includes manifest.json, DOWNLOAD.json and English/Russian RESTORE instructions; a single database download lists only downloaded IDs. Validate SHA-256 and use matching-major native pg_restore or safe physical extraction/pg_verifybackup without the original cluster or catalog service. The native instructions ship in the package and each download. Direct pgbk catalog operations require Kubernetes repository coordination; native restore of downloaded bytes does not. The optional scripts/restore_download.py helper verifies downloaded hashes and prepares/restores them without S3/Kubernetes access; it still requires separate destination credentials and isolation acknowledgement.

## Logical and physical restore

```sh
pgbk --config values.local.yaml --namespace backups restore logical --profile example-logical --run-id RUN_ID --database application --destination-config examples/destination.json
pgbk --config values.local.yaml --namespace backups restore logical --profile example-logical --run-id RUN_ID --database application --destination-config examples/destination.json --as-job
```

Replace [destination.json](../../examples/destination.json) placeholders with an isolated TCP endpoint/database and separate credentials. Defaults: acknowledgeIsolated required, destinationPolicy empty, portable no-owner/no-acl/no-tablespaces. preserveOwnership explicitly retains ownership/ACL/tablespace requirements. No destructive clean/drop or globals SQL execution is automatic. Endpoint/IP and detected system-identifier matches to sources are refused; administrative isolation remains required because aliases and restricted metadata can hide identity. A failed native restore is reported as RESTORE_PARTIAL and requires emptying/recreating the destination before retry.

For filtered archives, prepare known schemas/types/extensions/FK/parent prerequisites and explicitly choose `destinationPolicy: prerequisites-only`. Selected relations must not already exist. Known prerequisites are checked; incomplete recursive dependency discovery can still cause a clear native restore failure. Drills automatically create selected schemas only; custom dependencies must be present in the compatible image/destination or the drill fails.

Enable the following fragments inside a profile to opt into isolated Jobs. All namespaces/Secrets/images/claims must already exist and placeholders must be replaced. You can enable either restore or verification independently.

```yaml
restore:
  enabled: true
  namespace: restore
  timeoutSeconds: 21600
  credentialsSecretRefs: [restore-postgres, restore-ca]
  allowedDestinationClaims: [isolated-pgdata]
  resources:
    requests: {cpu: "500m", memory: 512Mi}
    limits: {cpu: "2", memory: 2Gi}
  scratch: {type: pvc, size: 40Gi, storageClassName: standard}
verification:
  enabled: true
  namespace: verification
  image: registry.example/pgbk-worker-17@sha256:0000000000000000000000000000000000000000000000000000000000000000
  schedule: {enabled: true, cron: "0 5 * * 0", timeZone: UTC}
  timeoutSeconds: 21600
  failureDebugTtlSeconds: 86400
  credentialsSecretRef: verification-server
  clientCredentialsSecretRef: verification-client
  resources:
    requests: {cpu: "500m", memory: 512Mi}
    limits: {cpu: "2", memory: 2Gi}
  scratch: {type: pvc, size: 40Gi, storageClassName: standard, retainFailed: true}
  assertions: ["SELECT NOT pg_is_in_recovery()"]
```

```sh
kubectl -n restore label pvc isolated-pgdata pgbk.io/installation=example-cluster pgbk.io/purpose=restore
pgbk --config values.local.yaml --namespace backups restore physical --profile example-physical --run-id RUN_ID --major 17 --pgdata /destination/pgdata --tablespace-map '{"16384":"/destination/tablespace-16384"}' --destination-pvc isolated-pgdata --acknowledge-isolated-stopped
```

Use actual tablespace OIDs from the manifest; the mapping must contain exactly all tablespaces. All Job restore paths must be below /destination, in empty nonoverlapping directories. Physical destination PVC must be allowlisted, labelled purpose=restore, unused, dedicated and administratively confirmed stopped/isolated. The worker rechecks ownership/Pod attachments after mounting. RWO is not node fencing: stop/fence any ambiguous predecessor before launch. Existing destination claims are never deleted.

Physical restore validates artifact hashes, archive paths, compatible major/architecture and pg_verifybackup before replacing source postgresql.conf/postgresql.auto.conf/HBA/identity mappings and recovery signals with isolated defaults. Report lists changed filenames without credentials. The server is not started by restore; deployment, compatible build/extensions, external config and production cutover remain operator actions. Without --destination-pvc, the direct command prepares explicitly acknowledged local/mounted directories; it does not certify PVC ownership. Use the Job path for product-enforced PVC checks.

## Verification, retention and monitoring

```sh
pgbk --config values.local.yaml --namespace backups verify --profile example-logical --run-id RUN_ID
pgbk --config values.local.yaml --namespace backups --output json backup list --profile example-logical
```

The verification Job uses only repository/destination credentials, restores into disposable scratch, starts PostgreSQL on loopback/private socket, scans user tables and reports catalog counts plus optional read-only assertions. Physical verification uses only downloaded data/WAL; source connectivity is unnecessary. Each report records timestamps, image/tool versions, scope checks and passed/failed independently of backup completion. Failure files remain only when retainFailed is configured, until failureDebugTtlSeconds removes the finished Job/Pod; successful files are cleaned immediately. Actual PVC reclamation follows the cluster storage policy.

Retention deletes only runs older than maxAgeDays AND outside the newest minSuccessfulBackups, ordered by completion time. Repository pins protect downloads/restores/drills. Deletion records precede marker withdrawal; partial deletions resume. Old incomplete runs require grace plus a terminal writerStopped event; node-loss orphans remain for fenced operator recovery. Cleanup/verification errors never reclassify successful backup manifests. Helm uninstall leaves remote data, existing Secrets/PVCs, kept configuration snapshots and coordination Leases; remove kept snapshots only after dependent Jobs finish.

Poll durable backup list/describe JSON for newest completedAt, durationSeconds, bytes, diagnosticRuns, lastCleanup and restoreVerification.status/finishedAt. For a daily profile, alert when no usable copy exists or newest completedAt is older than 48 hours; alert independently on latest failed verification or cleanup. Catalog errors/busy are unknown observations, not evidence of freshness. Use an external poller that persists its last observation and distinguishes unknown from healthy. This version re-hashes completed artifacts when listing: account for S3 traffic/time; an event-only Job metric is not adequate freshness monitoring.

## Capacity and troubleshooting

Size logical scratch for all custom archives plus tool temporary space. Physical backup needs tar artifacts AND an extraction for verification, including WAL/tablespaces; drill/restore needs downloaded archives AND expanded PGDATA/tablespaces. Reserve headroom for WAL activity and filesystem metadata. EmptyDir sizeLimit eviction depends on kubelet monitoring; enforce node ephemeral-storage budgets operationally. Per-profile CPU/memory limits, deadlines and staggered schedules bound work but do not promise throughput/RPO/RTO. The local 1/20-GiB exercises and their limits are documented in [compatibility](compatibility.md); also measure usage and duration on your own source and storage.

Exit codes: 0 success, 2 invalid input, 3 prerequisite/unsupported, 4 busy, 5 execution, 6 integrity, 7 cancellation, 8 authorization. Native stderr is reduced to safe classifications/tool exit codes to avoid credential/query leakage; use trusted upstream/server audit logs for detailed diagnostics.

| Reason code | Action |
|---|---|
| `INVALID_CONFIG / INVALID_REFERENCE` | Fix schema/reference before accessing any source. |
| `CLIENT_MAJOR_MISMATCH / SERVER_MAJOR_MISMATCH` | Select matching-major worker and acknowledged source version. |
| `POSTGRES_CONNECTION_FAILED / ACCESS_DENIED` | Check existing permissions, TLS, endpoint and Secret projection. Native diagnostics are redacted. |
| `SOURCE_BUSY / LEASE_LOST` | Do not steal the holder; terminate/fence prior processes and investigate renewal failure. |
| `SOURCE_IDENTITY_MISMATCH / SOURCE_IDENTITY_CHANGED` | Re-establish intended direct primary and system identifier; rerun with a new run ID. |
| `EMPTY_TABLE_SELECTION / QUERY_ROW_LIMIT / SELECTION_FAILED` | Inspect include/exclude sources, raw row cap, read privileges and query/lock timeout. |
| `UNSAFE_PVC / PVC_IN_USE / PVC_OWNERSHIP_UNVERIFIED` | Use a dedicated labelled allowlisted claim; verify all attachments and node fencing. |
| `SCRATCH_UNWRITABLE / SCRATCH_IO_FAILED` | Check capacity, filesystem ownership and storage class; preserve Kubernetes events. |
| `BACKUP_INCOMPLETE / ARTIFACT_HASH_MISMATCH / MANIFEST_MISMATCH` | Run is unusable; recover a different verified run and investigate storage corruption. |
| `S3_ACCESS_DENIED / S3_RETRY_EXHAUSTED / DELETE_DENIED` | Check keys/expiry/policy/network or Object Lock. Cleanup failure is separate from backup validity. |
| `RESTORE_SOURCE_REFUSED / DESTINATION_NOT_EMPTY` | Provision an isolated empty destination. Never point restore at a source. |
| `RESTORE_PREREQUISITE_MISSING / RESTORE_PARTIAL` | Supply known filtered prerequisites. After partial failure empty/recreate destination before retry. |
| `VERIFICATION_ASSERTION_FAILED / VERIFICATION_STARTUP_FAILED` | Inspect separate drill report and retained scratch; do not delete the completed backup. |
| `CANCELLED / DEADLINE_EXCEEDED` | Inspect Job state/logs; no incomplete run is usable. A new attempt uses a new ID. |

For 0.2 logical transfer/compression and directory restoration, see [logical modes](logical-modes.md). Direct mode uses multipart SDK upload, not WAL-G transfer.
