# Справочник конфигурации

Версия: 0.3.2. [Быстрый старт](quickstart.md). Строгая схема — [values.schema.json](../../charts/pgbk/values.schema.json); неизвестные ключи, повторяющиеся ключи YAML и встроенные учётные данные отклоняются. ID и ссылки на Secret/namespace состоят из строчных латинских букв, цифр и дефисов, максимум 48 символов. Сочетание release/profile должно помещаться в ограничение имени CronJob в 52 символа. [Полный пример](../../examples/values.yaml).

| Ключ | Значение |
|---|---|
| `installationId` | Обязательный стабильный идентификатор каталога. |
| `coordinationNamespace` | Существующий namespace; по умолчанию namespace операции. Задавайте явно при нескольких namespace. |
| `rbac.create / coordination.createLeases` | Оба по умолчанию false: используйте существующие права и именованные Lease; создание включается только для подготовки администратором. |
| `images.<14..18>` | Worker Linux amd64 соответствующей основной версии, с digest sha256. Обязателен для Helm. |
| `targets.<id>.sourceId / concurrencyKey` | Стабильный идентификатор источника / общий ключ сериализации. Для псевдонимов нужен одинаковый ключ. |
| `targets.<id>.host / port` | Inline TCP-адрес; порт по умолчанию 5432. При Secret-отображении обоих полей не задаются. |
| `targets.<id>.serverMajor / maintenanceDatabase` | 14–18; служебная база по умолчанию postgres. |
| `targets.<id>.credentialsSecretRef` | Существующий Secret PostgreSQL. |
| `targets.<id>.credentialsSecretKeys` | Соответствие ключей host/port/username/password общего Secret. Host и port выбираются только вместе; для username/password остаются стандартные имена по умолчанию. |
| `targets.<id>.tls.mode` | По умолчанию verify-full; явно доступны disable/allow/prefer/require/verify-ca. |
| `targets.<id>.tls.caSecretRef / clientSecretRef` | Необязательные Secret с CA и клиентским сертификатом. |
| `targets.<id>.physicalEndpoint / expectedSystemIdentifier` | Для physical-full обязательны direct-primary и подтверждённый числовой идентификатор системы. |
| `storage.<id>.endpoint / region / bucket / prefix` | Существующие S3 endpoint и bucket; prefix — непустые ASCII-компоненты через косую черту. |
| `storage.<id>.addressingStyle` | По умолчанию path; также доступен virtual. |
| `storage.<id>.credentialsSecretRef` | Существующий Secret с accessKeyId/secretAccessKey и необязательным sessionToken. |
| `storage.<id>.tls.verify / caSecretRef` | verify должен быть true; необязательный Secret частного CA. HTTP допустим для явно заданного тестового endpoint. |
| `targets.<id>.provider` | generic или yandex-managed-postgresql. Physical отклоняется для Yandex Managed PostgreSQL, включая известные endpoint провайдера; fallback отсутствует. |
| `profiles.<id>.target / storage / kind` | Явные ссылки и вид logical либо physical-full. |
| `profiles.<id>.download.allowedDestinationClaims` | Явный список PVC для Job скачивания; по умолчанию пустой. Worker повторно проверяет принадлежность/подключения после монтирования. |
| `profiles.<id>.logical.databases / globals` | Уникальные буквальные имена баз; globals по умолчанию disabled, enabled экспортирует SQL без паролей ролей. |
| `profiles.<id>.logical.tableSelection` | Правила выборки описаны ниже; без правила база получает обычный полный архив custom. |
| `profiles.<id>.schedule` | enabled по умолчанию false. cron: пять числовых полей; timeZone: по умолчанию UTC; startingDeadlineSeconds: по умолчанию 3600. |
| `profiles.<id>.execution.resources` | Обязательные положительные CPU/memory requests и limits, requests <= limits. |
| `profiles.<id>.execution.activeDeadlineSeconds / ttlSecondsAfterFinished` | По умолчанию 21600 / 86400. TTL удаляет завершённые Job/Pod, а не удалённые копии. |
| `profiles.<id>.execution.nodeSelector` | Необязательные метки узлов; обязательна платформа linux/amd64. |
| `profiles.<id>.scratch` | Обязательные type и положительный size; подробности ниже. |
| `profiles.<id>.retention` | maxAgeDays: 30, minSuccessfulBackups: 3, incompleteGraceDays: 7. Положительные целые. Расписание по умолчанию включено, 0 4 * * *, UTC. |
| `profiles.<id>.verification` | enabled по умолчанию false; enabled требует environmentRef. required требует dueWithinSeconds > 0; расписание verification не нужно. |
| `verificationEnvironments.<id>` | Именованные namespace, serviceAccountName, concurrencyKey, digest образа, resources, scratch, timeoutSeconds и обе ссылки на Secret; см. [проверки](verification.md). |
| `verificationEnvironments.<id>.assertions / maxConcurrent / failureDebugTtlSeconds` | Read-only boolean assertions; общая ёмкость по умолчанию 1; debug TTL Job по умолчанию 86400. Существующие claim не удаляются. |
| `profiles.<id>.restore` | Включаемые явно Job: enabled, namespace, resources, scratch, timeoutSeconds, credentialsSecretRefs и allowedDestinationClaims. По умолчанию выключены. |
| `profiles.<id>.serviceAccounts` | create по умолчанию false. При create: false задайте backup/repository и verify/restore для включённых операций — имена существующих аккаунтов в namespace операции. |
| `networkPolicy.enabled / egress` | Необязательная политика с запретом входящих и явно заданными исходящими соединениями. По умолчанию выключена; разрешите DNS, API Kubernetes, S3 и нужные адреса назначения. |

