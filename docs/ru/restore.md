# Полный цикл восстановления pgbk

Руководство для pgbk 0.3.1 и формата артефактов v1. Описывает действия оператора от выбора копии до проверки приложения. Команды — примеры для отдельного назначения; замените пути, имена баз, пользователей и версии своими. Для действующего источника эти команды не предназначены.

## 1. Выбор сценария и границы восстановления

| Копия → назначение | Порядок |
| --- | --- |
| Logical → локальный PostgreSQL | Проверить файлы → подготовить пустую базу → при необходимости восстановить роли → pg_restore → проверки. |
| Logical → managed PostgreSQL | Подготовить кластер/пользователей через провайдера → проверить расширения → переносимый pg_restore → назначить допустимые права → проверки. |
| Physical → локальный PostgreSQL/VM/контейнер | Подготовить совместимую среду → проверить и распаковать весь кластер → pg_verifybackup → заменить настройки → запустить изолированно → проверки. |
| Physical → managed PostgreSQL | Восстановить совместимый промежуточный сервер → pg_dump нужных баз → логическое восстановление в managed. Прямой импорт PGDATA обычно недоступен и pgbk его не реализует. |

«Локальный» здесь означает сервер под вашим управлением, в том числе VM, Docker и Kubernetes. Managed — сервис, где файловой системой PostgreSQL управляет провайдер. Источник logical может быть managed или обычным сервером; ограничения источника и назначения учитываются независимо.

Для `pgbk restore` и offline-helper нужна та же major-версия PostgreSQL, что в `manifest.json`; текущий диапазон pgbk — 14–18. Ручной логический перенос в более новую major возможен как отдельная миграция после проверки совместимости; понижение версии не поддерживается этим руководством. Утилита должна читать формат/сжатие архива. Физической копии нужны та же major, совместимые ОС/сборка/архитектура и бинарные файлы расширений. Linux amd64 PGDATA нельзя запустить нативным PostgreSQL на macOS/ARM: используйте совместимую Linux VM/контейнер, при необходимости с эмуляцией, или промежуточный логический экспорт.

Logical сохраняет только перечисленные базы и выбранный охват. Снимки разных баз не являются единым согласованным снимком. `filtered-database` не восстанавливает всю базу и может требовать внешних зависимостей. Physical восстанавливает весь кластер, включая базы, роли и хеши паролей. pgbk не предоставляет здесь непрерывное архивирование WAL/PITR. Конфигурацию сервиса, расширения, внешние сертификаты и интеграции подготавливает оператор.

## 2. Выбор копии, скачивание и проверка

Проверьте статус завершения копии и отчёт проверки восстановления. Успешное создание копии и успешный restore drill — разные результаты. Для полного восстановления logical заранее включается:

```yaml
profiles:
  example-logical:
    logical:
      databases: [application]
      globals: enabled
```

По умолчанию `globals: disabled`. Включённый режим сохраняет отдельный `globals.sql` через `pg_dumpall --globals-only --no-role-passwords`: роли, членство и глобальные объекты, включая tablespace, без паролей ролей. Нужны права на экспорт. Это не добавляет роли в архив каждой базы. Если globals отсутствуют в уже созданной копии, включение настройки задним числом её не изменит; роли потребуются из другого доверенного источника либо используется переносимый режим.

```sh
export PGBK_ARCHIVES=/srv/pgbk-restore/download
export PGBK_SOURCE=/opt/pgbk-source

pgbk --config values.local.yaml --namespace backups backup list --profile example-logical
pgbk --config values.local.yaml --namespace backups backup describe --profile example-logical --run-id RUN_ID
pgbk --config values.local.yaml --namespace backups backup download --profile example-logical --run-id RUN_ID --destination "$PGBK_ARCHIVES"
```

