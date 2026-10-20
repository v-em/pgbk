# Logical backup modes — pgbk 0.2.0

pgbk — PostgreSQL backups for Kubernetes. PostgreSQL and S3 adapters remain provider-independent. The [two-source Yandex example](../../examples/yandex/two-sources.yaml) contains separate Managed PostgreSQL and Kubernetes PostgreSQL targets. Both use logical backup. Replace synthetic host/database names, Secret references, image digest and StorageClass before deployment. The managed endpoint must accept pg_dump connections; the Kubernetes Service must reach the intended database. Existing Namespace, ServiceAccount, RBAC and Lease prerequisites are unchanged.

The managed profile uses direct S3 and the Kubernetes profile uses a PVC with parallel dump. [Managed PVC override](../../examples/yandex/managed-pvc-override.yaml) and [Kubernetes direct override](../../examples/yandex/kubernetes-direct-override.yaml) change the mode of the same profile, preserving catalog identity. After merging overlays, validate the merged configuration before installation. These examples declare PostgreSQL 17; use an image matching each actual server major.

## Direct stream and staged parallel dump

```yaml
logical:
  databases: [application]
  format: custom
  jobs: 1
  compression: {method: gzip, level: 6}
  transfer: {mode: direct, partSizeMiB: 16}
scratch: {type: emptyDir, size: 256Mi}
```

```yaml
logical:
  databases: [application]
  format: directory
  jobs: 4
  compression: {method: zstd, level: 3}
  transfer: {mode: staged}
scratch:
  type: pvc
  size: 100Gi
  storageClassName: REPLACE_WITH_STORAGE_CLASS
```

`direct` accepts only `custom` and `jobs: 1`. pg_dump stdout is uploaded sequentially through the S3 SDK multipart API. No complete dump is written to scratch, including optional globals. A small emptyDir is required for password files and bounded tool metadata. Memory includes multipart buffers (several times partSizeMiB), PostgreSQL clients and the runtime; do not select a part size close to the container memory limit. partSizeMiB defaults to 16, accepts 5–512, and at most 10000 parts are emitted. Larger dumps need a larger part size. This is one dump process and one sequential uploader, not parallel S3 upload.

Each part can be retried with the same bytes. The complete operation follows successful producer exit; pg_dump failure, deadline, cancellation or exhausted upload retries abort the upload when possible. A new run restarts the dump: a live PostgreSQL snapshot cannot be resumed from a failed pipe. Configure external lifecycle cleanup for abandoned multipart uploads after node loss. Completion markers are published only after remote archive TOC/selection validation and full remote SHA-256 verification. Direct streaming uses the SDK; staged files continue to use WAL-G raw transfer. There is no automatic fallback to staging.

`staged` writes custom or directory output to scratch first. `jobs` defaults to 1 and accepts 1–64; values above 1 require directory format. pg_dump opens jobs + 1 connections, in addition to preflight/selection sessions. Directory mode dumps tables in parallel, then packages the compressed member files in an uncompressed tar. Budget scratch for both the directory and tar (approximately twice the compressed archive, plus headroom). PVC or an allowlisted existing claim can be used; emptyDir remains available for smaller staged backups. The PVC example uses a Pod-owned generic ephemeral volume; it is not the source database PVC.

## Compression and artifact compatibility

The method/level are archive compression settings, distinct from transfer mode and dump jobs. Defaults: gzip level 6. PostgreSQL 14–15 allow none or gzip; PostgreSQL 16–18 additionally allow lz4/zstd when compiled into the client. gzip accepts levels 1–9; zstd 1–22; omit level for none/lz4. Omitted gzip/zstd level uses the native default. An unavailable codec fails the run, without silently changing algorithms. Outer tar bundling adds no second compression layer.

Custom artifacts remain `postgres-custom`. Directory artifacts are `postgres-directory-tar`: a tar of the native flat directory dump, including toc.dat. pgbk 0.2 understands both within manifest format v1; older readers reject the new format. Restore/drill safely extract directory bundles into isolated scratch and run pg_restore. Download includes both native restore instructions. For trusted archives, extraction followed by pg_restore on the directory works without Kubernetes, S3 or an application service. Matching PostgreSQL tools and extension dependencies are still required. A downloaded directory archive needs space for the tar and extracted directory during restoration.

Physical-full is unchanged and uses staged storage; these new options apply only to logical profiles. Increments, PITR and UI are not implemented in this version.

## Validate examples

```sh
uv sync --frozen
uv run --frozen pgbk --config examples/yandex/two-sources.yaml config validate
helm lint charts/pgbk -f examples/yandex/two-sources.yaml
helm template pgbk charts/pgbk -n backups -f examples/yandex/two-sources.yaml
```

```sh
helm template pgbk charts/pgbk -n backups \
  -f examples/yandex/two-sources.yaml \
  -f examples/yandex/managed-pvc-override.yaml
```

## Development with uv

Use uv 0.9.5 (pinned in CI and Docker), or a compatible newer uv. Install uv from its official distribution. `uv.lock` is authoritative; runtime constraints preserve the earlier pinned dependency set. `uv sync --frozen` installs runtime plus unit-test dependencies; add `--group integration` for Moto. Use the same group on subsequent `uv run` commands while the emulator is running, so synchronization does not remove its dependencies. `uv build` builds CLI distributions; `make package` also packages Helm. Worker builds use uv sync --frozen --no-dev and do not install packages at runtime. requirements.lock is retained as the Linux runtime inventory for existing evidence/SBOM scripts.
