# Compatibility, upgrade and evidence

Version: 0.2.0. Date: 2026-10-20. This is a development distribution; the full version-1 release acceptance is incomplete. The user performs live Yandex testing separately. Exact pins are in [compatibility.json](../compatibility.json); reports are retained in [tests/reports](../../tests/reports). [Acceptance ledger](../../tests/reports/acceptance.json) maps every requirement and test to implementation, subcase evidence and outstanding certification. A passing subset is not a passed full acceptance case.

| Component | Tested selection |
|---|---|
| PostgreSQL | 14.24, 15.19, 16.15, 17.11, 18.6; matching-major Linux amd64 workers |
| WAL-G | 3.0.9, release commit 3e49318, pinned binary checksum in Dockerfile |
| Runtime | Debian bookworm Python 3.11.2 workers; Python 3.14 host unit tests; locked Python dependencies |
| Helm | 3.19.0 lint/render/install |
| Kubernetes | kind 0.33.0, API v1.37.0 on arm64; amd64 worker scheduling/PVC lifecycle not certified |
| Kubernetes minimum | 1.27 declared for CronJob timeZone; minimum-version certification not-run |
| S3 | Moto 5.1.11 emulator; Yandex/AWS live certification not-run |
| Sources | Restricted fixture role; live managed and pinned Bitnami integration not-run |

Real integration reports for majors 14–18 cover two literal-named databases, a restricted dump role, 10000-row tables, wildcard/quote/Unicode names, partitions, an extra tablespace, RLS denial, raw-row overflow, empty selections, blocking-query cancellation, exact snapshot selection, native and product logical restore, source-offline physical restore, and both automated offline drills. Kubernetes reports prove real Lease conflict/release, immutable config, manual Job creation/cancellation, actual ServiceAccount authorization boundaries and installation with pre-existing identities/Leases and no RBAC changes. The arm64 API fixture does not certify amd64 Job execution.

Local PostgreSQL 17.11 capacity exercises passed for 1 and 20 GiB, for logical and physical backup followed by restore with the source stopped. Reports include tool versions, timings, scratch and RSS peaks. The 20-GiB exercise used a 2-CPU/4-GiB container; observed scratch peaked at 42.04 GiB and summed process RSS at 0.97 GiB. The fixture uses uncompressed PostgreSQL storage with highly compressible repeated text. Docker Desktop emulates amd64 on arm64 macOS; S3 Moto runs outside the measured container and Kubernetes is stubbed. These are local capacity/correctness observations, not production throughput guarantees. The first large-object attempt hit a macOS/Moto HTTP send-buffer limit and published no complete physical marker; the successful repeat uses the chunked test server. NetworkPolicy/TLS negative fixtures, real PVC failure/reclamation, node fencing/partition injection, promotion, concurrent DDL/writes, globals/large-object/dependency completeness and comprehensive secret-rotation/security injection remain release acceptance work. Code implements safe failures, but missing environment/test evidence is not inferred as passed.

[1-GiB report](../../tests/reports/benchmark-1.json), [20-GiB report](../../tests/reports/benchmark-20.json).

## Reproduce development checks

Run Moto in a separate terminal with synthetic credentials only, then initialize the disposable pgbk-test bucket. Do not point fixture scripts at production services: the harness provisions synthetic databases and uses its own integration prefixes.

```sh
uv sync --frozen --group integration
uv run --frozen --group integration python -m pytest
uv run --frozen --group integration python scripts/check_docs.py
helm lint charts/pgbk -f examples/values.yaml
uv run --frozen --group integration moto_server -H 0.0.0.0 -p 19000
```

```sh
uv run --frozen --group integration python tests/feasibility/bucket.py
python3 scripts/build_images.py
python3 scripts/run_integration.py
python3 scripts/evidence.py
```

For Linux Docker, add host.docker.internal:host-gateway; the integration runner handles this automatically. Kubernetes smoke uses a separately provisioned kind cluster and cleans its disposable namespaces. CI runs locked unit/chart checks and the same 14–18 real-tool matrix; it publishes no images and performs no live-provider actions.

## Upgrade and release artifacts

Before upgrade, retain a verified downloadable backup and matching-major restore environment. Build and publish images through your normal registry process, record their actual registry digests and update values/images. Do not substitute a Docker image config ID for a registry manifest digest. New immutable ConfigMaps apply only to new Jobs; running Jobs retain the old reference. Secrets remain externally managed projections and can change during a run, so perform rotation between runs. Kept Leases must not be reset on upgrade/rollback; shared pre-created Leases require coordination.createLeases=false. Stable installationId/sourceId/profileId/prefix preserve catalog identity. Changing them selects a different catalog, not a migration.

CLI wheel/source archive, Helm package, image receipts, checksums, CycloneDX SBOMs and third-party notices are produced under dist. Original code is Apache-2.0. PostgreSQL/WAL-G/Python/OS and Python dependencies retain their separate licenses; Debian copyright and package license texts are retained/extracted. Go license enrichment and unknown SBOM entries require final audit before distribution certification; a scanner result is not legal clearance. Builds pin PostgreSQL bases/WAL-G/Python dependencies, but apt repository state and build timestamps are not fixed for bit-identical rebuilds. Promote a tested image by immutable registry digest rather than rebuilding it during installation.

## Operator Yandex acceptance

Use pre-provisioned isolated source/destination/bucket prefixes and externally supplied credentials. Follow T-001–T-004, T-024, T-028, T-030–T-032 and T-035 in the [test plan](test-plan.md): backup, remote SHA verification, download, restore while source is unavailable, drills, denied access/TLS checks and scoped retention. Keep native tool/image/chart versions, run IDs, exact provider endpoint, expected data and sanitized duration/resource reports. Clean only your disposable test prefixes/claims after confirming no active writers. This repository does not execute live Yandex tests or certify them from emulator results.

Reports dated 2026-10-11 are the historical 0.1 baseline. 0.2 mode checks are recorded separately in `tests/reports/modes-*.json`. The old license/SBOM artifacts are not certification of the new build.