Для physical подставьте физический профиль. Каталог скачивания должен быть новым/пустым. Для полного logical-запуска не добавляйте `--database`: этот фильтр скачивает только архив одной базы, без globals. Для выборочного скачивания ориентируйтесь на `DOWNLOAD.json.downloadedFileIds`: полный манифест может перечислять и нескачанные файлы.

Скачивание содержит `manifest.json`, `DOWNLOAD.json`, архивы, краткие `RESTORE*.txt` и инструкции конкретной копии `BACKUP-RESTORE.md` / `BACKUP-RESTORE.ru.md`. Для новых копий последние сохраняются в S3 до публикации маркера завершения; их размеры/хеши записаны в `manifest.json.recoveryInstructions`. Они являются сопроводительными файлами в `recovery/`, отдельно от массива файлов PostgreSQL `artifacts`. При полном скачивании возвращаются сохранённые байты. Для старых копий или скачивания одной базы инструкции формируются из манифеста и фактически скачанных ID; происхождение и хеши результата записаны в `DOWNLOAD.json`. Манифест старой копии при этом не изменяется.

`pgbk backup download` проверяет хеши. После переноса файлов на другой компьютер повторите проверку потоково; следующий пример работает без S3 и Kubernetes:

```sh
python3 - "$PGBK_ARCHIVES" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
download_path = root / "DOWNLOAD.json"
download = json.loads(download_path.read_text()) if download_path.exists() else {}
assert download.get("runId", manifest["runId"]) == manifest["runId"]
artifacts = manifest["artifacts"]
ids = set(download.get("downloadedFileIds", [a["fileId"] for a in artifacts]))
assert ids and ids <= {a["fileId"] for a in artifacts}
records = [a for a in artifacts if a["fileId"] in ids]
records += download.get("recoveryInstructions", manifest.get("recoveryInstructions", []))
for item in records:
    name = item["filename"]
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name), name
    path = root / name
    assert path.is_file() and not path.is_symlink(), name
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    assert (digest.hexdigest(), path.stat().st_size) == (item["sha256"], item["size"]), name
    print("OK", name, item.get("database", item.get("scope", "instructions")))
print("runId:", manifest["runId"], "kind:", manifest["kind"], "major:", manifest["serverMajor"])
PY
```

Не используйте `python -O` для примеров с `assert`. Хеш подтверждает соответствие доверенному манифесту, а не подлинность независимо изменённого комплекта файлов. Если `DOWNLOAD.json` потерян, пример требует все файлы из манифеста. Сохраняйте исходный комплект неизменным, а логи и изменённые SQL — отдельно. После скачивания нативные инструменты работают без исходного сервера, S3 и Kubernetes. Для helper нужен установленный Python-пакет pgbk с зависимостями и `scripts/restore_download.py` из исходного пакета; одного wheel для наличия этого скрипта недостаточно.

## 3. Подготовка логического архива и назначения

В манифесте найдите артефакт по `database`, а не по предполагаемому имени файла. Имена архивов pgbk обычно непрозрачные ID. Проверьте `format`, `scope`, `selection`, `archiveInventory`, а также доступные `source.<database>.extensions` и `encodingLocale`. Неизвестные метаданные не означают отсутствие расширений. Установите нужные бинарные файлы расширений на обычном сервере; в managed проверьте доступность версий/способ включения у провайдера.

`postgres-custom` подаётся в pg_restore как файл. `postgres-directory-tar` — упаковка нативного directory-дампа, а не tar-format pg_dump; сначала распакуйте её в новый каталог. Не подавайте bundle напрямую в pg_restore. Например, после проверки внешнего хеша:

```sh
export PGBK_DUMP="$PGBK_ARCHIVES/ARCHIVE_FILE_FROM_MANIFEST"
export PGBK_LOGICAL_DIRECTORY=/srv/pgbk-restore/directory

python3 - "$PGBK_DUMP" "$PGBK_LOGICAL_DIRECTORY" <<'PY'
import re
import shutil
import sys
import tarfile
from pathlib import Path

destination = Path(sys.argv[2])
assert destination.is_absolute() and not destination.exists()
assert not any(p.is_symlink() for p in destination.parents)
destination.mkdir(mode=0o700)
seen = set()
with tarfile.open(sys.argv[1], "r:") as bundle:
    for member in bundle:
        assert member.isfile() and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", member.name)
        assert member.name.casefold() not in seen
        seen.add(member.name.casefold())
        with bundle.extractfile(member) as source, (destination / member.name).open("xb") as target:
            shutil.copyfileobj(source, target)
assert "toc.dat" in seen
PY

export PGBK_DUMP="$PGBK_LOGICAL_DIRECTORY"
pg_restore --list "$PGBK_DUMP"
```

Этот блок предназначен только для directory-bundle. Для custom достаточно задать `PGBK_DUMP` путём к файлу и выполнить `pg_restore --list`. При ошибке распаковки нужен другой новый каталог.

Для `filtered-database` до восстановления подготовьте отсутствующие схемы, типы, функции, расширения, FK-цели и родителей partitions согласно `selection.knownPrerequisites`; рекурсивный список зависимостей может быть неполным. Не создавайте сами восстанавливаемые таблицы. Предварительное создание расширения может создать объекты, уже присутствующие в TOC; согласуйте это с содержимым архива. Для нативного восстановления при необходимости составьте проверенный `pg_restore --list` / `--use-list` с исключением только уже подготовленных определений, сохраняя нужные данные и зависимости. В pgbk режим `destinationPolicy: prerequisites-only` разрешён только для filtered-архива.

## 4. Logical → локальный PostgreSQL

### 4.1. Переносимый режим без исходных ролей

Подходит для локальной проверки и назначения с другим набором пользователей. Пример предполагает существующего локального администратора `postgres`. Настройте password-файл заранее, с правами `0600`, форматом `host:port:database:user:password` и экранированием `:` и `\` в значениях. Пароли не передавайте в аргументах или connection URI.

```sh
export PGHOST=127.0.0.1
export PGPORT=5432
export PGUSER=postgres
export PGPASSFILE=/secure/restore.pgpass
export PGBK_DEST_DB=restored_application