## Общий Secret PostgreSQL

Один Secret может содержать host, port, username и password для каждого target. В values остаются только ссылки и имена ключей. Для этого задайте ОБА отображения `host` и `port` внутри `credentialsSecretKeys` и полностью уберите inline-поля `host`/`port` из target. Остальные обязательные параметры target (sourceId, concurrencyKey, serverMajor, TLS) сохраняются:

```yaml
targets:
  managed:
    credentialsSecretRef: database-credentials
    credentialsSecretKeys:
      host: managed-host
      port: managed-port
      username: managed-username
      password: managed-password
  kubernetes:
    credentialsSecretRef: database-credentials
    credentialsSecretKeys:
      host: kubernetes-host
      port: kubernetes-port
      username: kubernetes-username
      password: kubernetes-password
```

Пример содержимого — [Secret с подключениями двух targets](../../examples/shared-db-credentials-secret.yaml). `port` в stringData хранится строкой, например `"6432"`; при выполнении pgbk проверяет целое значение 1–65535 и TCP-hostname. Частичная или смешанная конфигурация endpoint запрещена: нет fallback на inline-адрес или порт 5432, если явно выбраны Secret-ключи. `config validate` и Helm проверяют структуру без чтения Secret; значения читаются из файлов проекции только при выполнении backup/preflight, без API-чтения Secret. Ошибки не раскрывают значения.

Для Helm используйте [overlay всех полей подключения](../../examples/yandex/shared-db-connection-override.yaml) поверх two-sources.yaml. Значения `null` в overlay очищают унаследованные inline host/port; они допускаются только при Secret-отображении обоих полей и удаляются нормализацией pgbk. В полном конфиге их можно просто не указывать. Имена ключей не являются адресами. Разные псевдонимы одного физического источника должны иметь один concurrencyKey, даже если endpoint скрыт в разных Secret; это остаётся обязанностью конфигурации. Одинаковые sourceId/Secret-ключи проверяются при валидации, совпадения endpoint в общем смонтированном Secret — до бэкапа.

Backup/preflight Job монтирует общий Secret один раз; restore/download/retention не получают Secret источника автоматически. При восстановлении адрес исходного сервера берётся из зафиксированного `sourceEndpoint` манифеста: он сохраняется в ограниченном S3-каталоге для отказа восстановления на источник. Другие скрытые endpoints без их Secret разрешить/проверить невозможно; реальная изоляция назначения обязательна. Пароли не помещаются в манифест, ConfigMap, argv или логи.

