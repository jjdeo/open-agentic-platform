# dark-factory — Flow B, Phase P1

**Argo Workflows on the hub that turn a GitHub issue into a PR**, claiming the
[Flow A](../agent-sandbox) warm Kata pool. This is P1 of the
[Dark Factory pattern](../../../../docs/dark-factory/README.md): trigger → claim
warm sandbox → coder implements + tests → open PR with live status → **stop,
awaiting human**. Verification gates (holdout, Security/DevOps), the iterate loop,
and merge/teardown are P2–P4.

## What it installs (hub only, `alwaysSelector: environment In [control-plane]`)

| Template | Resource | Role |
|---|---|---|
| `10-rbac.yaml` | SA `dark-factory-workflow` (argo ns) + Roles/Bindings | Workflow SA: executor perms in `argo`; sandboxclaims CRUD + read sandboxes/pods in `agent-sandbox-system`. No secrets, no exec, no cluster scope. |
| `20-workflowtemplate-df-run.yaml` | `WorkflowTemplate df-run` | The P1 pipeline: claim → await coder (GitHub poll) → status; `onExit` releases the claim. Per-issue mutex. |
| `30-workfloweventbinding.yaml` | `WorkflowEventBinding dark-factory` | Submits `df-run` from a POST to the argo-server events endpoint (the thin trigger). |

## How it works

1. **Trigger** — an issue labelled `dark-factory` fires the GitHub Action
   (`.github/workflows/dark-factory.yml`), which POSTs the issue to
   `argo-server/api/v1/events/argo/dark-factory`. The `WorkflowEventBinding`
   submits `df-run` keyed on the issue id.
2. **Claim** — the `claim` step creates a `SandboxClaim(warmPoolRef)` with the
   issue injected as env (`DF_ISSUE_NUMBER`, `DF_REPO`, `DF_BRANCH`, …). The
   operator provisions a **fresh** warm micro-VM with that env present at start.
3. **Code** — the coder image (baked into the Flow A SandboxTemplate) reads the
   `DF_*` env, fetches the issue as `SPEC.md`, implements on `df/issue-N` via
   Claude Code + Bifrost, builds + tests, pushes, and **opens the PR itself**,
   then sets the `dark-factory/implementation` commit status. The coder is
   credential-less to the k8s API, so **GitHub is the completion bus**.
4. **Await** — the `await-coder` step polls the GitHub API for the PR + that
   commit status (success/failure).
5. **Teardown** — `onExit` deletes the SandboxClaim (success *or* failure); the
   operator refills the pool. A reaper is the crash-net for force-killed runs.

Verified live end-to-end on the hub: claim binds, all `DF_*` env inject, the
coder container starts with the issue, and `onExit` releases the claim + refills
the pool (3/3).

## Activation (remaining, to run a real issue)

P1 is deployed and its **orchestration is proven** with the placeholder coder
image. To process a real issue:

1. **Build + push the coder image** (`examples/dark-factory/coder`, `entrypoint.js`)
   to ECR, then set `agent-sandbox` chart `coderTemplate.image` to it. Until then
   the warm pods idle on the busybox placeholder (claim mechanics still work).
