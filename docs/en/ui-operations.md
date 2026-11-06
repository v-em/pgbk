# Operating pgbk-ui v1

The independent `pgbk-ui` image and chart implement the [web UI v1 specification](ui-v1.md) against the pgbk 0.3.3 contracts. The console has Backups, Schedules and Jobs views. Its only backup-management write is a one-off backup. The worker release remains 0.3.3; the UI chart has its own 0.1.0 lifecycle.

## Build and install

Build from the repository root. The UI uses packaged static assets and Python's HTTPS server; no JavaScript build service or CDN is required. All Python dependencies remain in the existing frozen lock. The default UI base image and uv stage are pinned by digest. Supply `PYTHON_BASE` when qualifying another base.

```sh
docker build -f images/ui/Dockerfile -t pgbk-ui:0.1.0 .
helm lint charts/pgbk-ui -f examples/ui-values.yaml
helm upgrade --install pgbk-ui charts/pgbk-ui -n backup-console --create-namespace -f ui.local.yaml
```

Copy [the synthetic values example](../../examples/ui-values.yaml) into a local file. Configure the installation ID, backup Helm release and namespace, exact allowed profile/source/kind pairs and S3 storage prefixes. Set `configMapNames` to the immutable ConfigMap names actually referenced by the current backup CronJobs. A configuration or scope mismatch disables launch and appears in Schedules. Allowlist any additional verification/restore Job namespaces explicitly.

Create the TLS Secret outside Helm and Git. Its certificate needs SANs for the browser hostname and `<ui-release>.<ui-namespace>.svc`. The server always uses TLS on port 8443; the Service exposes 443. An ingress controller must use HTTPS to the backend and serve the configured `config.origin` exactly. Configure its HTTPS backend annotation in `ingress.annotations`, or use an existing HTTPS reverse proxy. Certificate issuance and renewal belong to the installation operator. Referenced TLS, CA and S3 Secrets must exist in the UI namespace. Their names and mount paths may appear in Helm values; their credential values must not.

Use separate read-only S3 credentials. Permit listing only the configured catalog roots and reading their `runs/*/manifest.json`, `complete.json`, `deletion.json`, `recovery/BACKUP-RESTORE*.md`, `events/*/*.json` and `verification/*/*.json` keys. Deny archive bodies, object writes and deletes. S3 credentials are mounted from existing Secrets under `/s3/<name>`; the UI has no Kubernetes Secret API permission. PostgreSQL and worker backup credentials are never mounted into the UI.

## Vault and administrator sessions

Use one installation-specific KV v2 secret containing `username` and `password`. Set `config.vault.address` to an HTTPS Vault URL, `role` to a Kubernetes auth role and `secretPath` to the KV v2 API data path, such as `secret/data/pgbk/example/administrator`. `authMount` defaults to `kubernetes`; an optional `caFile` points to a mounted CA bundle. The role must bind only the UI ServiceAccount and namespace. Its policy grants only `read` on that exact data path, without list or write access. Configure the Vault Kubernetes auth method and its TokenReview privileges through the existing Vault administration path.

Every protected request checks the current Vault secret version. Credentials are compared in memory and never stored by the UI. Rotation rejects previous sessions; Vault failures reject both login and protected data reads with `AUTHENTICATION_UNAVAILABLE`. Cookies are Secure, HttpOnly, SameSite=Strict, host-only and expire by default after one hour. A CSRF token is returned only to an authenticated same-origin client; every browser write also checks Origin. Login is rate-limited in SQLite by peer address and globally. No API request bodies, URLs, cookies or credential values are logged. Audit decisions and authenticated usernames are kept in the journal.

## Enable one-off launches

The default is view-only. To enable launch, set `config.manualLaunch` and `admission.enabled` to true and provide `admission.caBundle`, the base64 PEM CA that signs the UI's service certificate. The chart rejects manual launch without admission. Installing the validating webhook requires permission to manage cluster admission configuration. The webhook matches only the UI ServiceAccount identity, so its outage does not block CronJob-controller or worker operations.

