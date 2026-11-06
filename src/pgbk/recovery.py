"""Render small, source-independent recovery notes from frozen backup metadata."""
import json
import re
import shlex


NOTE_FILENAMES = ("BACKUP-RESTORE.md", "BACKUP-RESTORE.ru.md")


def literal(value):
    # Metadata is data, including database names containing Markdown delimiters.
    return json.dumps(value, ensure_ascii=False).replace("`", "\\u0060").replace("<", "\\u003c")


def code(command):
    fence = "~" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"~+", command)), default=0))
    return f"\n{fence}sh\n{command}\n{fence}\n"


def recovery_notes(manifest, downloaded_ids=None):
    """Return bilingual notes; a subset download must not promise absent globals."""
    items = manifest["artifacts"]
    available = {a["fileId"] for a in items} if downloaded_ids is None else set(downloaded_ids)
    result = {}
    for ru, filename in [(False, NOTE_FILENAMES[0]), (True, NOTE_FILENAMES[1])]:
        def tr(en, translated):
            return translated if ru else en
        lines = [tr("# Recovery of this backup", "# Восстановление этой копии"), "",
                 tr("Run", "Запуск") + ": " + literal(manifest["runId"]),
                 tr("Profile", "Профиль") + ": " + literal(manifest.get("profileId")),
                 tr("Kind", "Тип") + ": " + literal(manifest["kind"]),
                 "PostgreSQL major: " + str(manifest["serverMajor"]),
                 tr("Completed", "Завершён") + ": " + literal(manifest.get("completedAt")), "",
                 tr("Run commands from the download directory. Set destination connection variables and PGPASSFILE first; never use the source endpoint.",
                    "Выполняйте команды из каталога скачивания. Сначала задайте параметры подключения к назначению и PGPASSFILE; не используйте endpoint источника."),
                 tr("Verify sizes and SHA-256 against manifest.json and the downloaded IDs in DOWNLOAD.json before restoring. The full procedure is in docs/en/restore.md (wheel: share/doc/pgbk/en/restore.md).",
                    "До восстановления проверьте размеры и SHA-256 по manifest.json и скачанные ID в DOWNLOAD.json. Полный порядок: docs/ru/restore.md (wheel: share/doc/pgbk/ru/restore.md)."), ""]
        if manifest["kind"] == "logical":
            globals_items = [a for a in items if a["format"] == "postgres-globals-sql"]
            lines.append(tr("## Roles and permissions", "## Роли и права"))
            if not globals_items:
                lines.append(tr("This backup has no globals.sql. Source roles and their memberships cannot be reconstructed from the database archive alone. Use portable restore or provision roles separately.",
                                "В этой копии нет globals.sql. Определения ролей и членство нельзя восстановить из одного архива базы. Используйте переносимый режим либо подготовьте роли отдельно."))
            else:
                for item in globals_items:
                    present = item["fileId"] in available
                    lines.append(literal(item["filename"]) + ": " + tr("downloaded" if present else "NOT downloaded; download the run WITHOUT --database",
                                      "скачан" if present else "НЕ скачан; скачайте запуск БЕЗ --database"))
                lines.append(tr("For source ownership/ACLs, review globals SQL (existing/provider roles, privileges, tablespace paths), apply an adapted copy first, then restore databases without --no-owner/--no-acl. Role passwords were NOT backed up; set them separately. Managed services require provider-supported roles and privileges.",
                                "Для исходных владельцев/ACL сначала проверьте globals SQL (существующие/служебные роли, привилегии, пути tablespace), примените адаптированную копию, затем восстановите базы без --no-owner/--no-acl. Пароли ролей НЕ сохранены; задайте их отдельно. В managed используйте только поддерживаемые провайдером роли и права."))
            for item in items:
                if not item.get("database") or item["fileId"] not in available:
                    continue
                lines += ["", tr("## Database", "## База") + ": " + literal(item["database"]),
                          tr("Archive", "Архив") + ": " + literal(item["filename"]),
                          tr("Format / scope", "Формат / охват") + ": " + literal(item["format"]) + " / " + literal(item["scope"])]
                if item["scope"] == "filtered-database":
                    lines.append(tr("FILTERED backup: it is not a full database copy. Inspect this artifact's selection, effective scope and knownPrerequisites in manifest.json; prepare missing schemas/types/extensions/functions/FK targets/partition parents before restore.",
                                    "ВЫБОРОЧНАЯ копия: это не вся база. Проверьте selection, фактический охват и knownPrerequisites этого артефакта в manifest.json; заранее подготовьте недостающие схемы/типы/расширения/функции/FK-цели/родителей секций."))
                if item["format"] == "postgres-directory-tar":
                    lines.append(tr("Safely unpack this flat directory bundle into a new directory, rejecting links and unsafe/duplicate paths (full guide, section 3). Pass that directory as ARCHIVE; do not pass the bundle tar directly to pg_restore.",
                                    "Безопасно распакуйте directory-bundle в новый каталог, отклоняя ссылки и опасные/повторные пути (полное руководство, раздел 3). Передайте каталог как ARCHIVE; сам tar-bundle не подаётся в pg_restore."))
                    archive = '"$PGBK_LOGICAL_DIRECTORY"'
                else:
                    archive = shlex.quote("./" + item["filename"])
                lines.append(tr("Portable local/managed restore: create a NEW empty destination database with the required encoding/locale, extensions and prerequisites. Choose a different unused destination name for each source database.",
                                "Переносимое восстановление локально/в managed: создайте НОВУЮ пустую целевую базу с нужными encoding/locale, расширениями и зависимостями. Для каждой исходной базы выберите отдельное свободное имя назначения."))
                lines.append(code('pg_restore --exit-on-error --no-owner --no-acl --no-tablespaces '
                                  '--dbname="$PGBK_DEST_DB" ' + archive))
                lines.append(tr("The connecting role owns restored objects. Grant application access explicitly. With pgbk restore logical, use this source database name for --database and a separate destination.json; preserveOwnership defaults to false.",
                                "Восстановленные объекты принадлежат роли подключения. Явно выдайте права приложению. В pgbk restore logical укажите это исходное имя базы в --database и отдельный destination.json; preserveOwnership по умолчанию false."))
        else:
            lines += [tr("Architecture", "Архитектура") + ": " + literal(manifest.get("architecture")),
                      tr("## Physical recovery", "## Физическое восстановление")]
            for item in items:
                lines.append("- " + literal(item["filename"]) + ": " + str(item["size"]) + " bytes; SHA-256 " + item["sha256"])
            mappings = {oid: "/srv/pgbk-restore/tablespace-" + oid for oid in manifest["source"].get("tablespaceOids", [])}
            lines.append(tr("Use a stopped isolated destination with the SAME PostgreSQL major, compatible OS/build and architecture, plus extension binaries. On macOS/ARM, use a compatible Linux VM/container when the source is Linux/amd64. Do not initdb the destination.",
                            "Используйте остановленное изолированное назначение с ТОЙ ЖЕ major PostgreSQL, совместимыми ОС/сборкой и архитектурой, с бинарными файлами расширений. На macOS/ARM для Linux/amd64-источника нужна совместимая Linux VM/контейнер. Не выполняйте initdb в назначении."))
            lines.append(tr("Example for the source distribution helper; replace /opt/pgbk-source and every destination path with isolated empty directories. Install the matching pgbk runtime and PostgreSQL tools in this environment first.",
                            "Пример для helper из исходного пакета; замените /opt/pgbk-source и все пути назначения изолированными пустыми каталогами. В этой среде заранее установите pgbk и утилиты нужной версии PostgreSQL."))
            command = ('python3 /opt/pgbk-source/scripts/restore_download.py --archives "$PWD" physical '
                       '--pgdata /srv/pgbk-restore/pgdata --major ' + str(manifest["serverMajor"]) +
                       ' --tablespace-map ' + shlex.quote(json.dumps(mappings)) + ' --acknowledge-isolated-stopped')
            lines.append(code(command))
            lines.append(tr("The helper verifies hashes, safely extracts data/WAL/tablespaces, runs pg_verifybackup, sanitizes source configuration and returns prepared; it does NOT start PostgreSQL. Set non-root ownership and mode 0700, review all configuration and start with a private Unix socket. Its generated local trust HBA is for an isolated recovery environment only. Physical copies include roles and password hashes; globals.sql is unnecessary.",
                            "Helper проверяет хеши, безопасно распаковывает данные/WAL/tablespace, выполняет pg_verifybackup, заменяет конфигурацию источника и возвращает prepared; PostgreSQL он НЕ запускает. Задайте непривилегированного владельца и режим 0700, проверьте настройки и запускайте с приватным Unix-сокетом. Созданный local trust в HBA допустим только в изолированной среде восстановления. Физическая копия включает роли и хеши паролей; globals.sql не нужен."))
            lines.append(tr("Managed destination: pgbk physical files cannot be installed directly into a managed PGDATA. Recover on a compatible staging server, verify it, pg_dump each required database, then perform a logical managed restore. Provider-native backup restore is a different mechanism; these are not WAL-G backup-push artifacts.",
                            "Назначение managed: физические файлы pgbk нельзя напрямую установить в managed PGDATA. Восстановите совместимый промежуточный сервер, проверьте его, выгрузите нужные базы через pg_dump и выполните логическое восстановление в managed. Восстановление штатной копии провайдера — другой механизм; это не артефакты WAL-G backup-push."))
        lines += ["", tr("## Completion", "## Завершение"),
                  tr("Require zero restore errors, run ANALYZE, verify critical row counts, indexes/constraints, sequences, extensions, owners and application-role access. Test application reads/writes before changing its connection. After a failed restore, use a fresh empty destination; do not blindly retry over partial data.",
                     "Требуйте завершения без ошибок, выполните ANALYZE, проверьте критичные количества строк, индексы/ограничения, sequences, расширения, владельцев и доступ роли приложения. До переключения подключения проверьте чтение/запись приложения. После ошибки используйте новое пустое назначение; не повторяйте восстановление поверх частичных данных."), ""]
        result[filename] = "\n\n".join(lines).encode("utf-8")
    return result
