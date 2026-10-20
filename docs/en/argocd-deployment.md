# Deployment with Argo CD

This guide installs `pgbk` into Kubernetes from Git with Argo CD. It assumes PostgreSQL and an S3-compatible bucket already exist, and that you build and publish the worker image manually to your own registry. The solution does not create PostgreSQL, a bucket, a namespace, or credential Secrets.

## What gets installed

The Helm chart is in `charts/pgbk`. It creates a configuration ConfigMap and profile CronJobs; when enabled, it also creates retention, verification, download, or restore CronJobs. A backup worker runs as a Job on a schedule or when triggered manually. Images must be built in advance and available to Kubernetes nodes.

Start with one `logical` profile and leave the backup schedule disabled until validation is complete. For `physical-full`, separately validate the direct replication endpoint and the requirements in [operations](operations.md). By default, the chart does not create ServiceAccounts, RBAC, or Leases. These objects and permissions must be prepared by an administrator first.

## 1. Build and publish the worker

You need Docker or Podman/Buildah with multi-stage build support, registry access, and the matching PostgreSQL base image from `docs/compatibility.json`. The build script uses `images/Dockerfile`, targets `linux/amd64`, and creates `:17-test` as a local intermediate tag:

For Podman, use `python3 scripts/build_images.py --engine podman --major 17 --repository registry.example/pgbk-worker` and run login/tag/push using `podman`. The WAL-G checksum is verified with `sha256sum`; BuildKit extensions are not required. Integration checks still use Docker.

```sh
docker login registry.example
python3 scripts/build_images.py --major 17 --repository registry.example/pgbk-worker
docker tag registry.example/pgbk-worker:17-test registry.example/pgbk-worker:17-0.2.0
docker push registry.example/pgbk-worker:17-0.2.0
docker buildx imagetools inspect registry.example/pgbk-worker:17-0.2.0
```

Replace the registry and tag version. Build a separate image for each PostgreSQL major version you use. Use immutable tags and/or prohibit tag overwrites in the registry. After pushing, obtain the manifest/image digest from the registry output or `docker buildx imagetools inspect`; set the full `registry.example/pgbk-worker@sha256:…` reference in values, not a mutable tag. The chart requires a digest-pinned image for each configured major version.

For a private registry, configure `imagePullSecrets` using the cluster's standard process. For example, create a `kubernetes.io/dockerconfigjson` Secret in each operation namespace and connect it through a pre-created ServiceAccount. Do not put registry credentials in Git. Backup workers run in the release namespace; restore and verification may run in separate namespaces, which must also have access to the image and pull credentials.

## 2. Prepare Kubernetes and Argo CD access

