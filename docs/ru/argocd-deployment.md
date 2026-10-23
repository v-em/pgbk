# Развертывание через Argo CD

Инструкция для установки `pgbk` в Kubernetes из Git-репозитория с помощью Argo CD. Она предполагает, что PostgreSQL и S3-совместимый bucket уже существуют, а worker-образ собирается вручную и публикуется в собственном registry. Решение не создаёт PostgreSQL, bucket, namespace или Secret с учётными данными.

## Что будет установлено

Helm chart находится в `charts/pgbk`. Он создаёт ConfigMap конфигурации и CronJob профилей; для включённых операций — также CronJob retention, verification, download или restore. Сам backup worker запускается как Job по расписанию или вручную. Образы должны быть заранее собраны и доступны всем узлам Kubernetes.

Начните с одного профиля `logical`, расписание резервирования оставьте выключенным до завершения проверки. Если нужен `physical-full`, отдельно проверьте прямой replication endpoint и требования из [эксплуатации](operations.md). Chart по умолчанию не создаёт ServiceAccount, RBAC или Lease. Установка предполагает, что эти объекты и права заранее подготовлены администратором.

## 1. Соберите и опубликуйте worker

Нужен Docker или Podman/Buildah с поддержкой многоэтапной сборки, доступ к registry и соответствующий PostgreSQL base image из `docs/compatibility.json`. Сборщик использует `images/Dockerfile`, платформу `linux/amd64` и тег `:17-test` в качестве локального промежуточного тега:

Для Podman используйте `python3 scripts/build_images.py --engine podman --major 17 --repository registry.example/pgbk-worker`, а команды login/tag/push выполняйте через `podman`. Контрольная сумма WAL-G проверяется через `sha256sum`; расширения BuildKit не требуются. Проверки интеграции по-прежнему используют Docker.

```sh
docker login registry.example
python3 scripts/build_images.py --major 17 --repository registry.example/pgbk-worker
docker tag registry.example/pgbk-worker:17-test registry.example/pgbk-worker:17-0.2.0
docker push registry.example/pgbk-worker:17-0.2.0
docker buildx imagetools inspect registry.example/pgbk-worker:17-0.2.0
```

Замените `registry.example` и версию тега. Для каждой используемой основной версии PostgreSQL соберите отдельный образ. В registry используйте неизменяемый тег и/или включите запрет перезаписи тегов. После push получите digest manifest/image из вывода registry либо `docker buildx imagetools inspect`; в values укажите полный адрес `registry.example/pgbk-worker@sha256:…`, а не изменяемый тег. Chart требует образ по digest для каждой major-версии в конфигурации.

Для приватного registry настройте `imagePullSecrets` способом, принятым в кластере: например, создайте Secret типа `kubernetes.io/dockerconfigjson` в namespace каждой операции и подключите его через заранее созданный ServiceAccount. Не кладите registry credentials в Git. Учтите, что backup worker запускается в namespace релиза, а restore и verification могут запускаться в отдельных namespace; там тоже должны быть доступны образ и pull credentials.

## 2. Подготовьте Kubernetes и доступ Argo CD

