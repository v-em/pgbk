# Quickstart

For a GitOps deployment with Argo CD, see the detailed [guide](argocd-deployment.md).

Version: 0.2.0. Development build; release certification is incomplete. See [compatibility and evidence](compatibility.md), [configuration](configuration.md) and [operations](operations.md). The [specification](specification.md) remains the version-1 acceptance contract.

Install the CLI in a Python 3.11+ virtual environment. Helm 3.19.0 is the tested renderer. Kubernetes must support CronJob time zones (declared minimum 1.27; minimum-version and amd64 Job certification remain pending). The initial worker architecture is Linux amd64.

```sh
uv sync --frozen --no-dev
cp examples/values.yaml values.local.yaml
uv run --frozen pgbk --config values.local.yaml config validate
```

Replace every example endpoint, database, bucket, storage class and zero image digest. The normal configuration creates no namespaces, ServiceAccounts, RBAC rules, Leases, PostgreSQL source, bucket or credentials. Pre-existing accounts need get/update on the exact coordination Leases and the documented optional Job/PVC read permissions; have an administrator prepare them beforehand. See operations for runtime names/permissions. Optional administrator bootstrap is available through examples/access-bootstrap.yaml, but is not part of the normal deployment. An administrator must first provision access and existing Secrets in the release namespace. PostgreSQL Secrets contain `username` and `password` by default; targets may override either key via `credentialsSecretKeys`; S3 Secrets contain `accessKeyId`, `secretAccessKey` and optional `sessionToken`. CA Secrets contain `ca.crt`; client TLS Secrets contain `tls.crt` and `tls.key`. Supply credential files through your normal secret-management process; never put passwords in values or command arguments.

Build one worker per configured PostgreSQL major using immutable upstream bases in `docs/compatibility.json`. Push to your registry using your normal release process, record the resulting registry digest, and configure `images.<major>` with that digest. Image tools are installed at build time. No image is published automatically by this repository.

```sh
python3 scripts/build_images.py --major 17 --repository registry.example/pgbk-worker
helm lint charts/pgbk -f values.local.yaml
helm upgrade --install pgbk charts/pgbk --namespace backups -f values.local.yaml
pgbk --config values.local.yaml --namespace backups backup run --profile example-logical
kubectl -n backups create job example-manual --from=cronjob/pgbk-example-logical-backup
kubectl -n backups logs job/example-manual
```

Keep `coordinationNamespace` consistent with the existing namespace you use. Aliases and installations accessing the same source/repository must share the coordination namespace and keys. Independent cross-cluster writers are unsupported. Manual and scheduled Jobs use the same immutable configuration snapshot and process. A suspended backup schedule still supports manual Jobs.

The local CLI reads projected credential files under `PGBK_SECRET_ROOT/<SecretName>/<key>` when accessing S3/PostgreSQL directly. Set `PGBK_SECRET_ROOT` to a private directory containing files supplied by your secret manager. `backup run` and `verify` create Jobs and do not require exporting those credentials locally. Use the download Job path in [operations](operations.md) to keep S3 access inside Kubernetes. Kubernetes credentials come from the selected context/KUBECONFIG, or the worker's projected service account.

```sh
pgbk --config values.local.yaml --namespace backups --scratch /tmp/pgbk preflight --profile example-logical
pgbk --config values.local.yaml --namespace backups --output json backup list --profile example-logical
pgbk --config values.local.yaml --namespace backups --output json backup describe --profile example-logical --run-id RUN_ID
```

Preflight returns `unverified` when read-only checks pass but actual export or replication capacity remains unproven. A usable backup requires successful tools, archive validation, complete uploads, remote SHA-256 verification and the final completion marker. Successful uploads alone do not certify restore correctness.

For Managed PostgreSQL plus Kubernetes PostgreSQL, start with the [two-source example and modes](logical-modes.md).