psql -X --set=ON_ERROR_STOP=on --dbname=postgres --command='SELECT version(), current_user, inet_server_addr(), inet_server_port();'
createdb --template=template0 --owner="$PGUSER" "$PGBK_DEST_DB"
pg_restore --exit-on-error --no-owner --no-acl --no-tablespaces --dbname="$PGBK_DEST_DB" "$PGBK_DUMP"
```

При необходимости добавьте к `createdb` явные encoding/locale из источника, доступные на назначении. По умолчанию они наследуются от template0; это не гарантирует совпадение с источником. Не используйте существующую непустую базу. Флаги пропускают исходных владельцев, GRANT/REVOKE и размещение tablespace. Объекты принадлежат роли восстановления; роли в RLS-политиках, SQL-коде, SECURITY DEFINER-функциях и внешних подключениях всё равно требуют отдельного анализа. `--no-owner --no-acl` не переписывают такие ссылки.

### 4.2. Сохранение исходных ролей, владельцев и ACL

1. Убедитесь, что globals присутствуют и скачаны. Работайте в отдельном кластере с правами администратора.
2. Скопируйте `globals.sql` в рабочий файл. Сопоставьте уже существующие роли, включая bootstrap-администратора `postgres`; исключите конфликтующий CREATE ROLE и отдельно решите, какие ALTER ROLE допустимы. Роли managed-провайдера не переносите автоматически в локальные привилегированные роли.
3. Сохраните нужное членство/атрибуты пользовательских ролей. Для tablespace создайте отдельные каталоги назначения с правильным владельцем ОС и замените LOCATION в рабочем SQL. Если размещение переносить не нужно, исключите согласованные определения tablespace и используйте `--no-tablespaces` в pg_restore.
4. Выполните подготовленный SQL до дампа базы, останавливаясь при ошибке. Пароли задайте отдельно: новые роли из globals не смогут войти по паролю до его установки. Интерактивная `\password role_name` не помещает пароль в SQL-команду shell.

```sh
psql -X --set=ON_ERROR_STOP=on --dbname=postgres --file=/srv/pgbk-restore/globals.reviewed.sql
```

Для полной базы, исходное имя которой свободно в отдельном кластере, нативный `--create` восстанавливает также создание базы, её владельца, настройки и ACL базы:

```sh
pg_restore --exit-on-error --create --dbname=postgres "$PGBK_DUMP"
```

Здесь `--dbname=postgres` — только начальная служебная база; целевое имя берётся ИЗ АРХИВА. Команда не использует `PGBK_DEST_DB`. Не добавляйте `--clean` автоматически. Если исходное имя уже существует, относится к `postgres`/`template1`, нужно переименование или архив выборочный, заранее создайте отдельную пустую базу с нужным владельцем и используйте следующую команду; свойства и ACL самой базы, а также ALTER ROLE IN DATABASE перенесите отдельно по проверенному SQL:

```sh
pg_restore --exit-on-error --dbname="$PGBK_DEST_DB" "$PGBK_DUMP"
```

Повторите для каждой сохранённой базы. Исходные пароли, внешняя аутентификация, сертификаты и инфраструктурные настройки логическим бэкапом pgbk не восстанавливаются.

## 5. Logical → managed PostgreSQL

1. Создайте новый кластер/базу через поддерживаемый интерфейс провайдера. Подготовьте сетевой доступ, CA, endpoint записи, пользователя восстановления с нужными правами и пользователя приложения. У pgbk нет универсального API создания managed-кластера.
2. Выберите ту же major для восстановления средствами pgbk. Проверьте encoding/locale, расширения и их версии, лимиты подключений/хранилища, доступные языки и настройки. Зависимости расширений, требующие preload, включаются механизмом провайдера до восстановления.
3. Сопоставьте пользовательские роли источника с допустимыми ролями назначения. Не выполняйте globals целиком: SUPERUSER/REPLICATION/BYPASSRLS, служебные роли, tablespace и некоторые глобальные GRANT могут быть недоступны. Создавайте пользователей через console/API/SQL согласно правилам сервиса. Пароли задаются отдельно.
4. Подключитесь с проверкой TLS и восстановите в подготовленную пустую базу в переносимом режиме:

```sh
export PGHOST=DESTINATION_WRITER_FQDN
export PGPORT=6432
export PGUSER=restore_owner
export PGSSLMODE=verify-full
export PGSSLROOTCERT=/secure/destination-ca.crt
export PGPASSFILE=/secure/managed-restore.pgpass
export PGBK_DEST_DB=restored_application