2. **Provide the GitHub credentials** — three separate, differently-scoped
   credentials, provisioned from AWS Secrets Manager by ExternalSecrets (there is
   no longer a single shared `dark-factory-github` secret). See
   [docs/dark-factory §10a](../../../../docs/dark-factory/README.md#10a-github-credentials-secrets-manager-setup)
   for the SM keys, the exact GitHub permissions, and the `aws secretsmanager`
   commands.
3. **Wire the trigger** — set repo/org var `DARK_FACTORY_ARGO_SERVER` and secret
   `DARK_FACTORY_ARGO_TOKEN`, then label an issue `dark-factory`.

## Key values

| Value | Default | Purpose |
|---|---|---|
| `warmPool.name` | `coder-warmpool` | Flow A pool to claim from |
| `coder.profile` | `claude-code` | `claude-code` \| `kiro` |
| `coder.runTimeoutMinutes` | `30` | `await-coder` deadline |
| `claimTtlSeconds` | `10800` | reaper backstop on the claim |
| `github.tokenSecret` | `dark-factory-github-orchestrator` | PR + status + merge (hub, `argo` ns) |
| `trigger.argoEvents.githubSecret` | `dark-factory-github-events` | webhook HMAC + repo read (hub, `argo-events` ns) |

## Doc pipeline — Markdown spec in S3 → built HTML prototype in S3 (`docPipeline`)

A second, independent pipeline that reuses the same Kata coder to BUILD the
prototype an uploaded Markdown spec describes, as one self-contained HTML file.
The spec is treated as build instructions, not as content to publish: the result
is a working interface, not the spec rendered as a web page. No GitHub anywhere:
the trigger is an S3 upload and the result is an object plus a presigned link.

Additive and gated. With `docPipeline.enabled=false` (the default) nothing below
renders and the GitHub pipeline above is untouched — the chart's rendered output
is byte-identical to a tree without this feature.

### Flow

1. **Upload** `something.md` under `s3://<input.bucket>/<input.prefix>`.
2. S3 `ObjectCreated` → **SQS** → the `dark-factory-specs` **SQS EventSource**.
   Argo Events has no AWS-S3 source (its `s3` source speaks MinIO's protocol,
   which AWS S3 does not implement), so SQS is the only supported AWS path.
3. The Sensor submits **`df-doc-run`**, named `df-doc-<sha1(key|etag)>` so a
   duplicate delivery collides and is a no-op.
4. `fetch-spec` (trusted) validates the key against the configured prefix and
   bucket, size-checks the object, derives a title, and mints a short presigned
   **GET** for the spec.
5. `presign-output` (trusted) mints a presigned **PUT** bound to exactly one key,
   `<output.prefix>/<run>/index.html`, plus one for the `FAILED` sentinel.
6. `claim-sandbox` claims a warm micro-VM, injecting only those URLs — **no AWS
   credentials enter the sandbox**. The presigned URL is a capability for one
   object, not an identity, so the VM can write that key and nothing else.
7. The coder (`DF_MODE=doc`) generates the HTML and `PUT`s it with plain HTTPS.
   On failure it PUTs the sentinel so the hub stops waiting immediately.
8. `await-output` polls `HeadObject` — S3 is the completion bus, exactly as
   GitHub is for `df-run`. `publish-link` then mints the presigned **GET**.
9. `onExit` releases the claim on success and failure alike.

### Prerequisites

- **`agent_sandbox: true` AND `dark_factory: true`** in
  `gitops/overlays/environments/control-plane/enabled-addons.yaml`. Both are
  `false` upstream, and `dark_factory` requires `agent_sandbox` because the
  latter installs the Sandbox CRDs and the warm pool this claims from.
- **A dedicated, credential-free warm pool.** `docPipeline.warmPool.name` defaults
  to `doc-warmpool` and must NOT be the shared `coder-warmpool`: that pool's
  template mounts the standing `dark-factory-github-coder` write token on every
  pod, and a SandboxClaim cannot suppress a podTemplate volume. Doc-mode prompts
  come from whoever can upload a spec, so pointing it at the shared pool would
  expose a GitHub write token to an untrusted prompt. Create the pool with
  `secretsMount.enabled=false`. The coder enforces this: doc mode exits rather
  than run with a readable token.
- **A real coder image.** The warm pool idles on a busybox placeholder until one
  is built from `examples/dark-factory/coder` and pushed to ECR, with
  `agent-sandbox` `coderTemplate.image` pointed at it. Claim mechanics work
  without it; document generation does not.
- **`provider-aws-s3` and `provider-aws-sqs`**, which `04-doc-pipeline-providers.yaml`
  declares because a stock platform runs neither — `crossplane-base` gates every
  provider behind an empty version and has no `sqs` entry at all. Set
  `docPipeline.crossplaneProviders.enabled=false` if the base platform is ever
  configured to own them; `Provider` is cluster-scoped and two Applications
  declaring the same name is a sync loop.

### Link lifetime, honestly

The shareable link is a presigned GET and both buckets stay private. A presigned
URL signed with the temporary credentials Pod Identity vends dies when those
credentials do, regardless of `X-Amz-Expires`, so expect **hours, not days** —
`presign.py` logs the real cap. For a durable link the answer is CloudFront +
Origin Access Control with signed URLs, not a public bucket.

Note also that the output is **model-authored HTML served from an S3 origin**,
where no Content-Security-Policy can be set. See `docPipeline.output.contentType`.

### Key values

| Value | Default | Purpose |
|---|---|---|
| `docPipeline.enabled` | `false` | the gate |
| `docPipeline.input.bucket` / `.prefix` | — / `specs/` | where specs are picked up |
| `docPipeline.output.bucket` / `.prefix` | — / `docs/` | where documents are written |
| `docPipeline.coderModel` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | an inference profile; the `claude-sonnet` alias and bare model ids do not invoke |
| `docPipeline.presign.putExpirySeconds` | `3600` | write capability handed to the VM |
| `docPipeline.presign.getExpirySeconds` | `43200` | requested share-link lifetime (upper bound) |
| `docPipeline.claimTimeoutSeconds` | `900` | give up if the warm pool never binds |
