# Configuration reference

Version: 0.2.0. [Quickstart](quickstart.md). The strict schema is [values.schema.json](../../charts/pgbk/values.schema.json); unknown keys, duplicate YAML keys and embedded credentials are rejected. IDs and Secret/namespace references use lowercase letters, digits and hyphens, at most 48 characters. Release/profile combinations must fit the 52-character CronJob name limit. [Full example](../../examples/values.yaml).

| Key | Meaning |
|---|---|
| `installationId` | Required, stable catalog identity. |
| `coordinationNamespace` | Existing namespace; defaults to operation namespace. Set explicitly when operations span namespaces. |
| `rbac.create / coordination.createLeases` | Both default false: use pre-existing permissions and named Leases; opt-in administrator bootstrap only. |
| `images.<14..18>` | Matching-major Linux amd64 worker reference with sha256 digest. Required by Helm. |
| `targets.<id>.sourceId / concurrencyKey` | Stable source identity / shared serialization key. Aliases must share the key. |
| `targets.<id>.host / port` | TCP endpoint; port defaults to 5432. |
| `targets.<id>.serverMajor / maintenanceDatabase` | 14–18; maintenance database defaults to postgres. |
| `targets.<id>.credentialsSecretRef` | Existing Secret with username/password. |
| `targets.<id>.tls.mode` | verify-full default; disable/allow/prefer/require/verify-ca also explicit options. |
| `targets.<id>.tls.caSecretRef / clientSecretRef` | Optional CA and client certificate Secret projections. |
| `targets.<id>.physicalEndpoint / expectedSystemIdentifier` | For physical-full: direct-primary and an acknowledged numeric system identifier are required. |
| `storage.<id>.endpoint / region / bucket / prefix` | Existing S3 endpoint and bucket; prefix is nonempty slash-separated ASCII segments. |
| `storage.<id>.addressingStyle` | path default; virtual also supported. |
| `storage.<id>.credentialsSecretRef` | Existing Secret with accessKeyId/secretAccessKey and optional sessionToken. |
| `storage.<id>.tls.verify / caSecretRef` | verify must be true; optional private CA Secret. HTTP is allowed only for an explicitly configured test endpoint. |
| `profiles.<id>.target / storage / kind` | Explicit references and logical or physical-full. |
| `profiles.<id>.download.allowedDestinationClaims` | Explicit allowlist for download Job claims; defaults empty. Worker rechecks ownership/attachments after mount. |
| `profiles.<id>.logical.databases / globals` | Unique literal database names; globals defaults to disabled, enabled exports SQL without role passwords. |
| `profiles.<id>.logical.tableSelection` | See the selection rules below; absent database rules produce ordinary full custom archives. |
| `profiles.<id>.schedule` | enabled defaults to false. cron: numeric five fields; timeZone: UTC default; startingDeadlineSeconds: 3600 default. |
| `profiles.<id>.execution.resources` | Required CPU/memory requests and limits, all positive, requests <= limits. |
| `profiles.<id>.execution.activeDeadlineSeconds / ttlSecondsAfterFinished` | 21600 / 86400 defaults. TTL removes finished Jobs/Pods, not remote backups. |
| `profiles.<id>.execution.nodeSelector` | Optional node labels; linux/amd64 is mandatory. |
| `profiles.<id>.scratch` | Required type and positive size; details below. |
| `profiles.<id>.retention` | maxAgeDays: 30, minSuccessfulBackups: 3, incompleteGraceDays: 7. Positive integers. schedule defaults to enabled, 0 4 * * *, UTC. |
| `profiles.<id>.verification` | enabled defaults to false; enabled requires schedule, namespace, image digest, resources, scratch, timeoutSeconds and both credential references. |
| `profiles.<id>.verification.credentialsSecretRef / clientCredentialsSecretRef` | Disposable server initdb credentials / restored-server client. Logical drill usernames must match. Physical client role must already exist in the backup. |
| `profiles.<id>.verification.assertions / failureDebugTtlSeconds` | Optional read-only SELECTs returning one true boolean; Job debug TTL defaults to 86400. Set scratch.retainFailed to retain failure files until Pod removal. |
| `profiles.<id>.restore` | Opt-in Jobs: enabled, namespace, resources, scratch, timeoutSeconds, credentialsSecretRefs and allowedDestinationClaims. Defaults disabled. |
| `profiles.<id>.serviceAccounts` | create defaults false. With create: false configure backup/repository and verify/restore for enabled operations using existing names in their operation namespaces. |
| `networkPolicy.enabled / egress` | Optional deny-ingress/explicit-egress policy. Defaults disabled; allow DNS, Kubernetes API, S3 and necessary destination endpoints. |

