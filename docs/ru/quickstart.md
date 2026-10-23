# Быстрый старт

Для GitOps-развертывания через Argo CD см. подробную [инструкцию](argocd-deployment.md).

Версия: 0.2.0. Основная конфигурация не создаёт namespace, ServiceAccount, RBAC и Lease. Администратор заранее подготавливает аккаунты с get/update конкретных координационных Lease и необходимыми правами чтения Job/PVC; см. эксплуатацию. Необязательная подготовка через examples/access-bootstrap.yaml включается отдельно и не входит в обычное развертывание. Сборка для разработки; сертификация выпуска не завершена. См. [совместимость и доказательства](compatibility.md), [конфигурацию](configuration.md) и [эксплуатацию](operations.md). [Спецификация](specification.md) остаётся контрактом приёмки версии 1.

Установите CLI в virtual environment Python 3.11+. Проверенный renderer — Helm 3.19.0. Kubernetes должен поддерживать зоны CronJob (заявленный минимум 1.27; сертификация минимальной версии и Jobs на amd64 ещё предстоит). Начальная архитектура исполнителя — Linux amd64.

```sh
uv sync --frozen --no-dev
cp examples/values.yaml values.local.yaml
uv run --frozen pgbk --config values.local.yaml config validate
```

Замените все примеры endpoint, баз, бакетов, storage class и нулевой image digest. Чарт не создаёт источник PostgreSQL, бакет или учётные данные. Администратор заранее предоставляет доступ и существующие Secrets в namespace релиза. PostgreSQL Secret содержит `username` и `password` по умолчанию; targets могут переопределить каждый ключ через `credentialsSecretKeys`; S3 Secret — `accessKeyId`, `secretAccessKey` и необязательный `sessionToken`. CA Secret содержит `ca.crt`; клиентский TLS Secret — `tls.crt` и `tls.key`. Предоставляйте файлы через обычное управление секретами; не помещайте пароли в values или аргументы команд.

Соберите отдельный worker для каждой настроенной major-версии PostgreSQL из неизменяемых баз в `docs/compatibility.json`. Опубликуйте в своём registry обычным процессом выпуска, запишите полученный registry digest и задайте его в `images.<major>`. Инструменты устанавливаются при сборке. Репозиторий автоматически не публикует образы.

```sh
python3 scripts/build_images.py --major 17 --repository registry.example/pgbk-worker
helm lint charts/pgbk -f values.local.yaml
helm upgrade --install pgbk charts/pgbk --namespace backups -f values.local.yaml
pgbk --config values.local.yaml --namespace backups backup run --profile example-logical
kubectl -n backups create job example-manual --from=cronjob/pgbk-example-logical-backup
kubectl -n backups logs job/example-manual
```

Согласуйте `coordinationNamespace` с используемым существующим namespace. Псевдонимы и установки, работающие с одним источником/репозиторием, должны использовать общий namespace координации и ключи. Независимые писатели из разных кластеров не поддерживаются. Ручные и плановые Jobs используют один неизменяемый снимок конфигурации и процесс. Приостановленное расписание резервирования допускает ручные Jobs.

Для прямого доступа к S3/PostgreSQL локальный CLI читает проекции Secrets в `PGBK_SECRET_ROOT/<SecretName>/<key>`. Задайте `PGBK_SECRET_ROOT` как приватный каталог с файлами от своего secret manager. `backup run` и `verify` создают Jobs и не требуют локального экспорта этих учётных данных. Скачивание через Job описано в [эксплуатации](operations.md) и оставляет доступ к S3 внутри Kubernetes. Доступ к Kubernetes берётся из выбранного context/KUBECONFIG либо из service account исполнителя.

```sh
pgbk --config values.local.yaml --namespace backups --scratch /tmp/pgbk preflight --profile example-logical
pgbk --config values.local.yaml --namespace backups --output json backup list --profile example-logical
pgbk --config values.local.yaml --namespace backups --output json backup describe --profile example-logical --run-id RUN_ID
```

Preflight возвращает `unverified`, если проверки чтения прошли, но сам экспорт или replication capacity ещё не подтверждены. Пригодная копия требует успешных инструментов, проверки архива, полных uploads, проверки удалённых SHA-256 и финального маркера завершения. Успешная загрузка сама по себе не подтверждает восстановление.

Для Managed PostgreSQL и PostgreSQL в Kubernetes начните с [примера двух источников и режимов](logical-modes.md).