1. Проверьте кластер: chart заявляет Kubernetes `>=1.27`, а исполняемые образы рассчитаны на Linux amd64. CronJob использует `spec.timeZone`.
2. Создайте namespace релиза, например `backups`, и namespace, которые будут использоваться для restore/verification, если они включены. Argo CD/Helm chart namespace не создаёт.
3. Создайте именованные coordination Lease в namespace, заданном `coordinationNamespace`. Если несколько установок работают с одним источником или репозиторием, они должны согласованно использовать общий namespace и ключи координации.
4. Создайте ServiceAccount для операций и выдайте им минимальные права, описанные в [эксплуатации](operations.md#rbac-установки-и-существующие-аккаунты). При выключенных `rbac.create`, `coordination.createLeases` и `serviceAccounts.create` Argo CD не создаёт эти ресурсы. Для одной общей namespace можно адаптировать `examples/installer-rbac.yaml` при первоначальной подготовке администратором. Обычный deployer может использовать `examples/deployer-rbac.yaml`, если все аккаунты и Lease уже существуют. Примеры нужно ограничить фактическими namespace и именами ресурсов; не выдавайте Argo CD кластерные права только ради установки.
5. Подготовьте существующий PostgreSQL и S3 bucket, сеть/DNS/TLS и политику bucket. Для физического профиля PostgreSQL должен разрешать нужный replication/base-backup доступ.

Проверьте право синхронизации identity Argo CD в каждом затрагиваемом namespace. Минимальный набор для стандартного профиля включает управление ConfigMap и CronJob; Helm release Secret также требует чтения/создания/обновления Secret в namespace релиза. Если включается NetworkPolicy, добавьте соответствующие права. Поскольку включение RBAC/ServiceAccount/Lease через chart является административной подготовкой, не включайте его без выданных администратором прав.

## 3. Создайте Secrets вне Git

Secret должны быть созданы в namespace соответствующего Job до запуска. Для обычного профиля резервирования понадобятся:

| Назначение | Secret keys |
|---|---|
| PostgreSQL target | `username`, `password` по умолчанию; имена настраиваются через `credentialsSecretKeys` |
| S3-compatible storage | `accessKeyId`, `secretAccessKey`; `sessionToken` при необходимости |
| CA PostgreSQL, если используется | `ca.crt` |
| Клиентский TLS сертификат, если используется | `tls.crt`, `tls.key` |
| Частный CA S3, если используется | `ca.crt` |

Например, администратор может создать Secret напрямую из защищённых файлов (файлы должны быть получены из secret manager и не попадать в Git):

```sh
kubectl -n backups create secret generic backup-postgres \
  --from-file=username=/secure/path/pg-user \
  --from-file=password=/secure/path/pg-password
kubectl -n backups create secret generic backup-s3 \
  --from-file=accessKeyId=/secure/path/s3-access-key-id \
  --from-file=secretAccessKey=/secure/path/s3-secret-access-key
```

В производственной GitOps-среде управляйте этими объектами через принятый secret manager/controller, например External Secrets или Sealed Secrets. Обычный Kubernetes Secret, закоммиченный в открытом виде или только base64-кодированный, не является безопасным способом хранения. Если restore/verification включены в другом namespace, соответствующие Secrets должны быть независимо созданы там. Chart Secret между namespace не копирует.

## 4. Подготовьте GitOps overlay

Скопируйте `examples/values.yaml` в отдельный файл конфигурации для окружения, например `environments/prod/pgbk-values.yaml`, и замените все примеры. Не переносите в файл пароли, access keys или приватные ключи. Для первого запуска проверьте, что настройки соответствуют реальным именам Secret и образу.

```yaml
installationId: prod-backups
coordinationNamespace: backups
rbac:
  create: false
coordination:
  createLeases: false
images:
  "17": registry.example/pgbk-worker@sha256:REPLACE_WITH_REAL_DIGEST
storage:
  archive:
    bucket: prod-postgres-backups
    prefix: cluster-a
    endpoint: https://storage.yandexcloud.net
    region: ru-central1
    addressingStyle: path
    credentialsSecretRef: backup-s3
    tls:
      verify: true
targets:
  primary:
    sourceId: prod-primary
    concurrencyKey: prod-primary
    host: postgres.database.svc.cluster.local
    port: 5432
    serverMajor: 17
    maintenanceDatabase: postgres
    credentialsSecretRef: backup-postgres
    tls:
      mode: verify-full
      caSecretRef: postgres-ca
profiles:
  prod-logical:
    serviceAccounts:
      create: false
      backup: pgbk-backup-worker
      repository: pgbk-repository-worker
    target: primary
    kind: logical
    storage: archive
    logical:
      databases: [application]
      globals: disabled
    schedule:
      enabled: false
    execution:
      resources:
        requests: {cpu: "500m", memory: 512Mi}
        limits: {cpu: "2", memory: 2Gi}
    scratch:
      type: pvc
      size: 20Gi
      storageClassName: standard
    retention:
      maxAgeDays: 30
      minSuccessfulBackups: 3
```

Этот пример сохраняет расписание backup выключенным; retention CronJob имеет собственное расписание. Проверьте его значения согласно `examples/values.yaml` и схеме. Для production задайте scratch размер и storage class по измеренному объёму, включите расписание backup только после успешного ручного пробного запуска. Для нескольких PostgreSQL major добавьте все используемые `images` по digest.

Если используете отдельные namespace для операций, задайте их явно в профиле restore/verification. Создайте в них требуемые ServiceAccount, Secret, PVC и права. Проверьте ограничения имён: сочетание Helm release и profile должно помещаться в лимит имени CronJob.

## 5. Добавьте Argo CD Application

Chart и values могут жить в одном Git-репозитории. Например, сохраните Application в отдельном репозитории bootstrap Argo CD либо примените его администратором:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: pgbk-prod
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://git.example.com/platform/pgbk.git
    targetRevision: main
    path: charts/pgbk
    helm:
      releaseName: pgbk
      valueFiles:
        - ../../environments/prod/pgbk-values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: backups
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
```

Укажите свой URL, revision, путь к values и namespace. Путь values должен разрешаться Argo CD из директории chart внутри того же репозитория; при необходимости разрешите Helm values file из родительской директории (`../../`) в настройках Argo CD либо перенесите файл values внутрь chart-пути/используйте multi-source Application. Для нескольких репозиториев удобен multi-source Application: chart из репозитория кода, values из конфигурационного репозитория с `ref` и `$values/...`.

Включение `prune: true` означает, что удалённые из Git ресурсы chart будут удалены Argo CD. Удаление Kubernetes CronJob/ConfigMap не удаляет уже выгруженные объекты в S3; uninstall также не удаляет существующие Secret/PVC/Lease. Прежде чем включать автоматическую синхронизацию, проверьте diff и убедитесь, что Application не владеет заранее созданными Secret, ServiceAccount, RBAC или Lease.

Не указывайте `CreateNamespace=true`, если namespace должен создаваться отдельным platform/bootstrap процессом. Application destination namespace должен существовать к моменту синхронизации.

## 6. Проверьте рендеринг до синхронизации

Из рабочего checkout репозитория проверьте values и chart локально:

```sh
helm lint charts/pgbk -f environments/prod/pgbk-values.yaml
helm template pgbk charts/pgbk --namespace backups -f environments/prod/pgbk-values.yaml
```

Убедитесь, что образ имеет реальный digest, namespace/Secret/ServiceAccount имена верны, CronJob для backup при начальном запуске suspended, а ресурсы scratch соответствуют доступной ёмкости. Проверьте Argo CD diff перед первой синхронизацией. На кластере проверьте права заранее созданных worker аккаунтов и доступность приватного образа с worker nodes.

## 7. Синхронизируйте и выполните пробный запуск

После успешной синхронизации проверьте CronJob и ConfigMap:

```sh
kubectl -n backups get cronjobs,configmaps
kubectl -n backups describe cronjob pgbk-prod-logical-backup
```

Точное имя CronJob строится из Helm release, профиля и операции; шаблон для основного backup — `<release>-<profile>-backup`. Запустите пробный Job из CronJob:

```sh
kubectl -n backups create job pgbk-smoke-$(date +%s) \
  --from=cronjob/pgbk-prod-logical-backup
kubectl -n backups get jobs,pods -w
kubectl -n backups logs job/<job-name>
```

Проверьте завершение Job, доступность записи и чтения в S3, успешную проверку SHA-256 и появление завершённой записи в каталоге. Затем выполните проверочное скачивание и восстановление в изолированное тестовое назначение по [инструкции эксплуатации](operations.md#скачивание-и-автономное-восстановление). Только после этого поменяйте `schedule.enabled: true` в Git и дождитесь Argo CD sync. Помните, что одной успешной загрузки недостаточно, чтобы подтвердить пригодность копии.

## 8. Обновления и эксплуатация GitOps

- Выпускайте worker под новым неизменяемым тегом, публикуйте его и обновляйте digest в Git отдельным reviewed commit.
- Изменения расписаний и конфигурации проводите через Git; применяйте их после проверки Argo diff.
- Не удаляйте старые образы, пока живые Jobs или план восстановления могут от них зависеть.
- Ротацию credentials выполняйте через secret manager. Если изменились имена Secret, сначала создайте новые Secret и затем поменяйте references в Git.
- Отслеживайте успешность CronJob/Job, возраст последней успешной копии, ошибки integrity/retention и результат restore verification. Практические команды и требования к восстановлению приведены в [эксплуатации](operations.md).
- Сохраняйте S3-данные независимо от жизненного цикла Argo CD. Chart не управляет bucket lifecycle и не удаляет backup при uninstall.

## Ограничения

Это инструкция развертывания существующей версии chart, а не подтверждение сертификации конкретного Kubernetes/S3/PostgreSQL окружения. Совместимость и непроверенные условия перечислены в [матрице совместимости](compatibility.md). Текущая версия использует WAL-G для raw-передачи артефактов в S3; она не использует нативный формат `backup-push` и не включает инкрементальные копии или PITR.