Несколько targets могут использовать один Secret с разными именами ключей:

```yaml
targets:
  managed:
    credentialsSecretRef: database-credentials
    credentialsSecretKeys:
      username: managed-username
      password: managed-password
  kubernetes:
    credentialsSecretRef: database-credentials
    credentialsSecretKeys:
      username: kubernetes-username
      password: kubernetes-password
```

Если весь объект `credentialsSecretKeys` или его поля username/password пропущены, используются стандартные ключи `username` или `password`. Отсутствие явно указанного ключа завершает операцию ошибкой; переключения на другую пару credentials нет. Ключи должны быть допустимыми именами файлов проекции Secret: буквы, цифры, точки, подчёркивания и дефисы, до 253 символов; имя `.` и префикс `..` запрещены.

См. [overlay двух источников](../../examples/yandex/shared-db-credentials-override.yaml), применяемый поверх [two-sources.yaml](../../examples/yandex/two-sources.yaml), и [шаблон Secret](../../examples/shared-db-credentials-secret.yaml). Values содержат только имена ключей, настоящие credentials остаются в Secret. Каждый Job монтирует Secret целиком и может прочитать все пары credentials в нём. Secret должен существовать в namespace Job. JSON назначения PostgreSQL для восстановления также принимает `credentialsSecretKeys` с теми же defaults; для restore Job добавьте Secret в `restore.credentialsSecretRefs`. Названия ключей S3 и verification не меняются.

## Включение и исключение таблиц

`logical.tableSelection.queryTimeoutSeconds` по умолчанию 30; `maxQueryRows` — 10000, максимум 100000. Ключи `byDatabase` должны обозначать настроенные базы. Правило содержит `mode: all` (по умолчанию) или `selected`, необязательные `include` и `exclude` с `tables: [{schema, table}]` и/или `query`. Ручной список и результаты запросов объединяются без повторов; исключение имеет приоритет. В `all` включения запрещены. В `selected` нужны включение и непустой итоговый набор. Отсутствующие явно заданные или полученные запросом объекты вызывают ошибку; пустое исключение допустимо.

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

Имена буквальные, включая точки, кавычки, Unicode и символы шаблонов. Выбирать можно только обычные и секционированные пользовательские таблицы; представления, внешние/материализованные таблицы и системные объекты отклоняются. Отношения расширений поддерживаются в исключениях; их явное включение запрещено. Выбор родителя включает потомков, в том числе наследование; исключение потомка удаляет его определение и данные. Выбор листа не добавляет родителя и соседние секции.

Запрос должен быть единственным SELECT, возвращающим строго `schema_name` и `table_name` типа PostgreSQL text, без NULL, пустых имён и переводов строк. Записывающие CTE, SELECT INTO, блокирующие предложения и несколько операторов отклоняются парсером PostgreSQL. Выполнение использует read-only repeatable-read, серверный курсор, таймаут и лимит исходных строк до удаления повторов. Запросы являются доверенной конфигурацией администратора: read-only SQL не изолирует функции с побочными эффектами. Текст запроса не журналируется; манифест содержит хеши и происхождение выборки. Каждая база получает отдельный экспортированный снимок, удерживаемый до окончания pg_dump. Блокировки access-share фиксируют определения выбранных/исключённых таблиц; OID архива сверяются с набором. Общего снимка всего кластера нет.

Любое настроенное правило помечается `filtered-database`. Объекты, принадлежащие таблицам, следуют поведению pg_dump. Для восстановления могут понадобиться внешние схемы, типы, функции, расширения, родители секций и таблицы внешних ключей. Известные схемы, непосредственные типы столбцов, FK-таблицы и родители записываются; рекурсивное выявление зависимостей явно неполное. В selected по умолчанию `largeObjects: none`; явное `all` включает ВСЕ большие объекты базы, за пределами списка таблиц. В all сохраняется поведение pg_dump; исключение таблицы не определяет принадлежность больших объектов.

## Временное хранилище и изолированные операции