psql -X --set=ON_ERROR_STOP=on --dbname="$PGBK_DEST_DB" --command='SELECT version(), current_user, current_database();'
pg_restore --exit-on-error --no-owner --no-acl --no-tablespaces --dbname="$PGBK_DEST_DB" "$PGBK_DUMP"
```

Порт `6432` — пример; используйте документированный endpoint/порт своего сервиса, предпочтительно прямой endpoint для административных операций. Если провайдер создаёт расширения или схемы заранее, согласуйте TOC по разделу 3 вместо игнорирования всех ошибок. Для недоступного расширения/языка/функции может потребоваться адаптация схемы; успешный импорт неподдерживаемых объектов не обещается.

5. Назначьте прикладные права явно. Следующий пример предполагает существующую роль `app_login`, схему `public` и выполнение от владельца восстановленных объектов. Для других схем/владельцев повторите с соответствующими именами. Default privileges относятся только к будущим объектам создающей роли; при другом владельце миграций настройте их для него отдельно.

```sh
psql -X --set=ON_ERROR_STOP=on --dbname="$PGBK_DEST_DB" --set=target_db="$PGBK_DEST_DB" <<'SQL'
GRANT CONNECT ON DATABASE :"target_db" TO app_login;
GRANT USAGE ON SCHEMA public TO app_login;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_login;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_login;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_login;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_login;
SQL
```

Это пример модели доступа, а не копирование исходных ACL. Права EXECUTE на функции, доступ к большим объектам, RLS и другие схемы согласуются отдельно. Если требуется сохранить исходные ACL, заранее создайте все разрешённые роли и используйте проверенный нативный restore с нужным набором флагов; полный набор исходных привилегий в managed может быть недостижим. `preserveOwnership` pgbk одновременно сохраняет требования владельцев, ACL и tablespace; для независимого выбора этих параметров используйте pg_restore.

Для Yandex Managed Service for PostgreSQL используйте [официальный порядок логической миграции](https://yandex.cloud/en/docs/managed-postgresql/tutorials/data-migration). Его штатные бэкапы и API восстановления — отдельный механизм, описанный в [управлении резервными копиями](https://yandex.cloud/en/docs/managed-postgresql/operations/cluster-backups); файлы pgbk нельзя выдавать за backup ID провайдера. Живой managed-сценарий не сертифицирован текущими локальными отчётами pgbk; проверьте конкретный сервис и ограничения на дату восстановления.

## 6. Логическое восстановление средствами pgbk

Предыдущие нативные команды не требуют Kubernetes после скачивания. Команда pgbk работает с каталогом S3 и координацией Kubernetes:

```sh
pgbk --config values.local.yaml --namespace backups restore logical --profile example-logical --run-id RUN_ID --database application --destination-config /secure/destination.json
```

`--database` здесь — ИСХОДНАЯ база из манифеста. Поле `database` в destination JSON — новая целевая база. Подготовьте её заранее. Пример JSON для изолированного локального TCP-сервера:

```json
{
  "host": "127.0.0.1",
  "port": 5432,
  "serverMajor": 18,
  "database": "restored_application",
  "credentialsSecretRef": "restore-postgres",
  "tls": {"mode": "disable"},
  "acknowledgeIsolated": true,
  "destinationPolicy": "empty",
  "preserveOwnership": false
}
```

Для managed замените host/port и TLS на `{"mode":"verify-full","caSecretRef":"restore-ca"}`. Восстановление выполняется от пользователя в Secret; JSON не содержит пароль. `preserveOwnership: true` допустим после подготовки нужных ролей/ACL/tablespace. Globals никогда не выполняются автоматически. Совпадение источника и назначения по endpoint/IP или обнаруженному system identifier отклоняется. Подтверждение `acknowledgeIsolated` означает реальную изоляцию назначения, а не обход проверок.

Offline-helper из исходного пакета использует тот же алгоритм, включая проверку хешей выбранной базы, без обращения к S3/Kubernetes. Directory-архив он распаковывает сам. Каталогу archives нужно разрешение записи для временных файлов логического восстановления. Учётные данные читаются из локальной проекции: файлы `username` и `password` в `$PGBK_SECRET_ROOT/restore-postgres/`, CA — `$PGBK_SECRET_ROOT/restore-ca/ca.crt`. Подготовьте эти файлы с ограниченным доступом, без сохранения в Git.

```sh
export PGBK_SECRET_ROOT=/secure/pgbk-secrets
python3 "$PGBK_SOURCE/scripts/restore_download.py" --archives "$PGBK_ARCHIVES" logical --database application --destination-config /secure/destination.json
```

Job-вариант (`--as-job`) требует заранее созданных namespace, Secret, ресурсов и scratch; `127.0.0.1` внутри Job означает сам Pod. Используйте достижимый из Pod изолированный endpoint. Детали — в [эксплуатации](operations.md).

## 7. Physical → локальный PostgreSQL, VM или контейнер

### 7.1. Подготовка и распаковка

1. По манифесту проверьте major, architecture, system identifier, timeline и `source.tablespaceOids`. Скачайте ВСЕ `base.tar`, `pg_wal.tar`, `OID.tar` и `backup_manifest` и проверьте их хеши. Это отдельный полный кластер, не архив для pg_restore.
2. Подготовьте совместимую среду с PostgreSQL и `pg_verifybackup`/`pg_waldump` нужной major, библиотеками расширений, locale и необходимыми preload-библиотеками. Для физических копий нет межархитектурного переноса; helper также проверяет architecture.
3. Остановите только выделенное назначение; убедитесь, что его каталоги не используются другим сервером. Нужны новые пустые абсолютные PGDATA и отдельные непересекающиеся каталоги для КАЖДОГО tablespace. Ссылки в родителях путей не разрешены. Не выполняйте initdb. Обеспечьте место для архивов, распаковки, WAL и дальнейшей работы.
4. Запускайте helper от выбранного непривилегированного пользователя ОС, который будет запускать PostgreSQL и владеет каталогами. Пример без tablespace:

```sh
export PGBK_PGDATA=/srv/pgbk-restore/pgdata
export PGBK_SOCKET=/srv/pgbk-restore/socket

