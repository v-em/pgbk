# Режимы logical backup — pgbk 0.2.0

pgbk — PostgreSQL backups for Kubernetes. Адаптеры PostgreSQL и S3 остаются независимыми от провайдера. [Пример двух источников для Yandex](../../examples/yandex/two-sources.yaml) содержит отдельные targets Managed PostgreSQL и PostgreSQL в Kubernetes. Оба используют logical backup. Замените условные имена хостов/баз, ссылки Secret, digest образа и StorageClass. Managed endpoint должен принимать подключения pg_dump; Kubernetes Service — вести к нужному PostgreSQL. Требования к существующим Namespace, ServiceAccount, RBAC и Lease сохраняются.

Профиль managed использует direct S3, профиль Kubernetes — PVC и параллельный дамп. [Override managed через PVC](../../examples/yandex/managed-pvc-override.yaml) и [override Kubernetes напрямую](../../examples/yandex/kubernetes-direct-override.yaml) меняют режим того же профиля, сохраняя идентичность каталога. После объединения overlays проверьте итоговую конфигурацию до установки. Примеры объявляют PostgreSQL 17; образ должен соответствовать реальной основной версии каждого сервера.

## Прямой поток и параллельный дамп через scratch

```yaml
logical:
  databases: [application]
  format: custom
  jobs: 1
  compression: {method: gzip, level: 6}
  transfer: {mode: direct, partSizeMiB: 16}
scratch: {type: emptyDir, size: 256Mi}
```

```yaml
logical:
  databases: [application]
  format: directory
  jobs: 4
  compression: {method: zstd, level: 3}
  transfer: {mode: staged}
scratch:
  type: pvc
  size: 100Gi
  storageClassName: REPLACE_WITH_STORAGE_CLASS
```

`direct` допускает только `custom` и `jobs: 1`. Stdout pg_dump последовательно передаётся через multipart API S3 SDK. Полный дамп в scratch не записывается, включая необязательные globals. Небольшой emptyDir нужен для password-файлов и ограниченных метаданных утилит. Память расходуется на multipart-буферы (несколько размеров partSizeMiB), клиенты PostgreSQL и runtime; не выбирайте размер части близким к лимиту памяти контейнера. partSizeMiB по умолчанию 16, допустимо 5–512, максимум 10000 частей. Для больших дампов увеличьте размер части. Используется один процесс дампа и одна последовательная загрузка, без параллельных upload.

Каждая часть повторно отправляется теми же байтами. Завершение multipart допускается только после успешного выхода producer; ошибка pg_dump, deadline, отмена или исчерпание повторов приводят к попытке abort. Новый запуск начинает дамп заново: продолжить PostgreSQL snapshot из оборванного pipe невозможно. Настройте внешнее lifecycle-удаление незавершённых multipart после потери узла. Marker завершения публикуется после проверки TOC/выборки удалённого архива и полного SHA-256 объекта. Прямой поток использует SDK; staged-файлы по-прежнему передаются raw-командами WAL-G. Автоматического перехода на staging нет.

`staged` сначала записывает custom или directory в scratch. jobs по умолчанию 1, допустимо 1–64; значения больше 1 требуют directory. pg_dump открывает jobs + 1 подключений, дополнительно к сессиям preflight/выборки. Directory-режим параллельно выгружает таблицы, затем упаковывает сжатые файлы в tar без дополнительного сжатия. Scratch должен вместить directory и tar (примерно удвоенный размер сжатого архива с запасом). Доступны PVC, разрешённый existingClaim и emptyDir для небольших staged-копий. Пример PVC использует generic ephemeral volume, принадлежащий Pod; это не PVC исходной базы.

## Сжатие и совместимость артефактов

Метод/уровень относятся к сжатию архива и задаются отдельно от режима передачи и jobs. По умолчанию gzip level 6. PostgreSQL 14–15 допускают none/gzip; PostgreSQL 16–18 также lz4/zstd, если клиент собран с ними. Для gzip допустимы уровни 1–9, zstd 1–22; для none/lz4 поле level не задаётся. Без уровня gzip/zstd используется нативный default. Недоступный кодек завершает запуск ошибкой, без тихой замены алгоритма. Внешний tar не добавляет второй слой сжатия.

Custom-артефакт сохраняет формат postgres-custom. Directory-артефакт имеет формат postgres-directory-tar: tar из файлов нативного directory-дампа, включая toc.dat. pgbk 0.2 понимает оба внутри manifest v1; старые readers отклоняют новый формат. Restore/drill безопасно распаковывают directory-bundle в отдельное scratch и вызывают pg_restore. Download содержит нативные инструкции на двух языках. Доверенный архив можно распаковать и восстановить pg_restore из каталога без Kubernetes, S3 и сервиса приложения. Совместимые утилиты PostgreSQL и зависимости расширений по-прежнему нужны. При восстановлении требуется место для скачанного tar и распакованного directory.

Physical-full сохраняет staged-хранение; новые настройки относятся только к logical-профилям. Инкременты, PITR и UI в этой версии не реализованы.

## Проверка примеров

```sh
uv sync --frozen
uv run --frozen pgbk --config examples/yandex/two-sources.yaml config validate
helm lint charts/pgbk -f examples/yandex/two-sources.yaml
helm template pgbk charts/pgbk -n backups -f examples/yandex/two-sources.yaml
```

```sh
helm template pgbk charts/pgbk -n backups \
  -f examples/yandex/two-sources.yaml \
  -f examples/yandex/managed-pvc-override.yaml
```

## Разработка через uv

Используйте uv 0.9.5 (закреплён в CI/Docker) либо совместимый более новый uv; устанавливайте из официального дистрибутива. Основной lockfile — uv.lock; runtime constraints сохраняют ранее закреплённые зависимости. uv sync --frozen устанавливает runtime и unit-тесты; для Moto добавьте --group integration. Указывайте ту же группу в последующих uv run, пока работает эмулятор, иначе синхронизация удалит его зависимости. uv build собирает CLI; make package также упаковывает Helm. Worker собирается через uv sync --frozen --no-dev; runtime ничего не устанавливает. requirements.lock сохранён как перечень Linux runtime для существующих evidence/SBOM-скриптов.