1. Check the cluster: the chart declares Kubernetes `>=1.27`, and worker images target Linux amd64. CronJobs use `spec.timeZone`.
2. Create the release namespace, such as `backups`, and any restore/verification namespaces that will be used. The chart does not create namespaces.
3. Create the named coordination Leases in the namespace configured by `coordinationNamespace`. Installations sharing a source or repository must coordinate using a common namespace and keys.
4. Create operation ServiceAccounts and grant minimum permissions as described in [operations](operations.md#installation-rbac-and-existing-service-accounts). With `rbac.create`, `coordination.createLeases`, and `serviceAccounts.create` disabled, Argo CD will not create these resources. For a single shared namespace, adapt `examples/installer-rbac.yaml` during administrator bootstrap. A regular deployer can use `examples/deployer-rbac.yaml` when all accounts and Leases already exist. Scope examples to actual namespaces and resource names; do not grant Argo CD cluster-wide permissions just to install the chart.
5. Prepare the existing PostgreSQL and S3 bucket, network/DNS/TLS and bucket policy. For a physical profile, PostgreSQL must allow the required replication/base-backup access.

Check the Argo CD identity's sync permissions in every affected namespace. A standard profile needs permissions to manage ConfigMaps and CronJobs; Helm release state also requires Secret read/create/update in the release namespace. If NetworkPolicy is enabled, add the corresponding permissions. Since creating RBAC/ServiceAccounts/Leases through the chart is administrative bootstrap, do not enable it unless an administrator has granted those rights.

## 3. Create Secrets outside Git

Secrets must exist in the namespace where the corresponding Job runs. A regular backup profile needs:

| Purpose | Secret keys |
|---|---|
| PostgreSQL target | `username`, `password` |
| S3-compatible storage | `accessKeyId`, `secretAccessKey`; optionally `sessionToken` |
| PostgreSQL CA, if used | `ca.crt` |
| Client TLS certificate, if used | `tls.crt`, `tls.key` |
| Private S3 CA, if used | `ca.crt` |

For example, an administrator can create a Secret from protected files (the files must come from a secret manager and must not enter Git):

```sh
kubectl -n backups create secret generic backup-postgres \
  --from-file=username=/secure/path/pg-user \
  --from-file=password=/secure/path/pg-password
kubectl -n backups create secret generic backup-s3 \
  --from-file=accessKeyId=/secure/path/s3-access-key-id \
  --from-file=secretAccessKey=/secure/path/s3-secret-access-key
```

In production GitOps, manage these objects through the chosen secret manager/controller, such as External Secrets or Sealed Secrets. A plain Kubernetes Secret committed in clear text or merely base64-encoded is not a safe storage method. If restore/verification use another namespace, create their Secrets independently there. The chart does not copy Secrets across namespaces.

## 4. Prepare the GitOps values overlay

Copy `examples/values.yaml` to an environment-specific file such as `environments/prod/pgbk-values.yaml` and replace all examples. Do not put passwords, access keys, or private keys in this file. For the first deployment, check that the references match real Secret names and the published image.

```yaml
installationId: prod-backups
coordinationNamespace: backups
rbac:
  create: false
coordination:
  createLeases: false
images:
  "17": registry.example/pgbk-worker@sha256:REPLACE_WITH_REAL_DIGEST
storage:
  archive:
    bucket: prod-postgres-backups
    prefix: cluster-a
    endpoint: https://storage.yandexcloud.net
    region: ru-central1
    addressingStyle: path
    credentialsSecretRef: backup-s3
    tls:
      verify: true
targets:
  primary:
    sourceId: prod-primary
    concurrencyKey: prod-primary
    host: postgres.database.svc.cluster.local
    port: 5432
    serverMajor: 17
    maintenanceDatabase: postgres
    credentialsSecretRef: backup-postgres
    tls:
      mode: verify-full
      caSecretRef: postgres-ca
profiles:
  prod-logical:
    serviceAccounts:
      create: false
      backup: pgbk-backup-worker
      repository: pgbk-repository-worker
    target: primary
    kind: logical
    storage: archive
    logical:
      databases: [application]
      globals: disabled
    schedule:
      enabled: false
    execution:
      resources:
        requests: {cpu: "500m", memory: 512Mi}
        limits: {cpu: "2", memory: 2Gi}
    scratch:
      type: pvc
      size: 20Gi
      storageClassName: standard
    retention:
      maxAgeDays: 30
      minSuccessfulBackups: 3
```

This example leaves backup scheduling disabled; the retention CronJob has its own schedule. Check its settings against `examples/values.yaml` and the chart schema. For production, size scratch and select a storage class based on measured workload. Enable backup scheduling only after a successful manual trial. Add digest-pinned `images` for every PostgreSQL major you use.

If operation namespaces are separate, set them explicitly in restore/verification profiles. Create the required ServiceAccounts, Secrets, PVCs, and permissions there. Check name limits: the Helm release and profile name together must fit the CronJob name limit.

## 5. Add an Argo CD Application

The chart and values can live in the same Git repository. For example, keep the Application in an Argo CD bootstrap repository or apply it through an administrator:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pgbk-prod
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://git.example.com/platform/pgbk.git
    targetRevision: main
    path: charts/pgbk
    helm:
      releaseName: pgbk
      valueFiles:
        - ../../environments/prod/pgbk-values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: backups
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
```

Replace the repository URL, revision, values path, and namespace. The values path must resolve from the chart directory within the same repository; if needed, allow Helm values files from the parent directory (`../../`) in Argo CD settings, move the file under the chart path, or use a multi-source Application. For separate repositories, a multi-source Application can load the chart from the source repository and values from a configuration repository using `ref` and `$values/...`.

Enabling `prune: true` means Argo CD deletes chart resources removed from Git. Deleting Kubernetes CronJobs/ConfigMaps does not delete artifacts already uploaded to S3; uninstall also leaves existing Secrets/PVCs/Leases. Before enabling automated sync, inspect the diff and ensure this Application does not own pre-created Secrets, ServiceAccounts, RBAC, or Leases.

Do not set `CreateNamespace=true` if namespace creation is owned by a separate platform/bootstrap process. The destination namespace must exist when the Application syncs.

## 6. Check rendering before sync

From a repository checkout, validate the values and render the chart locally:

```sh
helm lint charts/pgbk -f environments/prod/pgbk-values.yaml
helm template pgbk charts/pgbk --namespace backups -f environments/prod/pgbk-values.yaml
```

Confirm the image has a real digest, namespace/Secret/ServiceAccount names are correct, the backup CronJob starts suspended, and scratch capacity is available. Review the Argo CD diff before the first sync. Check permissions for pre-created worker accounts and verify worker nodes can pull the private image.

## 7. Sync and run a smoke backup

After sync, inspect the CronJob and ConfigMap:

```sh
kubectl -n backups get cronjobs,configmaps
kubectl -n backups describe cronjob pgbk-prod-logical-backup
```

The exact CronJob name combines the Helm release, profile, and operation; for backup it is `<release>-<profile>-backup`. Create a trial Job from the CronJob:

```sh
kubectl -n backups create job pgbk-smoke-$(date +%s) \
  --from=cronjob/pgbk-prod-logical-backup
kubectl -n backups get jobs,pods -w
kubectl -n backups logs job/<job-name>
```

Check Job completion, S3 write/read access, SHA-256 verification, and a completed catalog entry. Then download and restore into an isolated test destination using the [operations guide](operations.md#download-and-standalone-restore). Only after that, set `schedule.enabled: true` in Git and wait for Argo CD sync. A successful upload alone does not prove the backup is restorable.

## 8. Updates and GitOps operations

- Release workers with a new immutable tag, publish them, then update the digest in Git in a reviewed commit.
- Make schedule and configuration changes through Git and review the Argo diff before applying them.
- Do not remove old images while running Jobs or planned recovery may still depend on them.
- Rotate credentials through the secret manager. If Secret names change, create the new Secrets before changing references in Git.
- Monitor CronJob/Job success, age of the last successful backup, integrity/retention errors, and restore verification. The [operations guide](operations.md) covers practical commands and restore requirements.
- Keep S3 data lifecycle independent of Argo CD. The chart does not manage bucket lifecycle or delete backups on uninstall.

## Limitations

This is a deployment guide for the current chart, not certification of a particular Kubernetes/S3/PostgreSQL environment. Compatibility and unverified conditions are listed in the [compatibility matrix](compatibility.md). The current version uses WAL-G for raw S3 artifact transfer; it does not use the native `backup-push` format and does not include incremental backups or PITR.