`scratch.type: emptyDir` использует `size` как sizeLimit. `pvc` создаёт generic ephemeral claim заданного размера с необязательным storageClassName; он принадлежит Pod и удаляется при удалении Pod с учётом работы контроллера хранилища. `existingClaim` требует claimName и allowedClaims; необходимы метки установки/назначения и отсутствие подключения другого активного Pod. Существующие claim никогда не удаляются. `retainFailed` по умолчанию false; для existingClaim он запрещён, поскольку ограниченное сохранение отладки требует тома, принадлежащего Pod.

Namespace проверки/восстановления должны существовать. Образы должны содержать pgbk и совместимые бинарные файлы сервера, клиентов и расширений PostgreSQL, работать с UID/GID 999 и read-only корневой ФС; обычный образ postgres без pgbk не выполнит шаблон. Доставляйте нужные S3/CA/Secret назначения в эти namespace своим механизмом управления секретами. Chart не копирует Secret и не подключает исходные тома данных. Restore Job использует обычный worker соответствующей основной версии; verification допускает совместимый пользовательский образ по digest.

```yaml
# Overlay on a full values file. Accounts must already exist in each operation namespace.
profiles:
  example-logical:
    serviceAccounts:
      create: false
      backup: existing-backup-worker
      repository: existing-repository-worker
```

Права, назначения восстановления и требования к источникам описаны в [эксплуатации](operations.md). Валидация схемы проверяет структуру и ссылки; права endpoint, ёмкость хранилища и совместимость восстановления требуют preflight и приёмочных испытаний.

## Выбор схем

Версия 0.3.1 поддерживает `logical.schemaSelection` для каждого профиля. Этот overlay исключает всю схему `cron`, используемую расширением `pg_cron`.

```yaml
profiles:
  example-logical:
    logical:
      schemaSelection:
        exclude: [cron]
```

Разместите настройку внутри своего существующего профиля, например `ia-dev-backup`. При исключении всей схемы удалите прежний список из двух таблиц `cron.job` / `cron.job_run_details`. Используйте чарт и worker версии 0.3.1. Валидатор 0.3.0 отклоняет этот ключ.

`include` и `exclude` по умолчанию пусты и содержат точные имена схем. Пустой include сохраняет обычный охват дампа. Исключения имеют приоритет. Имена разрешаются отдельно в каждой базе; несовпавшие имена записываются в metadata. Непустой include без оставшихся схем завершает базу/запуск ошибкой. Кавычки, Unicode, точки и символы шаблонов трактуются буквально. Пустые списки сохраняют прежнее поведение дампа.

Фильтры схем применяются после раскрытия partitions/inheritance, до табличных исключений. В selected-режиме передаётся только разрешённый точный список таблиц. Остальные режимы используют флаги схем PostgreSQL. Манифест содержит настроенные/разрешённые схемы, таблицы, отсеянные фильтром схем, и политику large objects. Preflight использует тот же обработчик, с отдельным snapshot для каждой операции.

`largeObjects: none | all` необязателен. Непустой include и selected-режим таблиц по умолчанию используют `none`; режим только исключений сохраняет обычное поведение `all`. Явные политики схем/таблиц должны совпадать. `all` включает все large objects базы. Globals остаются отдельным артефактом со своим настроенным охватом.

Таблицы расширений можно исключать явно и через query. Допускается исключение всей схемы расширения. Проверка архива контролирует отдельные определения и записи TABLE DATA, включая конфигурационные таблицы расширений. При восстановлении `CREATE EXTENSION` может создать исключённые объекты с установочными значениями. Для восстановления нужны бинарные файлы расширений; внешние зависимости выбранных объектов также могут потребовать предварительной подготовки. Локальный тест pg_cron проверяет отсутствие исходных заданий и истории после восстановления при сохранении данных приложения.

## Настройки logical-режимов (0.3.1)

См. [режимы logical backup](logical-modes.md): logical.transfer.mode, logical.transfer.partSizeMiB, logical.format, logical.jobs и logical.compression.method/level, defaults, совместимость и пример двух источников.
