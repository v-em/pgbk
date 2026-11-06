# Эксплуатация pgbk-ui v1

Отдельные образ и chart `pgbk-ui` реализуют [спецификацию web UI v1](ui-v1.md) поверх контрактов pgbk 0.3.3. В консоли есть Backups, Schedules и Jobs. Единственная операция изменения резервных копий — разовый запуск. Версия worker остаётся 0.3.3, у UI chart отдельный цикл версий 0.1.0.

## Сборка и установка

Выполняйте команды из корня репозитория. UI использует упакованные статические файлы и HTTPS-сервер Python; отдельная сборка JavaScript и CDN не нужны. Зависимости Python зафиксированы существующим lock-файлом. Базовый образ UI и стадия uv закреплены digest. Для проверки другой базы задайте `PYTHON_BASE`.

```sh
docker build -f images/ui/Dockerfile -t pgbk-ui:0.1.0 .
helm lint charts/pgbk-ui -f examples/ui-values.yaml
helm upgrade --install pgbk-ui charts/pgbk-ui -n backup-console --create-namespace -f ui.local.yaml
```

Скопируйте [синтетический пример values](../../examples/ui-values.yaml) в локальный файл. Задайте installation ID, Helm release и namespace резервного копирования, точные разрешённые сочетания profile/source/kind и S3-префиксы. В `configMapNames` укажите имена immutable ConfigMap, на которые действительно ссылаются текущие backup CronJob. Несовпадение конфигурации или области отключает запуск и отображается в Schedules. Дополнительные namespace Job проверки и восстановления разрешаются явно.

Создайте TLS Secret вне Helm и Git. Сертификату нужны SAN для браузерного адреса и `<ui-release>.<ui-namespace>.svc`. Сервер всегда работает по TLS на 8443, Service предоставляет порт 443. Ingress должен использовать HTTPS к backend и обслуживать точно заданный `config.origin`. Укажите соответствующую аннотацию контроллера в `ingress.annotations` либо используйте существующий HTTPS reverse proxy. Выпуск и обновление сертификатов выполняет администратор установки. Secret с TLS, CA и S3 должны существовать в namespace UI. В Helm values допускаются их имена и пути монтирования; значения учётных данных туда не помещаются.

Используйте отдельные S3 credentials только для чтения. Разрешите list только для настроенных корней каталога и чтение ключей `runs/*/manifest.json`, `complete.json`, `deletion.json`, `recovery/BACKUP-RESTORE*.md`, `events/*/*.json` и `verification/*/*.json`. Запретите чтение архивов, запись и удаление объектов. Credentials монтируются из существующих Secret в `/s3/<name>`; у UI нет Kubernetes API-права чтения Secret. Credentials PostgreSQL и записи worker в UI не монтируются.

## Vault и сессии администратора

Используйте отдельный для установки KV v2 secret с `username` и `password`. В `config.vault.address` укажите HTTPS URL Vault, в `role` — роль Kubernetes auth, в `secretPath` — API data path KV v2, например `secret/data/pgbk/example/administrator`. `authMount` по умолчанию равен `kubernetes`; необязательный `caFile` указывает на смонтированный CA bundle. Роль связывается только с ServiceAccount и namespace UI. Её policy разрешает только `read` этого конкретного data path, без list и write. Настройте метод Kubernetes auth и его TokenReview-права через существующий административный процесс Vault.

Каждый защищённый запрос проверяет текущую версию secret в Vault. Credentials сравниваются в памяти и не сохраняются UI. Ротация отклоняет старые сессии; недоступный Vault отклоняет вход и чтение данных с `AUTHENTICATION_UNAVAILABLE`. Cookie имеет Secure, HttpOnly, SameSite=Strict, действует только на текущем хосте и по умолчанию истекает через час. CSRF token доступен только аутентифицированному same-origin клиенту; каждая браузерная запись также проверяет Origin. Попытки входа ограничиваются в SQLite по адресу клиента и глобально. Тела API-запросов, URL, cookie и credentials не журналируются. Решения аудита и имя аутентифицированного пользователя сохраняются в журнале.

## Включение разовых запусков

По умолчанию доступен только просмотр. Для запуска задайте `config.manualLaunch` и `admission.enabled` равными true и укажите `admission.caBundle`: base64 PEM CA, подписавшего сервисный сертификат UI. Chart отклоняет ручные запуски без admission. Установка validating webhook требует права управления cluster admission configuration. Webhook выбирает только identity ServiceAccount UI, поэтому его недоступность не блокирует CronJob controller и worker.

