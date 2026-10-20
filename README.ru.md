# pgbk — PostgreSQL backups for Kubernetes

Реализация разработки 0.2.0: CLI/worker на Python, Helm chart и образы Linux amd64 соответствующих версий PostgreSQL 14–18. Logical custom/directory-архивы и самодостаточные физические копии сохраняются в неизменяемый S3-каталог; прямой logical-поток использует S3 SDK, staged-файлы — raw-передачу WAL-G с проверкой SHA-256, обновляемой координацией, ограниченным retention, скачиванием, изолированным восстановлением и restore drill.

Обычное развертывание использует существующие namespace, ServiceAccount, права RBAC, Lease, источники, Secret и bucket. Оно не создаёт workload источника и не меняет его настройки. Начните с [быстрого старта](docs/ru/quickstart.md), [конфигурации](docs/ru/configuration.md) и [эксплуатации/RBAC](docs/ru/operations.md). [Английская документация](README.md) поддерживается синхронно.

Отчёты локальной реальной матрицы PostgreSQL 14–18 и Kubernetes API/RBAC находятся в [tests/reports](tests/reports). [Совместимость и условия выпуска](docs/ru/compatibility.md) отделяют выполненные локальные части от полной сертификации. Живые тесты Yandex оператор проводит отдельно; проверки Kubernetes/PVC amd64 и TLS/сбоев остаются открытыми. Сборка не обозначена production-ready.

- [Спецификация v1.1](docs/ru/specification.md) и [план приёмки](docs/ru/test-plan.md).
- [Архитектурное решение](docs/ru/architecture.md), [версии образов](docs/compatibility.json), [реестр приёмки](tests/reports/acceptance.json).
- [Пример values](examples/values.yaml), [существующие аккаунты](examples/existing-service-accounts.yaml), [права deployer](examples/deployer-rbac.yaml).
- [Исходный код CLI](src/pgbk), [Helm chart](charts/pgbk), [сборка worker](images/Dockerfile), [CI](.github/workflows/test.yaml).

Версия 1 не включает инкрементальные копии, непрерывное архивирование WAL, PITR, управление HA, operator и подготовку источника. Выборочные logical-архивы фиксируют точный scope и зависимости восстановления. Артефакты используют формат проекта v1, а не репозиторий WAL-G backup-push; применяйте pg_restore или проверенную физическую распаковку, не backup-fetch.

Исходный код лицензирован Apache-2.0; см. [LICENSE](LICENSE), [NOTICE](NOTICE) и [уведомления о сторонних компонентах](THIRD-PARTY-NOTICES.txt). Пакеты/SBOM/checksums создаются в dist через Makefile и release-скрипты.

Новое в 0.2: [direct S3 / параллельный staged backup, сжатие и два источника](docs/ru/logical-modes.md); [план будущего UI](docs/ru/roadmap.md). Этот каталог — публичный репозиторий. Конфигурации окружений и внутренние материалы находятся в отдельном соседнем репозитории pgbk-private; credentials хранятся вне Git. Установка: uv sync --frozen; тесты: uv run --frozen pytest; сборка: uv build.
