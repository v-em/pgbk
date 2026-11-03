# Test evidence

Reports dated 2026-10-11 describe pgbk 0.1. Reports named `modes-<major>-<mode>-<compression>.json` describe 0.2 and identify the tested image. The real-tool fixture uses local PostgreSQL, a synthetic S3 emulator and a stubbed Kubernetes coordinator. It exercises direct multipart, staged archives, native/product offline restore, filtered selection and physical regression. Helm rendering validates PVC/emptyDir templates; these tests do not certify real CSI/PVC lifecycle or Yandex services. Detailed logs live outside the public repository.


Specification 1.3 validation is separate from the historical reports above:

- `unit-spec13.json` / `unit-spec13.xml`: 136 passing unit tests for the updated implementation.
- `spec13-minikube18.json`: 14 passing real PostgreSQL 18.6/S3/Kubernetes subcases, matching source SHA-256 and immutable local-registry image digest. Linux amd64 workers run under emulation on an arm64 minikube node (Kubernetes 1.34.1); this is not native amd64 certification.
- `kubernetes-spec13.json`: 6 real API/Lease/RBAC/immutable-template regression subcases.
- `spec13-offline18.json`: logical and physical recovery in a fresh network-disabled container after the original minikube/source were stopped. Data, sequences, partitions, constraints and large objects are checked without S3/Kubernetes credentials.

Yandex Cloud, the new full 14–18 matrix, schema/log extensions and automatic verification are not certified by these reports. Failed intermediate fixture logs and detailed Job logs remain in the private validation directory. `acceptance.json` links subcases without promoting incomplete full acceptance procedures to passed.
