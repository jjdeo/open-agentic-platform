{{- define "dark-factory.labels" -}}
app.kubernetes.io/name: dark-factory
app.kubernetes.io/part-of: dark-factory
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
dark-factory.requiredChecks — the dark-factory/* commit statuses merge.js must see
GREEN (and PRESENT) on the PR head before it will merge. Comma-separated.

Derived from the enabled flags rather than hardcoded, because each gate is
individually switchable and merge.js now treats an ABSENT status as a refusal. A
fixed list would demand a status that a disabled gate never posts and block every
merge; ignoring the flags entirely would let a gate that IS enabled go unchecked.

  implementation  always — posted by the coder inside the VM, not gated
  holdout         holdout.enabled     (templates/20 … "dark-factory/holdout")
  security        review.enabled      (posted as "dark-factory/" + role)
  devops          review.enabled      (same step, role=devops)
  deploy-test     deployTest.enabled  (templates/20 … "dark-factory/deploy-test")
*/}}
{{/*
dark-factory.docQueueName / .docQueueArn / .docQueueUrl — the one SQS queue S3
publishes spec uploads to.

Defined once here because FIVE objects have to agree on it (the Queue, its
QueuePolicy, the BucketNotification's queueArn, the EventSource's queue, and the
events role's inline policy). A literal repeated five times is how a notification
ends up pointing at a queue nothing consumes.

accountId goes through toString: an all-digit account id arrives from YAML or
--set as an int64, and printf %s on that renders the literal "%!s(int64=…)",
silently producing a malformed ARN and an IAM policy AWS rejects.
*/}}
{{- define "dark-factory.docQueueName" -}}
{{- .Values.docPipeline.queue.name | default (printf "%s-dark-factory-specs" .Values.docPipeline.podIdentity.clusterName) -}}
{{- end -}}

{{- define "dark-factory.docQueueArn" -}}
{{- $accountId := required "docPipeline.aws.accountId is required when docPipeline.enabled — inject it from the cluster's aws_account_id annotation" .Values.docPipeline.aws.accountId -}}
{{- printf "arn:aws:sqs:%s:%s:%s" (.Values.docPipeline.aws.region | toString) ($accountId | toString) (include "dark-factory.docQueueName" .) -}}
{{- end -}}

{{- define "dark-factory.docQueueUrl" -}}
{{- $accountId := required "docPipeline.aws.accountId is required when docPipeline.enabled — inject it from the cluster's aws_account_id annotation" .Values.docPipeline.aws.accountId -}}
{{- printf "https://sqs.%s.amazonaws.com/%s/%s" (.Values.docPipeline.aws.region | toString) ($accountId | toString) (include "dark-factory.docQueueName" .) -}}
{{- end -}}

{{- define "dark-factory.requiredChecks" -}}
{{- $checks := list "dark-factory/implementation" -}}
{{- if .Values.holdout.enabled -}}
{{- $checks = append $checks "dark-factory/holdout" -}}
{{- end -}}
{{- if .Values.review.enabled -}}
{{- $checks = append $checks "dark-factory/security" -}}
{{- $checks = append $checks "dark-factory/devops" -}}
{{- end -}}
{{- if .Values.deployTest.enabled -}}
{{- $checks = append $checks "dark-factory/deploy-test" -}}
{{- end -}}
{{- join "," $checks -}}
{{- end -}}

{{/*
dark-factory.githubTriggerEnabled — "true" or empty, for use in `and`/`if`.

Not `default true`: Helm treats false as empty, so an explicit
trigger.argoEvents.github.enabled=false would collapse back to true and a
doc-only deployment would still get the GitHub EventSource it turned off.
hasKey keeps the explicit value while defaulting absence to enabled.
*/}}
{{- define "dark-factory.githubTriggerEnabled" -}}
{{- if hasKey .Values.trigger.argoEvents "github" -}}
{{- if .Values.trigger.argoEvents.github.enabled -}}true{{- end -}}
{{- else -}}true{{- end -}}
{{- end -}}
