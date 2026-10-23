# ServiceAccount и Lease при развёртывании через Argo CD

Это пошаговая инструкция для установки `pgbk` в namespace, где у вас есть права. Она помогает понять, кто выполняет каждое действие, подготовить ServiceAccount worker'а, проверить права и получить точные имена Lease из конфигурации Helm.

## Кто есть кто

В этой схеме участвуют три идентичности:

1. **Ваш пользователь kubectl** — identity из вашего kubeconfig. Его права проверяются командами `kubectl auth can-i`. Если вы вручную применяете YAML, именно этот пользователь создаёт ресурсы.
2. **Argo CD** — применяет chart в целевой кластер с учётными данными, настроенными для destination-кластера. Обычно это кластерные credentials Argo CD; это не обязательно ServiceAccount Pod контроллера Argo CD.
3. **ServiceAccount worker'а** — identity, указанная в `spec.serviceAccountName` Pod backup/repository. Именно ей нужны права на Lease во время работы.

Права одного из этих участников не переходят к остальным. Например, `kubectl auth can-i update leases.coordination.k8s.io` без `--as` сообщает о правах вашего пользователя, а не worker'а.

## 1. Выберите namespace

Специальный namespace для координации не нужен. Для простой установки используйте один существующий namespace, например `backups`, и для Argo CD release, и для worker'ов, и для Lease:

```yaml
coordinationNamespace: backups
```

Если `coordinationNamespace` не задан, chart использует namespace Helm release. Если выберете отдельный namespace, Lease и Role должны находиться там; `RoleBinding` в этом namespace может ссылаться на ServiceAccount worker'а в другом namespace. Для такой схемы нужны права подготовки в обоих namespace.

## 2. Получите точные ключи и имена Lease

Не выбирайте имя Lease вручную по имени приложения или профиля. Chart вычисляет имена из ключей:

- Lease источника: ключ — `targets.<target>.concurrencyKey`; имя — `pgbk-source-` плюс первые 24 символа SHA-256 ключа.
- Lease репозитория: ключ строится из endpoint, bucket, prefix, `installationId`, `sourceId` и имени профиля; имя — `pgbk-repository-` плюс первые 24 символа SHA-256 этого ключа.

Один и тот же `concurrencyKey` у нескольких targets означает общий source Lease. Профиль входит в ключ Lease репозитория, поэтому разные профили обычно получают разные repository Lease. Все установки, которым нужно координироваться на одном источнике или репозитории, должны иметь совпадающие соответствующие ключи и namespace.

Посмотрите итоговую Helm-рендеризацию, включив создание Lease **только для рендеринга**. Это не применяет ресурсы в кластер:

```sh
helm template pgbk ./charts/pgbk \
  --namespace backups \
  --values environments/prod/pgbk-values.yaml \
  --set coordination.createLeases=true
```

В выводе найдите `kind: Lease`, затем посмотрите `metadata.name` и `metadata.namespace`. Это точные имена, которые нужно использовать в Role worker'а. Если используете другой release name, namespace или values-файл, подставьте их в команду. Не коммитьте включённый `createLeases`, если Argo CD не должен создавать Lease.

Перед рендерингом убедитесь, что values задают `targets.<id>.concurrencyKey`, `targets.<id>.sourceId`, `installationId`, а также storage endpoint/bucket/prefix и profile ID. Изменение этих значений может выбрать другой Lease и другой каталог резервных копий.

## 3. Проверьте права своего пользователя

Выполните проверки в namespace, где будут создаваться ресурсы. Команды не меняют кластер:

```sh
kubectl auth can-i create serviceaccounts -n backups
kubectl auth can-i create roles.rbac.authorization.k8s.io -n backups
kubectl auth can-i create rolebindings.rbac.authorization.k8s.io -n backups
kubectl auth can-i create leases.coordination.k8s.io -n backups
```

Для ручной подготовки нужны первые три разрешения; `create leases` нужен только тому, кто будет создавать сами Lease. Ответ `yes` на `create rolebindings` сам по себе не означает, что Kubernetes позволит привязать любые права: создание Role/RoleBinding ограничено правилами RBAC против повышения привилегий. В частности, роль можно выдать, если у создателя уже есть все указанные в ней права либо выдано специальное право `bind`.

Если какой-либо ответ `no`, или API отклоняет YAML с `forbidden`, попросите администратора подготовить соответствующий ресурс в вашем namespace. Это не требует cluster-admin для всего приложения: администратор может создать только нужные namespaced Role/RoleBinding и ServiceAccount.

## 4. Подготовьте worker ServiceAccount и минимальные права

Для одного source и одного профиля пример ниже создаёт аккаунт и разрешает ему читать и обновлять только два нужных Lease. Замените имена в `resourceNames` на значения, полученные на шаге 2. Все имена Lease должны быть в namespace, указанном у Role.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: pgbk-backup-worker
  namespace: backups
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pgbk-backup-lease-access
  namespace: backups
