# Эксплуатация, требования и восстановление

Версия: 0.3.3. Начните с [быстрого старта](quickstart.md) и [конфигурации](configuration.md). Подтверждения и непроверенные условия приёмки — в [совместимости](compatibility.md).

## Требования к существующим источникам

Продукт обращается к PostgreSQL по TCP, не меняя настройки, права, deployment или PVC источника. Администратор заранее создаёт роль входа с CONNECT для каждой базы, USAGE для нужных схем, SELECT для выгружаемых таблиц/последовательностей и соответствующим доступом к большим объектам. SQL-выборка также требует чтения управляющих таблиц. Ошибки RLS/доступа останавливают запуск; продукт не отключает RLS и не исправляет права. Необязательные метаданные версии/кодировки/расширений/system identifier могут остаться неизвестными для ограниченной роли. Globals по умолчанию выключены; включение требует прав pg_dumpall и не превращает logical-профиль нескольких баз в физическую копию экземпляра.

Для обычных Kubernetes/Bitnami-источников используйте существующий сетевой Service и настроенные учётные данные/TLS; не подключайте тома данных и не выполняйте exec в Pod источника. Physical-full требует прямой адрес нужного primary, существующую аутентификацию репликации, достаточное число WAL sender для basebackup и потока WAL, читаемые файлы источника и expectedSystemIdentifier. SQL pooler или managed logical endpoint не является physical endpoint. Настройки WAL/репликации уже должны разрешать pg_basebackup; archive_mode включать не требуется. Постоянный слот не создаётся. Физическая копия содержит все базы, глобальные объекты, WAL и tablespace; внешняя конфигурация и бинарные файлы расширений — отдельные условия восстановления.

Managed-сервисы используются через logical с учётом прав провайдера, ограниченных объектов и мощности экспорта. Сертификация конкретного и живого managed-провайдера пока не выполнена. Автоматического перехода к physical или привилегированной роли нет. Используйте проверенный TLS с CA/именем, соответствующим endpoint; ослабление TLS требует явного изменения конфигурации. Учётные данные/CA/клиентские сертификаты проецируются из существующих Secret и не помещаются в values, argv или логи. Физические архивы могут содержать чувствительную конфигурацию источника: ограничьте доступ к bucket и скачанным данным.

## RBAC установки и существующие аккаунты

Chart не создаёт Namespace. Подготовьте namespace операций/координации заранее либо явно используйте Helm --create-namespace для namespace release. Для существующих ServiceAccount задайте `profiles.<id>.serviceAccounts.create: false` и имена аккаунтов операций; [пример overlay](../../examples/existing-service-accounts.yaml). Аккаунты находятся в namespace соответствующих Job. Отдельные аккаунты отделяют координацию источника от работы только с репозиторием; verification/restore могут использовать аккаунты в своих namespace.

Основные values отключают создание RBAC, ServiceAccount и Lease. Helm нужны namespaced get/list/watch/create/update/patch/delete для ConfigMap, CronJob и необязательных NetworkPolicy там, где они создаются. Управление Role/RoleBinding, ServiceAccount и Lease требуется только при явно включённой подготовке администратором. Worker уже должны иметь права на заранее созданные именованные Lease; отключение управления RBAC не обходит авторизацию. По умолчанию Helm хранит состояние release в Secret его namespace. Оператору ручного запуска дополнительно нужны CronJob get, Job create/get/patch для отмены, Pod/log get и PVC get/Pod list для выбранных claim. Worker получает get/update только явно именованных Lease, backup/repository — чтение статуса Job, при необходимости — get разрешённых PVC и list Pod. Нет чтения Secret через API, изменения источника, Pod exec, кластерных разрешений и создания Job. Проекция Secret через kubelet не требует у worker API-прав на Secret.