python3 "$PGBK_SOURCE/scripts/restore_download.py" --archives "$PGBK_ARCHIVES" physical --pgdata "$PGBK_PGDATA" --major 18 --tablespace-map '{}' --acknowledge-isolated-stopped
```

Если в копии есть, например, OID `16384`, замените пустую карту на карту всех OID. Пути в ней — новые пути назначения; исходные LOCATION не используются:

```sh
python3 "$PGBK_SOURCE/scripts/restore_download.py" --archives "$PGBK_ARCHIVES" physical --pgdata "$PGBK_PGDATA" --major 18 --tablespace-map '{"16384":"/srv/pgbk-restore/tablespace-16384"}' --acknowledge-isolated-stopped
```

Выберите ОДНУ команду, соответствующую копии. Helper проверяет внешние хеши, безопасно распаковывает данные и WAL, пересоздаёт только сопоставленные ссылки `pg_tblspc/OID`, размещает `backup_manifest`, выполняет `pg_verifybackup` и затем заменяет конфигурацию источника. При ошибке не запускайте результат и используйте заново подготовленные пустые каталоги для повтора.

При ручной распаковке без helper сохраняйте тот же порядок: проверка внешних хешей → безопасная распаковка base → WAL в PGDATA/pg_wal → отдельные каталоги tablespace и явные ссылки → upstream backup_manifest → владелец/режим 0700 → pg_verifybackup → замена настроек. Нельзя разрешать произвольные tar-ссылки/выход за каталог; `tar -xf` без проверки недостаточен. Не изменяйте файлы данных и `backup_label`; не удаляйте требуемые WAL. Не используйте `--no-parse-wal` для обхода ошибки отсутствующего WAL. Проверять нужно ДО изменения конфигурации и первого запуска: после запуска исходный backup_manifest уже не описывает рабочее состояние.

### 7.2. Настройки и первый запуск

Результат helper `status: prepared` означает подготовленные файлы, а не работающую базу. pgbk удаляет исходные recovery/standby signal-файлы, старые PID/options, заменяет postgresql.conf, postgresql.auto.conf, pg_hba.conf и pg_ident.conf; отключает source archive/restore commands и primary_conninfo/slot. Пересозданные настройки минимальны: перенесите необходимые локальные настройки совместимости и `shared_preload_libraries` осознанно. Не возвращайте конфигурацию исходной репликации, внешние subscriptions/FDW/user mappings или задания планировщиков без проверки; изолируйте сеть промежуточного сервера.

Созданный HBA содержит `local all all trust`: он предназначен для отдельного пользователя ОС и приватного сокета в изолированной среде. До открытия сети настройте рабочую аутентификацию, TLS, HBA и секреты. Для первой проверки запретите TCP и используйте приватный каталог сокета, все команды — от того же непривилегированного пользователя ОС:

```sh
chmod 700 "$PGBK_PGDATA"
mkdir -m 700 "$PGBK_SOCKET"
pg_ctl -D "$PGBK_PGDATA" -l /srv/pgbk-restore/server.log -o "-c listen_addresses='' -c unix_socket_directories=$PGBK_SOCKET -p 55432" -w start

