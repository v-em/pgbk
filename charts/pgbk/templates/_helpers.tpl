{{- define "pgbk.sourceLease" -}}
pgbk-source-{{ . | sha256sum | trunc 24 }}
{{- end }}
{{- define "pgbk.repositoryLease" -}}
{{- $storage := index .root.Values.storage .profile.storage -}}
{{- $target := index .root.Values.targets .profile.target -}}
{{- $key := printf "%s/%s/%s/v1/%s/%s/%s/" (trimSuffix "/" $storage.endpoint) $storage.bucket $storage.prefix .root.Values.installationId $target.sourceId .name -}}
pgbk-repository-{{ $key | sha256sum | trunc 24 }}
{{- end }}
{{- define "pgbk.configName" -}}
{{ printf "%s-config-%s" .Release.Name (.Values | toJson | sha256sum | trunc 16) }}
{{- end }}
{{- define "pgbk.scratchVolume" -}}
- name: scratch
  {{- if eq .type "emptyDir" }}
  emptyDir:
    sizeLimit: {{ .size | quote }}
  {{- else if eq .type "pvc" }}
  ephemeral:
    volumeClaimTemplate:
      metadata:
        labels:
          pgbk.io/purpose: scratch
      spec:
        accessModes: [ReadWriteOnce]
        {{- if hasKey . "storageClassName" }}
        storageClassName: {{ .storageClassName | quote }}
        {{- end }}
        resources:
          requests:
            storage: {{ .size | quote }}
  {{- else }}
  persistentVolumeClaim:
    claimName: {{ required "Existing scratch requires claimName" .claimName | quote }}
  {{- end }}
{{- end }}

{{- define "pgbk.account" -}}
{{- if and (eq .purpose "verify") .profile.verification .profile.verification.serviceAccountName -}}
{{ .profile.verification.serviceAccountName }}
{{- else -}}
{{- $accounts := .profile.serviceAccounts | default dict -}}
{{- $configured := index $accounts .purpose -}}
{{- if $configured -}}
{{ $configured }}
{{- else if eq (dig "create" false $accounts) false -}}
{{- fail (printf "Existing ServiceAccount name required for %s" .purpose) -}}
{{- else -}}
{{ printf "%s-%s%s" .root.Release.Name .name (ternary "" (printf "-%s" .purpose) (eq .purpose "backup")) }}
{{- end -}}
{{- end }}
{{- end }}

{{- define "pgbk.profile" -}}
{{- $profile := deepCopy .profile -}}
{{- if and $profile.verification $profile.verification.enabled -}}
{{- $environment := index (.root.Values.verificationEnvironments | default dict) ($profile.verification.environmentRef | default "") -}}
{{- if not $environment }}{{ fail "Enabled verification requires a named verificationEnvironments entry" }}{{ end -}}
{{- if dig "schedule" "enabled" false $profile.verification }}{{ fail "Scheduled verification is deferred; use a manual copy ID" }}{{ end -}}
{{- $_ := set $profile "verification" (mergeOverwrite (deepCopy $environment) $profile.verification) -}}
{{- end -}}
{{ $profile | toYaml }}
{{- end }}

{{- define "pgbk.verificationSlots" -}}
{{- $slots := list -}}
{{- range $i := until (int (.maxConcurrent | default 1)) -}}
{{- $slots = append $slots (printf "pgbk-verification-%s" (printf "%s/%d" $.concurrencyKey $i | sha256sum | trunc 24)) -}}
{{- end -}}
{{ $slots | toJson }}
{{- end }}
