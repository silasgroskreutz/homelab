{{- define "bookorbit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "bookorbit.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "bookorbit.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "bookorbit.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "bookorbit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "bookorbit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bookorbit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "bookorbit.postgresFullname" -}}
{{- printf "%s-postgres" (include "bookorbit.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
