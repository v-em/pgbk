# Test evidence

Reports dated 2026-10-11 describe pgbk 0.1. Reports named `modes-<major>-<mode>-<compression>.json` describe 0.2 and identify the tested image. The real-tool fixture uses local PostgreSQL, a synthetic S3 emulator and a stubbed Kubernetes coordinator. It exercises direct multipart, staged archives, native/product offline restore, filtered selection and physical regression. Helm rendering validates PVC/emptyDir templates; these tests do not certify real CSI/PVC lifecycle or Yandex services. Detailed logs live outside the public repository.