rules:
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    resourceNames:
      - pgbk-source-REPLACE_WITH_RENDERED_NAME
      - pgbk-repository-REPLACE_WITH_RENDERED_NAME
    verbs: ["get", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: pgbk-backup-lease-access
  namespace: backups
subjects:
  - kind: ServiceAccount
    name: pgbk-backup-worker
    namespace: backups
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: pgbk-backup-lease-access
```

Сохраните как `worker-lease-rbac.yaml` и примените от своего пользователя:

```sh
kubectl apply -f worker-lease-rbac.yaml
```

Lease должны существовать заранее. Если вы создаёте их вручную, примените отредактированный вывод `helm template` с объектами `kind: Lease` либо отдельный YAML с теми же именами и namespace. Их `spec` может быть пустым: worker заполняет поля при захвате. Сам worker не должен создавать Lease.

Для дополнительных профилей добавьте соответствующие имена Lease в `resourceNames` или подготовьте отдельные Role/RoleBinding. У проекта также могут быть отдельные ServiceAccount для backup, repository, verification и restore; назначайте роль именно тому аккаунту, под которым запускается соответствующая операция. Namespace restore/verification может отличаться от namespace backup.

## 5. Настройте values, чтобы chart использовал готовые аккаунты

Укажите существующие ServiceAccount и оставьте создание ресурсов выключенным:

```yaml
rbac:
  create: false
coordination:
  createLeases: false

profiles:
  prod-logical:
    serviceAccounts:
      create: false
      backup: pgbk-backup-worker
      repository: pgbk-repository-worker
```

Создайте `pgbk-repository-worker` и выдайте ему `get`/`update` на соответствующий repository Lease тем же способом. Профили с включённой verification/restore требуют существующих ServiceAccount и RoleBinding также в namespace этих операций. Конкретные обязательные аккаунты перечислены в `docs/ru/configuration.md`.

Обычный chart не создаёт ServiceAccount, RBAC и Lease. Argo CD по-прежнему должен иметь права на ресурсы самого приложения, например ConfigMap, CronJob и Helm release Secrets. Для deployer без прав управления RBAC см. `examples/deployer-rbac.yaml`.

Если вы намеренно хотите поручить создание ServiceAccount/RBAC/Lease самому Argo CD, эти объекты можно хранить в Git, но тогда identity, которой Argo CD применяет Application, должна иметь права на них и право выдавать указанные worker-права. Не включайте `rbac.create` или `coordination.createLeases` только ради обхода ограничений: это требует административных полномочий у применяющей identity.

## 6. Проверьте права worker'а

Проверьте каждое имя Lease отдельно:

```sh
kubectl auth can-i get leases.coordination.k8s.io \
  --resource-name=pgbk-source-REPLACE_WITH_RENDERED_NAME \
  -n backups \
  --as=system:serviceaccount:backups:pgbk-backup-worker

kubectl auth can-i update leases.coordination.k8s.io \
  --resource-name=pgbk-source-REPLACE_WITH_RENDERED_NAME \
  -n backups \
  --as=system:serviceaccount:backups:pgbk-backup-worker
```

Повторите команды для repository Lease и для каждого ServiceAccount. Ожидаемый результат — `yes`. Флаг `--as` проверяет права указанной identity, но ваш пользователь должен иметь право `impersonate` этой ServiceAccount. Если получаете ошибку про impersonation, попросите администратора выполнить проверку или проверьте созданные Role/RoleBinding:

```sh
kubectl get serviceaccount,role,rolebinding -n backups
kubectl describe role pgbk-backup-lease-access -n backups
kubectl describe rolebinding pgbk-backup-lease-access -n backups
```

## 7. Проверьте identity Argo CD

`kubectl auth can-i` без `--as` проверяет вашего пользователя. Чтобы узнать, сможет ли Argo CD создать обычные ресурсы приложения, нужны credentials, настроенные в Argo CD для целевого кластера. Их нельзя надёжно определить только по имени Pod контроллера: Argo CD может использовать отдельные credentials кластера.

Попросите владельца Argo CD подтвердить целевой кластер и namespace, затем проверить теми credentials права на синхронизируемые ресурсы. Для стандартной установки права на ServiceAccount/Role/RoleBinding Argo CD не нужны, если вы подготовили их заранее и chart настроен на существующие аккаунты. Если Argo CD должен сам создавать эти объекты, проверьте `can-i create serviceaccounts`, `create roles` и `create rolebindings` именно для его destination identity; кроме этого, выдаваемые worker-права должны быть допустимы по правилам RBAC.

## Если что-то не работает

- `forbidden` при `kubectl apply`: не хватает прав вашему пользователю либо вы пытаетесь выдать права выше разрешённых.
- Argo CD показывает `SyncFailed` на ServiceAccount/Role/RoleBinding: этих ресурсов нет в GitOps-манифестах или destination identity не может их применить.
- Worker получает `leases.coordination.k8s.io is forbidden`: проверьте namespace Lease, имя в `resourceNames`, RoleBinding subject и `spec.serviceAccountName` Pod.
- `can-i` от worker'а отвечает `no`: сравните ServiceAccount из команды с `kubectl get pod ... -o jsonpath='{.spec.serviceAccountName}'` и проверьте, что RoleBinding ссылается на неё.
- Worker не находит ожидаемый Lease: повторно отрендерите chart с тем же release name, namespace и теми же values; сравните `metadata.name` и namespace с фактическими Lease.

Подробнее о требованиях проекта: [Argo CD deployment](argocd-deployment.md), [эксплуатация и RBAC](operations.md), [конфигурация](configuration.md). Официальные правила: [ограничения RBAC при создании Role/RoleBinding](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#restrictions-on-role-creation-or-update), [проверка через kubectl auth can-i](https://kubernetes.io/docs/reference/access-authn-authz/authorization/#checking-api-access).
