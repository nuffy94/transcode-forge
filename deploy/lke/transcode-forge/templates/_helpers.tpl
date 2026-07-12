{{- define "transcode-forge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "transcode-forge.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "transcode-forge.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "transcode-forge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "transcode-forge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "transcode-forge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* S3 env block shared by the scheduler and worker containers.
     Endpoint/region are plain values; keys come from the release Secret. */}}
{{- define "transcode-forge.s3Env" -}}
- name: TF_S3_ENDPOINT_URL
  value: {{ .Values.s3.endpointUrl | quote }}
- name: TF_S3_REGION
  value: {{ .Values.s3.region | quote }}
- name: TF_S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "transcode-forge.fullname" . }}
      key: TF_S3_ACCESS_KEY_ID
- name: TF_S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "transcode-forge.fullname" . }}
      key: TF_S3_SECRET_ACCESS_KEY
{{- end }}
