# Manual copy verification and retention

Specification 1.3 implementation. Local PostgreSQL 18 evidence is recorded separately from live-provider and full-release acceptance in [compatibility](compatibility.md). Schema filters (REQ-043–REQ-045) are implemented in 0.3.1. Execution log access (REQ-046–REQ-048) and verification automation (REQ-054) remain deferred.

Configure named environments at the root of values. The following is an overlay: retain the target, storage, logical, execution and scratch settings of the existing profile. Replace the image digest and provide existing namespaces, ServiceAccounts, RBAC, Leases and referenced Secrets, or explicitly opt into chart bootstrap. The image must contain pgbk and matching PostgreSQL server/client/extension binaries. The configured positive maxConcurrent uses shared pre-created Lease slots; exhausted capacity returns busy instead of queuing.

```yaml
verificationEnvironments:
  pg18-isolated:
    namespace: verification
    serviceAccountName: pgbk-verifier
    concurrencyKey: isolated-pg18
    maxConcurrent: 1
    image: registry.example/pgbk-worker-18@sha256:0000000000000000000000000000000000000000000000000000000000000000
    resources:
      requests: {cpu: "500m", memory: 512Mi}
      limits: {cpu: "2", memory: 2Gi}
    scratch: {type: pvc, size: 40Gi, storageClassName: standard}
    timeoutSeconds: 21600
    failureDebugTtlSeconds: 86400
    credentialsSecretRef: verification-server
    clientCredentialsSecretRef: verification-client
    assertions: ["SELECT NOT pg_is_in_recovery()"]
verificationReconciliation:
  enabled: true
  cron: "*/5 * * * *"
  timeZone: UTC
profiles:
  example-logical:
    verification:
      enabled: true
      environmentRef: pg18-isolated
      required: true
      dueWithinSeconds: 86400
```

Verification defaults to disabled. Set enabled with environmentRef for optional manual checks; add required and positive dueWithinSeconds for a per-copy obligation. An enabled verification schedule is rejected: the reconciliation schedule only records deadlines and recovers proven terminal attempts. It never launches a restore test. Legacy inline environment fields must move to verificationEnvironments.

At backup completion the manifest captures the nonsecret policy, due time, environment, configuration digest, retained ConfigMap reference and Secret references. Later profile edits do not change these obligations. Referenced secret values may rotate; keep the old namespace, ConfigMap, identity, RBAC, Secrets and image available for retries. An explicitly selected alternative environment is recorded without changing the captured deadline or requirement.

```sh
pgbk --config values.local.yaml --namespace backups verify --profile example-logical --run-id RUN_ID --verification-profile pg18-isolated
pgbk --config values.local.yaml --namespace backups verify --profile example-logical --run-id RUN_ID --verification-profile pg18-isolated
pgbk --config values.local.yaml --namespace backups verification reconcile --profile example-logical
pgbk --config values.local.yaml --namespace backups --output json backup describe --profile example-logical --run-id RUN_ID
```

Manual verify requires an exact copy ID and explicit --verification-profile. Selecting the captured environment name uses its immutable settings; use a different named profile to request an alternative. It reads the catalog using local projected S3 credentials and reserves the attempt under repository coordination before creating the Job. Operators need named Lease get/update, backup CronJob get and Job create in the selected verification namespace. Workers receive no source credentials or source workload mutation permissions. Unlike backup run, verify needs catalog credentials on the CLI host. Its immediate JSON response identifies the Job, attempt and copy; the Job's exit code and immutable report give the final result. Use kubectl logs on the returned Job and describe the copy afterward.

An integrity-valid completed copy is successful, including required verification still pending; backup creation exits 0. Required passes/failures expose verification-successful/verification-failed and profileCompliant. Optional failures are reported separately and leave status successful. Failed verification does not rewrite manifest/marker/artifact hashes or prohibit downloading an intact copy.

The disposable server runs only on loopback/private socket. Verification checks hashes, startup, restore exit status, archive/restored schema/table/index/constraint/sequence inventory, table row counts and optional bounded read-only SQL assertions. Custom dependencies remain environment prerequisites. Inventory alone does not establish application correctness. Reports distinguish integrity, environment, startup, restore, assertion and Job failures. Worker logs retain the existing sanitized diagnostic contract; richer live/replay logs are deferred.

Attempts have immutable ordered reports. A running retry preserves the latest completed result. Duplicate requests return VERIFICATION_BUSY with activeAttemptId. Required copies without a passing governing attempt become overdue at their original due time, even when no check was launched or a check is still running. Reads derive this state without the service CronJob. A later pass rehabilitates the same copy without resetting data age or its due time.

Retention preserves the newest N integrity-confirmed copies and the newest N profile-compliant copies, ordered by backup completion time and run-ID ties; these sets can differ. Before its due time an unstarted required check also protects its copy. At expiry this waiting protection ends; an active operation pin and both minima remain. Optional unstarted checks do not add protection. Pin creation and deletion are serialized by the repository Lease, preventing verification of deleting copies and deletion during a check.

Finished reports are saved before normal scratch cleanup. Pod-owned claims follow Job/Pod TTL and storage-controller reclamation; existing claims are never removed. Reconciliation may close attempts only after the matching Job and stopped containers establish termination. Missing API objects or node loss do not prove fencing: preserve the pin and occupied Lease, fence the prior worker/server, and recover the exact recorded holder through compare-and-swap as described in [operations](operations.md). Keep failure evidence before removing resources. After independently fencing the exact worker, release its recorded slot/pin using the explicit recovery command below; a mismatched attempt is refused.

```sh
pgbk --config values.local.yaml --namespace backups verification recover --profile example-logical --run-id RUN_ID --attempt-id ATTEMPT_ID --acknowledge-worker-fenced
```

List JSON exposes coverage.latestIntegrityConfirmed, coverage.latestProfileCompliant and mandatory failure alerts separately. Per-database snapshotAt is a conservative pre-dump lower bound, labelled backup-start-lower-bound; unknown timestamps stay unknown. A completion-time-fallback is explicitly labelled for legacy copies. Verification timestamps never refresh data age. Measure actual restore/provisioning/cutover duration for operator RPO/RTO objectives.


## Local validation

The reproducible driver tests the newly built worker against real PostgreSQL 18, S3 and Kubernetes APIs. Use a local Moto endpoint accessible from the node, provide a pinned local worker image, and retain private logs. The driver provisions only synthetic test sources/namespaces/Secrets and removes its namespaces afterward. It verifies the running source-tree SHA-256 against the workspace before exercising Jobs. For an ARM minikube node, --allow-amd64-emulation explicitly enables a test-only scheduling override; the production chart still requires Linux amd64. A separate host S3 endpoint may route the same real emulator through a port forward without changing repository identity.

```sh
python tests/kubernetes/spec13.py --context pgbk-local --worker-image "$WORKER_IMAGE" --s3-endpoint "$NODE_S3_ENDPOINT" --s3-host-endpoint "$HOST_S3_ENDPOINT" --logs /tmp/pgbk-validation
```

Run tests/integration/offline_recovery.py in a fresh matching worker container with --network none, a writable /scratch, and read-only downloaded logical/physical directories at /archives, after stopping the original executor. The test checks restored fixture values, sequence state, partitions, constraints and large objects. Reports must identify emulation and excluded cloud certification; historical reports do not certify a newly changed image.