[Обычная Role deployer](../../examples/deployer-rbac.yaml) не управляет аккаунтами/RBAC/Lease. Разрешения на NetworkPolicy исключены, поскольку она выключена по умолчанию; добавьте их отдельно только при её включении. Права Secret здесь нужны для состояния Helm. Чтение/PVC и Lease get/update для прямых команд каталога — отдельные права оператора. [Административный пример Role установщика](../../examples/installer-rbac.yaml) рассчитан на один существующий namespace backups. Администратор создаёт RoleBinding и заменяет субъект. Уберите serviceaccounts из этой Role, если все аккаунты уже существуют. Для нескольких namespace выдайте соответствующую namespaced-часть в каждом; не превращайте пример в ClusterRoleBinding. Kubernetes разрешает создание Role/RoleBinding, только если установщик уже имеет выдаваемые права либо явные bind/escalate; пример выдаёт сами права без bind/escalate. См. [официальную документацию RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#restrictions-on-role-creation-or-update).

```sh
kubectl auth can-i create cronjobs.batch -n backups
kubectl auth can-i update leases.coordination.k8s.io -n backups
kubectl auth can-i create rolebindings.rbac.authorization.k8s.io -n backups
helm upgrade --install pgbk charts/pgbk --namespace backups -f values.local.yaml -f examples/existing-service-accounts.yaml
```

## S3 и Yandex

Подготовьте bucket, endpoint, ключи и политику вне продукта. Для Yandex задайте `https://storage.yandexcloud.net`, регион `ru-central1`, path addressing и существующий Secret ключей. Это соответствует [S3 API Yandex](https://yandex.cloud/ru/docs/storage/s3/); живой тест выполняет оператор. Ограничьте list/read/write/delete и права multipart upload/abort нужным bucket/префиксом; проверьте точное поведение политики провайдера. Шифрование bucket управляется извне; специфические заголовки шифрования в этой версии не настраиваются.

WAL-G st передаёт исходные байты с явно отключёнными преобразованиями сжатия/шифрования. boto3 сохраняет метаданные и независимо перечитывает каждый артефакт для SHA-256. ETag не подтверждает целостность. Повторы временных ошибок метаданных/передачи ограничены числом и deadline; ошибочная выгрузка БД не продолжается. Настройте внешнее lifecycle-правило AbortIncompleteMultipartUpload для незавершённых multipart после потери узла/процесса; продукт не меняет политику/lifecycle bucket. Артефакты не имеют формат WAL-G backup-push и не восстанавливаются через backup-fetch.

## Ручные и плановые операции

```sh
pgbk --config values.local.yaml --namespace backups backup run --profile example-logical
kubectl -n backups create job example-manual --from=cronjob/pgbk-example-logical-backup
kubectl -n backups logs job/example-manual
pgbk --config values.local.yaml --namespace backups backup cancel --profile example-logical --job JOB_NAME
pgbk --config values.local.yaml --namespace backups retention run --profile example-logical
```

Расписания используют явный IANA timezone, по умолчанию UTC. Планирование Kubernetes выполняется без строгих гарантий; переходы летнего времени могут пропускать/повторять запуски. startingDeadlineSeconds ограничивает опоздавший старт; гарантий catch-up/exactly-once нет. Каждая попытка получает случайный новый ID. Source Lease охватывает ручные/плановые запуски и псевдонимы. Repository Lease сериализует публикацию, скачивание, восстановление, очистку и изменения pin/отчётов проверки; длительная проверка удерживает устойчивый pin копии и общий capacity Lease; длительная операция может давать busy при опросе каталога. Потеря обновления API останавливает утилиты и блокирующие SQL; просроченный Lease не перехватывается.

Для зависшего Lease сначала подтвердите завершение worker, всех потомков и временного сервера, включая изоляцию потерявшего связь узла/процесса. Удалённого API-объекта Pod/Job или истечения времени недостаточно. Зафиксируйте инцидент, прочитайте holder/resourceVersion и очистите только ожидаемый holder обновлением Kubernetes compare-and-swap. Не удаляйте все Lease при очистке/обновлении Helm. Общие источники/репозитории требуют одинаковых namespace/ключей координации между установками; независимые writer в разных кластерах не поддерживаются.

## Скачивание и автономное восстановление

Подробный порядок всех сценариев — в [руководстве полного цикла восстановления](restore.md): logical и physical, локальный сервер и managed, роли/владельцы/ACL, подготовка назначения, проверки и повтор после ошибки. Оно включено в исходный пакет и wheel (`share/doc/pgbk/ru/restore.md` относительно префикса установки). Связанные файлы документации доступны в исходном пакете.

```sh
pgbk --config values.local.yaml --namespace backups backup download --profile example-logical --run-id RUN_ID --destination /tmp/isolated-download --database application
kubectl -n backups label pvc download-output pgbk.io/installation=example-cluster pgbk.io/purpose=download
pgbk --config values.local.yaml --namespace backups backup download --profile example-logical --run-id RUN_ID --destination /destination/RUN_ID --destination-pvc download-output --acknowledge-dedicated-pvc
```

Используйте выделенное пустое назначение; у локального пути не должно быть родительских symlink. PVC скачивания должен быть свободным и помеченным для установки/назначения, никогда исходным PGDATA. Скачивание содержит manifest.json, DOWNLOAD.json и инструкции RESTORE на двух языках; при скачивании одной базы перечислены только скачанные ID. Проверьте SHA-256 и используйте pg_restore соответствующей версии либо безопасную физическую распаковку/pg_verifybackup без исходного кластера и сервиса каталога. Нативные инструкции входят в пакет и каждое скачивание. Прямые операции pgbk с каталогом требуют координации Kubernetes; нативное восстановление скачанных байтов — нет. Необязательный scripts/restore_download.py проверяет хеши скачанных файлов и восстанавливает/подготавливает их без доступа к S3/Kubernetes; по-прежнему нужны отдельные учётные данные назначения и подтверждённая изоляция.

## Logical и physical восстановление

Новые копии содержат отдельные сопроводительные `recovery/BACKUP-RESTORE.md` и `recovery/BACKUP-RESTORE.ru.md` в S3. Они формируются по зафиксированному манифесту, проверяются до маркера завершения и учитываются в `recoveryInstructions` с размером и SHA-256, отдельно от файлов PostgreSQL в `artifacts`. При скачивании инструкции попадают в корень назначения; для старых копий и скачивания одной базы формируется актуальная версия по скачанным ID, без изменения исходного бэкапа. `DOWNLOAD.json` фиксирует происхождение/хеши и помогает отличить недоступный globals от скачанного.

```sh
pgbk --config values.local.yaml --namespace backups restore logical --profile example-logical --run-id RUN_ID --database application --destination-config examples/destination.json
pgbk --config values.local.yaml --namespace backups restore logical --profile example-logical --run-id RUN_ID --database application --destination-config examples/destination.json --as-job
```

Замените placeholders [destination.json](../../examples/destination.json) изолированным TCP endpoint/базой и отдельными учётными данными. По умолчанию обязательны acknowledgeIsolated, destinationPolicy empty и переносимый режим no-owner/no-acl/no-tablespaces. preserveOwnership явно сохраняет требования к владельцам/ACL/tablespace. Destructive clean/drop и выполнение globals SQL не автоматизируются. Совпадения endpoint/IP и обнаруженного system identifier с источниками отклоняются; административная изоляция обязательна, поскольку псевдонимы и ограниченные метаданные могут скрывать идентичность. Ошибка нативного восстановления возвращает RESTORE_PARTIAL; перед повтором очистите/пересоздайте назначение.

Для выборочных архивов подготовьте известные схемы/типы/расширения/FK/родителей и явно выберите `destinationPolicy: prerequisites-only`. Выбранные отношения не должны существовать. Известные зависимости проверяются; неполное рекурсивное выявление всё равно может вызвать явную ошибку нативной утилиты. Drill автоматически создаёт только выбранные схемы; пользовательские зависимости должны присутствовать в совместимом образе/назначении, иначе проверка завершится ошибкой.

Следующие фрагменты внутри профиля включают изолированные Job. Все namespace/Secret/образы/claim должны существовать; замените placeholders. Restore и verification можно включать независимо.

```yaml
restore:
  enabled: true
  namespace: restore
  timeoutSeconds: 21600
  credentialsSecretRefs: [restore-postgres, restore-ca]
  allowedDestinationClaims: [isolated-pgdata]
  resources:
    requests: {cpu: "500m", memory: 512Mi}
    limits: {cpu: "2", memory: 2Gi}
  scratch: {type: pvc, size: 40Gi, storageClassName: standard}
verification:
  enabled: true
  environmentRef: pg18-isolated
  required: true
  dueWithinSeconds: 86400
```

```sh
kubectl -n restore label pvc isolated-pgdata pgbk.io/installation=example-cluster pgbk.io/purpose=restore
pgbk --config values.local.yaml --namespace backups restore physical --profile example-physical --run-id RUN_ID --major 17 --pgdata /destination/pgdata --tablespace-map '{"16384":"/destination/tablespace-16384"}' --destination-pvc isolated-pgdata --acknowledge-isolated-stopped
```

Используйте фактические OID tablespace из манифеста; карта должна включать ровно все tablespace. Все пути Job должны находиться под /destination, в пустых непересекающихся каталогах. PVC physical-назначения должен быть разрешённым, помеченным purpose=restore, свободным, выделенным и административно подтверждённым остановленным/изолированным. Worker повторно проверяет владельца/подключения Pod после монтирования. RWO не является изоляцией узла: остановите/изолируйте неоднозначного предшественника до запуска. Существующие claim назначения не удаляются.

Physical restore проверяет хеши, пути архива, основную версию/архитектуру и pg_verifybackup до замены исходных postgresql.conf/postgresql.auto.conf/HBA/сопоставлений идентичности и recovery-сигналов изолированными настройками. Отчёт перечисляет изменённые файлы без учётных данных. Restore не запускает сервер; deployment, совместимые бинарные файлы/расширения, внешняя конфигурация и переключение production выполняет оператор. Без --destination-pvc прямая команда подготавливает явно подтверждённые локальные/подключённые каталоги, не подтверждая принадлежность PVC. Для встроенной проверки PVC используйте Job.

## Проверка, retention и мониторинг

```sh
pgbk --config values.local.yaml --namespace backups verify --profile example-logical --run-id RUN_ID --verification-profile pg18-isolated
pgbk --config values.local.yaml --namespace backups --output json backup list --profile example-logical
```

Verification Job использует только учётные данные репозитория/назначения, восстанавливает в одноразовое scratch, запускает PostgreSQL на loopback/закрытом сокете, читает пользовательские таблицы и отчёт о каталогах с необязательными read-only assertions. Physical-проверка использует только скачанные данные/WAL; подключение к источнику не требуется. Отчёт содержит время, версии образа/утилит, проверки scope и passed/failed независимо от завершения копии. Файлы ошибки остаются только при retainFailed, до удаления завершённого Job/Pod по failureDebugTtlSeconds; успешные файлы очищаются сразу. Освобождение PVC следует политике хранилища кластера.

Retention защищает оба набора новейших N целостных и N соответствующих профилю копий, ожидающие обязательной проверки копии до срока и активные operation pin. Статусы, неизменяемые попытки, сроки, reconciliation, очистка и отдельный мониторинг возраста данных описаны в [ручной проверке](verification.md). Частичное удаление продолжается; незавершённые копии по-прежнему требуют grace и подтверждённого завершения writer. Helm uninstall сохраняет удалённые данные, существующие Secret/PVC, сохранённые ConfigMap и Lease.

## Ёмкость и диагностика

### Ошибки выгрузки globals

При `logical.globals: enabled` pgbk выгружает роли/глобальные объекты через `pg_dumpall --globals-only --no-role-passwords`. pg_dumpall не выбирает начальную базу из `PGDATABASE`: ему нужен `--database` (`-l`), иначе он пробует `postgres` и может перейти к `template1`. Начиная с 0.3.3 pgbk явно передаёт `targets.<id>.maintenanceDatabase`, сохраняя буквальные имена, и выгружает globals до архивов баз, чтобы не тратить время на дамп при ошибке.

Задайте `maintenanceDatabase` существующей базой, к которой может подключиться роль бэкапа, например `ia`. По умолчанию остаётся `postgres`. Проверьте CONNECT к этой базе и доступ к каталогам ролей/членства/tablespace. `--no-role-passwords` исключает чтение хешей паролей через pg_authid; сам по себе сбой globals не доказывает необходимость SUPERUSER. В managed возможны дополнительные ограничения провайдера.

В Yandex Managed Service for PostgreSQL база `postgres` системная и подключения к ней запрещены ([документация провайдера](https://yandex.cloud/en/docs/managed-postgresql/qa/connection)). Явно задайте прикладную базу в target, на который ссылается профиль; выбор базы подключения не ограничивает выгрузку ролей только этой базой:

```yaml
targets:
  managed:
    maintenanceDatabase: ia
```

В 0.3.2 и ранее stderr потокового subprocess отбрасывался, поэтому `TOOL_FAILED` с `tool: pg_dumpall` / кодом 1 не раскрывает причину PostgreSQL. В 0.3.3 streaming и staged одинаково классифицируют распознанные ошибки прав/аутентификации как `ACCESS_DENIED`, указывая инструмент и код завершения. Исходные stderr/SQL/credentials остаются скрыты; из старого общего лога нельзя выводить конкретное нативное сообщение. Неуспешная выгрузка globals запрещает публикацию завершённой копии, даже если часть файлов баз уже загружена.

Logical scratch рассчитан на все custom-архивы и временные файлы утилит. Physical backup требует tar-артефакты И распакованную копию для проверки, включая WAL/tablespace; drill/restore — скачанные архивы И развёрнутые PGDATA/tablespace. Оставляйте запас для активности WAL и метаданных ФС. Eviction по EmptyDir sizeLimit зависит от наблюдения kubelet; operationally задавайте node ephemeral-storage budgets. Лимиты CPU/памяти профиля, deadline и разнесённые расписания ограничивают работу, но не гарантируют throughput/RPO/RTO. Локальные проверки 1/20 GiB и их ограничения описаны в [совместимости](compatibility.md); измерьте расход и время также на своём источнике и storage.

Коды выхода: 0 успех, 2 неверный ввод, 3 требования/неподдерживаемое, 4 занято, 5 выполнение, 6 целостность, 7 отмена, 8 авторизация. Native stderr сводится к безопасным классам/кодам выхода утилит, чтобы не раскрывать учётные данные/SQL; подробности ищите в доверенных upstream/server audit logs.

| Код причины | Действие |
|---|---|
| `INVALID_CONFIG / INVALID_REFERENCE` | Исправьте схему/ссылки до обращения к источнику. |
| `CLIENT_MAJOR_MISMATCH / SERVER_MAJOR_MISMATCH` | Выберите worker соответствующей основной версии и подтверждённую версию источника. |
| `POSTGRES_CONNECTION_FAILED / ACCESS_DENIED` | Проверьте существующие права, TLS, endpoint и проекцию Secret. Вывод нативных утилит скрыт. |
| `SOURCE_BUSY / LEASE_LOST` | Не захватывайте Lease другого holder; завершите/изолируйте прежние процессы и проверьте обновление Lease. |
| `SOURCE_IDENTITY_MISMATCH / SOURCE_IDENTITY_CHANGED` | Повторно подтвердите нужный прямой primary и system identifier; новый запуск получает новый ID. |
| `EMPTY_TABLE_SELECTION / QUERY_ROW_LIMIT / SELECTION_FAILED` | Проверьте включения/исключения, лимит исходных строк, права чтения и таймаут запросов/блокировок. |
| `UNSAFE_PVC / PVC_IN_USE / PVC_OWNERSHIP_UNVERIFIED` | Используйте выделенный claim с метками из разрешённого списка; проверьте подключения и изоляцию узла. |
| `SCRATCH_UNWRITABLE / SCRATCH_IO_FAILED` | Проверьте ёмкость, владельца ФС и storage class; сохраните события Kubernetes. |
| `BACKUP_INCOMPLETE / ARTIFACT_HASH_MISMATCH / MANIFEST_MISMATCH` | Копия непригодна; выберите другую проверенную копию и выясните причину повреждения. |
| `S3_ACCESS_DENIED / S3_RETRY_EXHAUSTED / DELETE_DENIED` | Проверьте ключи, срок действия, политику, сеть или Object Lock. Ошибка очистки отделена от пригодности копии. |
| `RESTORE_SOURCE_REFUSED / DESTINATION_NOT_EMPTY` | Подготовьте изолированное пустое назначение. Не направляйте восстановление на источник. |
| `RESTORE_PREREQUISITE_MISSING / RESTORE_PARTIAL` | Подготовьте известные зависимости выборочного архива. После частичной ошибки очистите/пересоздайте назначение. |
| `VERIFICATION_ASSERTION_FAILED / VERIFICATION_STARTUP_FAILED` | Проверьте отдельный отчёт проверки и сохранённые временные файлы; не удаляйте завершённую копию. |
| `CANCELLED / DEADLINE_EXCEEDED` | Проверьте состояние/логи Job; незавершённая копия непригодна. Новый запуск получает новый ID. |

Настройки передачи/сжатия logical 0.2 и directory restore описаны в [режимах logical backup](logical-modes.md). Direct использует multipart SDK upload вместо WAL-G transfer.