The UI identity may create Jobs only in the backup namespace. Admission checks the complete Job spec, image, arguments, ServiceAccount, environment and volumes against the current trusted backup template. It also checks the reserved journal request, profile, deterministic name and template version. Unknown changes fail closed; custom mutating webhooks that alter the worker template must be qualified before enabling launch. The UI has no CronJob write, Lease, Secret or Pod log permissions. [Kubernetes webhook behavior](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/) and [Vault KV v2 paths](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2) describe the upstream contracts.

Opening confirmation issues a random server-recorded launch key valid for 24 hours. The browser retains it in memory for retries. Atomic reservation precedes the Kubernetes request. A replay verifies Job identity or returns the recorded result after TTL deletion. A reserved request without a known Job is reported as Outcome unknown; it never silently creates another Job. Inspect Jobs/catalog events before intentionally opening a new confirmation. An unavailable journal disables launch. A paused valid backup CronJob remains manually launchable. Schedules are never edited by a launch.

## Persistence, metadata and upgrades

Use a dedicated PVC and one active replica. The Deployment uses Recreate. Keep and back up the journal PVC: it contains server-side sessions, issued launch keys, request outcomes, audit records and a rebuildable projected metadata index. The chart retains the PVC on uninstall; reinstallation with the same release name reuses it. UI removal leaves the pgbk release, schedules, source data and S3 artifacts intact. Remove a retained PVC only after the retry/history requirements have expired and a deliberate administrator decision.

The index refreshes up to four prefixes concurrently, with a default 30-second interval. Partial initial scans and per-prefix failures are visible with the last successful refresh. Direct backup and recovery reads revalidate the publication marker and deletion record. Page requests never GET or HEAD artifact bodies. Metadata projection omits unknown fields and redacts nested internal endpoints, Secret references and SQL; JSON is displayed as escaped text with key search. Artifact hashes are labelled SHA-256 from manifest. Recovery notes are fetched individually, size/hash checked and displayed as literal text in the writer's original order. No replacement instructions are synthesized for missing or corrupt notes.

Jobs display Kubernetes execution, persisted worker phase and restore verification independently. Correlation requires namespace and Job UID within the same profile/source scope. An API access failure never proves Job removal. Worker `ACCESS_DENIED` links to globals-export guidance; UI session/API denials have separate codes. Historical `TOOL_FAILED` records retain an unknown underlying cause.

## Local validation

```sh
uv sync --frozen --group integration
uv run --frozen pytest
uv run --frozen python scripts/check_docs.py
uv run --frozen --group integration python tests/kubernetes/ui_v1.py --helm helm --allow-amd64-emulation --serve
```

The Kubernetes harness creates an isolated namespace and generated credentials outside the repository. It uses real PostgreSQL 18, Kubernetes and Vault 1.18.0 with a local TLS fixture proxy, and TLS Moto S3. It checks scheduled publication before/during/after UI installation, paused manual launches, concurrent requests, admission/RBAC, UID correlation, stored notes, restart, credential rotation, removal and reinstall. `--serve` keeps the HTTPS UI on localhost:18443 for browser QA; interrupting the process cleans up its resources. The emulation flag is for local ARM Macs only and explicitly removes the worker's architecture node selector in the test fixture. Production worker platform restrictions remain unchanged. The JSON report records limitations: Moto does not certify cloud IAM, Vault dev mode is a fixture, and ingress/cloud integration requires separate validation.

For visual QA in a browser that does not trust the fixture CA, `tests/ui_preview.py` serves a separate synthetic, read-only preview on loopback port 18444. It has no Kubernetes/Vault/cloud connections, contains no operational credentials and denies all writes. This fixture is never a production authentication mode. It exercises the packaged frontend independently of the real HTTPS API acceptance checks. Trusting a local certificate in a browser remains an operator action.