export PGHOST="$PGBK_SOCKET"
export PGPORT=55432
export PGUSER=postgres
psql -X --set=ON_ERROR_STOP=on --dbname=postgres --command='SELECT version(), pg_is_in_recovery();'
```

Пути примера не содержат пробелов. Роль `postgres` должна существовать в ИСХОДНОМ кластере; иначе используйте известного исходного администратора. Physical уже содержит роли и хеши паролей: globals повторно не применяются. `POSTGRES_USER`/`POSTGRES_PASSWORD` контейнера не пересоздают роли существующего PGDATA. Дождитесь окончания восстановления WAL и готовности сервера; если standalone-проверка остаётся в recovery или требует отсутствующий WAL, разберите журнал и комплект копии, не включайте репликацию на источник.

Для контейнера используйте совместимые Linux/architecture и образ с нужными расширениями. Смонтируйте PGDATA и все tablespace в те же пути, что указаны в карте; UID/GID должны совпадать с владельцами файлов. Запускайте существующий PGDATA как PostgreSQL, без автоматической инициализации. Не публикуйте порты до проверки конфигурации. На Apple Silicon для Linux amd64 потребуется подходящая VM/эмуляция; наличие Docker само по себе не делает PGDATA совместимым с ARM PostgreSQL.

После проверки остановить выделенное назначение можно так:

```sh
pg_ctl -D "$PGBK_PGDATA" -m fast -w stop
```

Каталог prepared можно также получить через `pgbk restore physical`; команда не запускает сервер. Для Job и PVC см. [операционные требования](operations.md).

## 8. Physical → managed через промежуточный сервер

Прямой импорт физических файлов pgbk в managed не реализован. Даже если провайдер использует WAL-G для своих копий, формат pgbk `st put/get` не является его репозиторием backup-push; `wal-g backup-fetch` здесь не подходит. Для Yandex managed pgbk также отклоняет физический бэкап источника.

1. Полностью выполните раздел 7 на совместимом промежуточном сервере и проверьте данные по разделу 9. Не подключайте к нему рабочее приложение и не меняйте данные между экспортами, если нужна согласованность нескольких баз.
2. От исходного администратора выгрузите каждую нужную базу в custom-format. При необходимости отдельно выгрузите роли без паролей как материал для сопоставления с ролями managed:

```sh
pg_dump --format=custom --file=/srv/pgbk-restore/application.dump --dbname=application
pg_dumpall --globals-only --no-role-passwords --file=/srv/pgbk-restore/staging-globals.sql
```

3. Запишите размеры/хеши новых файлов, имя базы, версии и источник экспорта. Это НОВЫЕ логические дампы, не файлы исходного physical manifest; не подменяйте ими артефакты в исходном комплекте.
4. Перенесите файлы на машину с доступом к managed, подготовьте кластер/расширения/роли и выполните раздел 5. `staging-globals.sql` не исполняется целиком в managed. Если экспортированы несколько баз, восстановите каждую и учтите межбазовые внешние зависимости.
5. После функциональных проверок переключите приложение на новое назначение отдельным операционным действием. До этого сохраните исходную копию и промежуточный сервер для повторного экспорта.

## 9. Проверка результата и ввод в работу

Завершение без ошибок — необходимое, но недостаточное условие. Сохраните команду, версии утилит, run ID, время и полный лог с ограниченным доступом. В каждой восстановленной базе выполните анализ и просмотр каталогов:

```sh
vacuumdb --analyze-in-stages --dbname="$PGBK_DEST_DB"
psql -X --set=ON_ERROR_STOP=on --dbname="$PGBK_DEST_DB" <<'SQL'
SELECT current_database(), current_user, version();
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT n.nspname, c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT i.indisvalid OR NOT i.indisready;
SELECT conrelid::regclass, conname FROM pg_constraint WHERE NOT convalidated;
\dn+
\dp
\ddp
SQL
```

Для physical задайте `PGBK_DEST_DB` нужной восстановленной базой и повторите для остальных. Сопоставьте ожидаемые таблицы/partition/индексы/ограничения, большие объекты, владельцев и ACL с охватом копии и исходными контрольными данными. Не все NOT VALID-ограничения означают ошибку восстановления: сравнивайте с исходным состоянием. Проверьте критичные количества строк и бизнес-агрегаты. Для sequences сравните `last_value`/`is_called`, параметры increment/cycle и максимум ключа; не вызывайте `nextval` только ради проверки. Исправление sequence требует отдельного согласованного изменения с учётом приложения.

Проверьте вход именно ролью приложения, чтение и тестовую запись в допустимом тестовом сценарии. Учтите RLS, права на функции/sequences/large objects и владельцев SECURITY DEFINER-функций. Согласуйте новую аутентификацию, сертификаты, задания, FDW и внешние интеграции, прежде чем открыть сеть и переключить строку подключения. Сохраните прежнее подключение/план возврата до завершения проверки; pgbk не переключает приложение автоматически.

## 10. Типичные ошибки и повтор

| Сообщение/ситуация | Действие |
| --- | --- |
| `creating ACL ... role ... does not exist` | Исходной роли нет. Для переносимой копии используйте `--no-owner --no-acl`; для исходных ACL сначала подготовьте роли. Ошибки только ACL не доказывают полноту остальных данных. |
| Роль отсутствует при CREATE POLICY или в функции | Переносимые флаги не переписывают RLS/код. Подготовьте допустимую роль либо согласованное изменение определения. |
| `role already exists` в globals | Адаптируйте рабочую копию SQL с учётом существующих ролей; не игнорируйте все ошибки файла. |
| `relation already exists`, duplicate key, `RESTORE_PARTIAL` | Назначение может быть частичным. Сохраните лог и повторяйте в новом пустом назначении. Полный повтор поверх частичных данных не является продолжением. |
| Недоступное расширение/привилегия в managed | Проверьте возможности провайдера; подготовьте расширение/TOC/схему. Полный перенос может быть невозможен без изменения приложения. |
| `RESTORE_MAJOR_MISMATCH`, `CLIENT_MAJOR_MISMATCH` | Используйте предусмотренную major утилит/назначения. Миграцию версии проводите отдельно. |
| `RESTORE_ARCHITECTURE_MISMATCH` | Физическое восстановление выполните в совместимой среде; для другой архитектуры используйте логический экспорт. |
| Хеш, backup_manifest или WAL не проходят проверку | Остановитесь до запуска; проверьте комплект и повторно скачайте файлы. Не обходите проверку и не удаляйте backup_label. |
| `RESTORE_SOURCE_REFUSED`, `DESTINATION_NOT_EMPTY` | Подготовьте действительно отдельное пустое назначение. Не отключайте изоляцию ради повтора. |

## 11. Дополнительные материалы

- [Эксплуатация, Job, PVC, Secret и восстановление](operations.md).
- [Конфигурация](configuration.md), [охват логических архивов](logical-modes.md), [проверка копий](verification.md), [границы подтверждённой совместимости](compatibility.md).
- PostgreSQL: [pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html), [pg_dumpall](https://www.postgresql.org/docs/18/app-pg-dumpall.html), [SQL dump](https://www.postgresql.org/docs/18/backup-dump.html), [pg_verifybackup](https://www.postgresql.org/docs/18/app-pgverifybackup.html). Для другой major выберите соответствующую версию документации.