## Table inclusion and exclusion

`logical.tableSelection.queryTimeoutSeconds` defaults to 30; `maxQueryRows` to 10000 (maximum 100000). `byDatabase` keys must name configured databases. Each rule has `mode: all` (default) or `selected`, optional `include` and `exclude` with `tables: [{schema, table}]` and/or `query`. Manual and query results are unioned and deduplicated; exclusion wins. `all` forbids includes. `selected` requires an include and a nonempty effective set. Missing manual/query-resolved objects fail; an empty exclusion is valid.

```yaml
mode: selected
largeObjects: none
include:
  tables:
    - schema: public
      table: invoices
  query: |
    SELECT schemaname::text AS schema_name,
           tablename::text AS table_name
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'reporting'
exclude:
  tables:
    - schema: reporting
      table: temporary_totals
```

Names are literal, including dots, quotes, Unicode and wildcard characters. Only ordinary/partitioned user tables are selectable; views, foreign/materialized tables, system objects and extension-owned relations fail. Selecting a parent expands descendants, including inheritance; excluding a child removes that child's definition/data. Choosing a leaf does not add its parent or siblings.

A query must be exactly one SELECT returning exactly `schema_name` and `table_name`, both PostgreSQL text, without NULL/empty/newline names. Writing CTEs, SELECT INTO, locking clauses and multi-statements are rejected using the PostgreSQL parser. Execution uses read-only repeatable-read, a server cursor, timeout and a raw-row limit before deduplication. Queries are trusted administrator configuration: read-only SQL does not sandbox side-effecting functions. Query text is not logged; manifests contain digests/provenance. Each database has its own exported snapshot held through pg_dump. Access-share locks freeze selected/excluded definitions, and archive OIDs are checked against the frozen set; this is not a cluster-wide snapshot.

Any configured rule is labeled `filtered-database`. Table-owned objects follow pg_dump. Outside schemas, types, functions, extensions, partition parents and foreign-key targets may be required for restore. Known schemas, direct column types, FK targets and parents are recorded; recursive dependency discovery is explicitly incomplete. Selected mode defaults `largeObjects: none`; explicit `all` includes ALL database large objects, beyond the table set. All mode preserves pg_dump large-object behavior; excluding a table does not infer large-object ownership.

## Scratch and isolated operations

`scratch.type: emptyDir` uses `size` as sizeLimit. `pvc` creates a generic ephemeral claim with the declared size and optional storageClassName; it belongs to the Pod and is removed when the Pod is removed, subject to storage-controller behavior. `existingClaim` requires claimName and allowedClaims; it must carry installation/purpose labels and have no other active Pod attachment. Existing claims are never deleted. `retainFailed` defaults false and is rejected for existingClaim because bounded debug retention needs a Pod-owned volume.

Verification/restore namespaces must already exist. Their images must include pgbk and compatible PostgreSQL server/client/extension binaries, run as UID/GID 999 and support a read-only root filesystem; a plain postgres image without pgbk cannot execute the template. Replicate the required S3/CA/destination Secrets into those namespaces through your secret-management process. The chart never copies Secrets or mounts source data volumes. Restore Jobs use the normal matching-major worker image; verification accepts a custom compatible image digest.

```yaml
# Overlay on a full values file. Accounts must already exist in each operation namespace.
profiles:
  example-logical:
    serviceAccounts:
      create: false
      backup: existing-backup-worker
      repository: existing-repository-worker
```

See [operations](operations.md) for permissions, restore destinations and source prerequisites. Schema validation checks shape and references; endpoint privileges, storage capacity and real restore compatibility require preflight and acceptance tests.

## Logical mode options (0.2.0)

See [logical modes](logical-modes.md) for `logical.transfer.mode`, `logical.transfer.partSizeMiB`, `logical.format`, `logical.jobs` and `logical.compression.method/level`, defaults, compatibility and the two-source example.