Identity UI может создавать Job только в backup namespace. Admission сверяет полный spec Job, image, аргументы, ServiceAccount, environment и volumes с текущим доверенным backup template. Также проверяются резервирование запроса в журнале, профиль, детерминированное имя и версия шаблона. Неизвестные изменения отклоняются; собственные mutating webhook, изменяющие worker template, нужно проверить до включения запуска. У UI нет прав записи CronJob, чтения Lease, Secret и логов Pod. Контракты описаны в документации [Kubernetes webhook](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/) и [Vault KV v2](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2).

При открытии подтверждения сервер выдаёт случайный записанный в журнал ключ запуска на 24 часа. Браузер хранит его в памяти для повторов. Атомарное резервирование предшествует запросу Kubernetes. Повтор проверяет identity Job либо возвращает сохранённый результат после удаления по TTL. Зарезервированный запрос без известного Job получает статус Outcome unknown и не создаёт новый Job автоматически. Перед намеренным открытием нового подтверждения изучите Jobs и события каталога. Недоступный журнал отключает запуск. Корректный приостановленный backup CronJob можно запустить вручную. Запуск не изменяет расписание.

## Хранение, метаданные и обновления

Используйте отдельный PVC и одну активную реплику. Deployment использует Recreate. Сохраняйте журнал PVC и делайте его резервные копии: в нём находятся серверные сессии, ключи запусков, результаты, аудит и восстанавливаемый индекс проецированных метаданных. Chart сохраняет PVC после удаления; повторная установка с тем же именем release использует его. Удаление UI оставляет pgbk release, расписания, исходные данные и S3-артефакты. Удалять сохранённый PVC следует только после завершения требований к повторам и истории по отдельному решению администратора.

Индекс обновляет до четырёх префиксов одновременно, по умолчанию каждые 30 секунд. Начальное неполное сканирование и ошибки отдельных префиксов видны со временем последнего успешного обновления. Прямое чтение копии и recovery notes повторно проверяет marker и deletion record. Просмотр страниц не выполняет GET или HEAD архивов. Проекция метаданных исключает неизвестные поля и скрывает вложенные внутренние адреса, ссылки на Secret и SQL; JSON выводится как безопасный текст с поиском ключей. Хеши артефактов подписаны SHA-256 from manifest. Recovery notes читаются по одной, проверяются размером и хешем и выводятся буквально в порядке автора. Для отсутствующих и повреждённых инструкций замены не генерируются.

Jobs независимо отображает Kubernetes execution, сохранённую фазу worker и restore verification. Корреляция требует namespace и UID Job в той же области profile/source. Ошибка API не доказывает удаление Job. Worker `ACCESS_DENIED` ведёт к инструкции по globals export; отказы сессии и API UI имеют отдельные коды. У исторических записей `TOOL_FAILED` первопричина остаётся неизвестной.

## Локальные проверки

```sh
uv sync --frozen --group integration
uv run --frozen pytest
uv run --frozen python scripts/check_docs.py
uv run --frozen --group integration python tests/kubernetes/ui_v1.py --helm helm --allow-amd64-emulation --serve
```

Kubernetes harness создаёт отдельный namespace и генерирует credentials вне репозитория. Он использует настоящие PostgreSQL 18, Kubernetes и Vault 1.18.0 с локальным TLS proxy, а также TLS Moto S3. Проверяются публикации по расписанию до/во время/после установки UI, ручной запуск paused-профиля, одновременные запросы, admission/RBAC, UID-корреляция, сохранённые инструкции, перезапуск, ротация credentials, удаление и переустановка. `--serve` оставляет HTTPS UI на localhost:18443 для браузерной проверки; прерывание процесса очищает созданные ресурсы. Флаг эмуляции предназначен только для локальных ARM Mac и явно снимает architecture node selector worker в тестовом стенде. Ограничения production worker не меняются. JSON-отчёт фиксирует ограничения: Moto не подтверждает cloud IAM, Vault dev mode является тестовым стендом, интеграции с ingress и облаком проверяются отдельно.

Для визуальной проверки в браузере, не доверяющем тестовому CA, `tests/ui_preview.py` предоставляет отдельный синтетический preview только для чтения на loopback-порту 18444. Он не обращается к Kubernetes/Vault/облаку, не содержит рабочих credentials и запрещает запись. Этот стенд не является production-режимом аутентификации. Он проверяет frontend независимо от реальных HTTPS API-проверок. Настройку доверия локальному сертификату в браузере выполняет оператор.
