# Ручная проверка копий и retention

Реализация спецификации 1.3. Локальные доказательства для PostgreSQL 18 отделены от live-provider и полной приёмки в [совместимости](compatibility.md). Фильтры схем (REQ-043–REQ-045) реализованы в 0.3.1. Доступ к логам (REQ-046–REQ-048) и автоматизация проверок (REQ-054) остаются отложенными.

Именованные окружения задаются в корне values. Ниже overlay: сохраните target, storage, logical, execution и scratch существующего профиля. Замените digest образа и предоставьте существующие namespace, ServiceAccount, RBAC, Lease и указанные Secret либо явно включите bootstrap chart. Образ должен содержать pgbk и совместимые бинарные файлы сервера/клиента/расширений PostgreSQL. Положительный maxConcurrent использует общие предварительно созданные Lease-слоты; исчерпание ёмкости возвращает busy без постановки в очередь.

```yaml
verificationEnvironments:
  pg18-isolated:
    namespace: verification
    serviceAccountName: pgbk-verifier
    concurrencyKey: isolated-pg18
    maxConcurrent: 1
    image: registry.example/pgbk-worker-18@sha256:0000000000000000000000000000000000000000000000000000000000000000
    resources:
      requests: {cpu: "500m", memory: 512Mi}
      limits: {cpu: "2", memory: 2Gi}
    scratch: {type: pvc, size: 40Gi, storageClassName: standard}
    timeoutSeconds: 21600
    failureDebugTtlSeconds: 86400
    credentialsSecretRef: verification-server
    clientCredentialsSecretRef: verification-client
    assertions: ["SELECT NOT pg_is_in_recovery()"]
verificationReconciliation:
  enabled: true
  cron: "*/5 * * * *"
  timeZone: UTC
profiles:
  example-logical:
    verification:
      enabled: true
      environmentRef: pg18-isolated
      required: true
      dueWithinSeconds: 86400
```

По умолчанию проверка выключена. enabled с environmentRef разрешает необязательные ручные проверки; required и положительный dueWithinSeconds задают обязательство для каждой копии. Включённое расписание verification отклоняется: расписание reconciliation лишь фиксирует сроки и восстанавливает доказанно завершившиеся попытки. Оно не запускает restore-тесты. Прежние inline-поля окружения нужно перенести в verificationEnvironments.

При завершении backup манифест фиксирует несекретную политику, срок, окружение, digest конфигурации, ссылку на сохранённый ConfigMap и ссылки на Secret. Изменения профиля не меняют обязательства старых копий. Значения Secret могут ротироваться; сохраняйте прежний namespace, ConfigMap, identity, RBAC, Secret и образ для повторных попыток. Явно выбранное альтернативное окружение фиксируется без изменения исходного срока и требования.

```sh
pgbk --config values.local.yaml --namespace backups verify --profile example-logical --run-id RUN_ID --verification-profile pg18-isolated
pgbk --config values.local.yaml --namespace backups verify --profile example-logical --run-id RUN_ID --verification-profile pg18-isolated
pgbk --config values.local.yaml --namespace backups verification reconcile --profile example-logical
pgbk --config values.local.yaml --namespace backups --output json backup describe --profile example-logical --run-id RUN_ID
```

Ручная verify требует точный ID копии и явный --verification-profile. Выбор сохранённого имени окружения использует неизменяемые настройки; для альтернативы укажите другое именованное окружение. Она читает каталог с локальными проекциями S3-учётных данных и резервирует попытку под repository coordination до создания Job. Оператору нужны get/update именованных Lease, get backup CronJob и create Job в выбранном namespace проверки. Worker не получает учётных данных источника или прав изменения его workload. В отличие от backup run, verify требует доступ к каталогу с CLI-хоста. Немедленный JSON-ответ содержит Job, попытку и копию; итог дают код выхода Job и неизменяемый отчёт. Читайте kubectl logs возвращённого Job, затем describe копии.

Завершённая целостная копия имеет successful, включая ожидание обязательной проверки; backup возвращает 0. Обязательные успех/ошибка дают verification-successful/verification-failed и profileCompliant. Ошибка необязательной проверки показывается отдельно и оставляет successful. Неудачная проверка не меняет хеши manifest/marker/артефактов и не запрещает скачивание целостной копии.

