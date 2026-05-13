{{/*
Common labels.
*/}}
{{- define "zkcex.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: zkcex
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
zkcex.io/region: {{ .Values.region | default "a" }}
zkcex.io/primary: {{ .Values.isPrimary | default false | quote }}
{{- end -}}

{{/*
Selector labels for matchLabels (must be stable).
*/}}
{{- define "zkcex.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: zkcex
{{- end -}}

{{/*
Compute final replica count: explicit svc.replicas, else 1.
For singletons we still allow 1 replica (active+passive pattern uses Lease).
*/}}
{{- define "zkcex.replicas" -}}
{{- default 1 .svc.replicas -}}
{{- end -}}

{{/*
Resources block: prefer svc.resources, fallback to .Values.defaults.resources.
*/}}
{{- define "zkcex.resources" -}}
{{- if .svc.resources -}}
{{ toYaml .svc.resources }}
{{- else -}}
{{ toYaml .Values.defaults.resources }}
{{- end -}}
{{- end -}}

{{/*
Image reference for a service: <repo>/zkcex-<name>:<tag>.
Honors per-service overrides at .Values.services.<name>.image.{repository,name,tag}.
*/}}
{{- define "zkcex.image" -}}
{{- $svc := index .Values.services .name | default dict -}}
{{- $img := $svc.image | default dict -}}
{{- $repo := $img.repository | default .Values.image.repository -}}
{{- $imgName := $img.name | default .name -}}
{{- $tag := $img.tag | default .Values.image.tag -}}
{{ $repo }}/zkcex-{{ $imgName }}:{{ $tag }}
{{- end -}}

{{/*
Standard pod anti-affinity: spread across nodes (required), spread across zones (preferred).
*/}}
{{- define "zkcex.podAntiAffinity" -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            app.kubernetes.io/name: {{ .name }}
        topologyKey: kubernetes.io/hostname
    - weight: 50
      podAffinityTerm:
        labelSelector:
          matchLabels:
            app.kubernetes.io/name: {{ .name }}
        topologyKey: topology.kubernetes.io/zone
{{- end -}}

{{/*
Topology spread constraints: even distribution across zones.
*/}}
{{- define "zkcex.topologySpreadConstraints" -}}
- maxSkew: 1
  topologyKey: topology.kubernetes.io/zone
  whenUnsatisfiable: ScheduleAnyway
  labelSelector:
    matchLabels:
      app.kubernetes.io/name: {{ .name }}
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: ScheduleAnyway
  labelSelector:
    matchLabels:
      app.kubernetes.io/name: {{ .name }}
{{- end -}}
