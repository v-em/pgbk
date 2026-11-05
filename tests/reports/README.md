# Test evidence

Reports dated 2026-10-11 describe pgbk 0.1. Reports named `modes-<major>-<mode>-<compression>.json` describe 0.2 and identify the tested image. The real-tool fixture uses local PostgreSQL, a synthetic S3 emulator and a stubbed Kubernetes coordinator. It exercises direct multipart, staged archives, native/product offline restore, filtered selection and physical regression. Helm rendering validates PVC/emptyDir templates; these tests do not certify real CSI/PVC lifecycle or Yandex services. Detailed logs live outside the public repository.


Specification 1.3 validation is separate from the historical reports above:

- `unit-spec13.json` / `unit-spec13.xml`: 136 passing unit tests for the updated implementation.
- `spec13-minikube18.json`: 14 passing real PostgreSQL 18.6/S3/Kubernetes subcases, matching source SHA-256 and immutable local-registry image digest. Linux amd64 workers run under emulation on an arm64 minikube node (Kubernetes 1.34.1); this is not native amd64 certification.
- `kubernetes-spec13.json`: 6 real API/Lease/RBAC/immutable-template regression subcases.
- `spec13-offline18.json`: logical and physical recovery in a fresh network-disabled container after the original minikube/source were stopped. Data, sequences, partitions, constraints and large objects are checked without S3/Kubernetes credentials.

The 0.3.0 reports above do not certify Yandex Cloud, the new full 14–18 matrix, schema selection, execution-log extensions or automatic verification. Failed intermediate fixture logs and detailed Job logs remain in the private validation directory. `acceptance.json` links subcases without promoting incomplete full acceptance procedures to passed.

Version 0.3.1 adds separate evidence for schema selection and extension-table exclusions.

- `schema-unit.json` / `schema-unit.xml` record 155 unit tests, including schema names, partitions, extension exclusions, archive-data rejection and existing-schema restore handling.
- `schema-selection-minikube18.json` records actual backup, preflight, download and restore Jobs. The fixture uses PostgreSQL 18.1 with pg_cron 1.6 and tools from the PostgreSQL 18.6 worker. Original jobs and history are populated, then checked for absence after restoration. Application data, schema objects, exact identifiers, large objects, multi-database failures and restore RBAC are checked.
- `spec13-minikube18-031.json` records the 14 existing verification/retention subcases on the 0.3.1 image and chart. `kubernetes-031.json` records six API checks.
- `spec13-offline18-031.json` records independent logical and physical native restores after stopping minikube, using the final image with networking disabled.

The minikube reports identify source hashes and chart hashes. The node is arm64 and workers use amd64 emulation. Cloud and full PostgreSQL 14–18 acceptance remain separate gates.