Одноразовый сервер слушает только loopback/закрытый сокет. Проверка контролирует хеши, запуск, код restore, inventory схем/таблиц/индексов/ограничений/последовательностей архива и восстановленной БД, количество строк таблиц и необязательные ограниченные read-only SQL assertions. Нестандартные зависимости остаются требованиями к окружению. Inventory не доказывает корректность приложения. Отчёты различают ошибки целостности, окружения, запуска, restore, assertions и Job. Логи worker сохраняют действующий контракт скрытия чувствительных данных; расширенные live/replay-логи отложены.

Каждая попытка имеет неизменяемый упорядоченный отчёт. Активный retry сохраняет последний завершённый результат. Повторный запрос возвращает VERIFICATION_BUSY с activeAttemptId. Обязательная копия без действующего успешного результата становится overdue в исходный срок, даже без запуска проверки или при ещё работающей проверке. Чтение вычисляет это состояние независимо от service CronJob. Поздний успех реабилитирует ту же копию без обновления возраста данных и срока.

Retention сохраняет новейшие N целостных и новейшие N соответствующих профилю копий по времени завершения backup с разрешением совпадений по run ID; наборы могут отличаться. До срока незапущенная обязательная проверка дополнительно защищает копию. После срока заканчивается только защита ожидания; активный operation pin и оба минимума сохраняются. Незапущенная необязательная проверка не добавляет защиты. Создание pin и удаление сериализованы repository Lease: нельзя проверить удаляемую копию или удалить копию во время проверки.

Итоговый отчёт сохраняется до обычной очистки scratch. Claim, принадлежащие Pod, следуют TTL Job/Pod и политике storage controller; существующие claim не удаляются. Reconciliation закрывает попытку только при подтверждённом завершении соответствующего Job и контейнеров. Отсутствие API-объектов и потеря узла не доказывают изоляцию: сохраните pin и занятый Lease, изолируйте прежний worker/server и восстановите точно записанного holder через compare-and-swap согласно [эксплуатации](operations.md). Сохраните доказательства ошибки перед удалением ресурсов. После независимого подтверждения изоляции конкретного worker снимите его записанные слот/pin явной командой ниже; несовпадение ID попытки отклоняется.

```sh
pgbk --config values.local.yaml --namespace backups verification recover --profile example-logical --run-id RUN_ID --attempt-id ATTEMPT_ID --acknowledge-worker-fenced
```

JSON list отдельно показывает coverage.latestIntegrityConfirmed, coverage.latestProfileCompliant и тревоги обязательных проверок. snapshotAt каждой БД — консервативная нижняя граница перед dump, обозначенная backup-start-lower-bound; неизвестное время остаётся неизвестным. Для старых копий явно помечается completion-time-fallback. Время проверки не обновляет возраст данных. Для операторских целей RPO/RTO измеряйте фактическое время restore, подготовки назначения и переключения.


## Локальная проверка

Воспроизводимый драйвер испытывает новый worker с настоящими PostgreSQL 18, S3 и Kubernetes API. Используйте локальный Moto endpoint, доступный узлу, закреплённый локальный worker image и сохраняйте приватные логи. Драйвер создаёт только синтетические тестовые источники/namespace/Secret и удаляет свои namespace после завершения. До запуска Jobs он сверяет SHA-256 исходников работающего образа с workspace. Для ARM-узла minikube --allow-amd64-emulation явно включает переопределение планирования только в тесте; production chart по-прежнему требует Linux amd64. Отдельный S3 endpoint хоста может обращаться к тому же настоящему эмулятору через port forward без изменения идентичности репозитория.

```sh
python tests/kubernetes/spec13.py --context pgbk-local --worker-image "$WORKER_IMAGE" --s3-endpoint "$NODE_S3_ENDPOINT" --s3-host-endpoint "$HOST_S3_ENDPOINT" --logs /tmp/pgbk-validation
```

Запускайте tests/integration/offline_recovery.py в новом совместимом worker-контейнере с --network none, доступным для записи /scratch и скачанными logical/physical-каталогами, смонтированными read-only в /archives, после остановки исходного executor. Тест проверяет восстановленные значения fixtures, состояние sequences, партиции, ограничения и large objects. Отчёты должны обозначать эмуляцию и исключённую облачную сертификацию; исторические отчёты не сертифицируют новый изменённый образ.
