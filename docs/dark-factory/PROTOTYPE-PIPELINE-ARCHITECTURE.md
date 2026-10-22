# Prototype Pipeline — Architecture

A Markdown spec uploaded to S3 is turned into a **working HTML prototype** by a
coding agent running in a Kata micro-VM, and published as a presigned link. No
GitHub, no human in the loop, and **no AWS credentials inside the VM**.

All diagrams are Mermaid and render on GitHub. Every component shown was verified
on a live `oap-hub` cluster in account `818947917436` / `us-west-2`.

Companion to [`README.md`](./README.md) (the GitHub-issue pipeline this reuses)
and the chart reference in
[`gitops/addons/charts/dark-factory/README.md`](../../gitops/addons/charts/dark-factory/README.md).

---

## 1. System context

Where the pipeline sits in the platform. Everything in blue is pre-existing OAP;
green is added by `docPipeline`.

```mermaid
flowchart TB
    subgraph user["Anyone who can write to the input bucket"]
        UP["uploads spec.md"]
    end

    subgraph aws["AWS — account 818947917436 / us-west-2"]
        S3IN["S3 · oap-dark-factory-specs<br/>specs/*.md"]
        SQS["SQS · oap-hub-dark-factory-specs"]
        S3OUT["S3 · oap-dark-factory-docs<br/>docs/&lt;spec name&gt;.html"]
        BR["Amazon Bedrock<br/>claude-sonnet-4-5"]
    end

    subgraph eks["EKS cluster oap-hub — Auto Mode + Karpenter"]
        subgraph hub["Trusted hub nodes"]
            AE["Argo Events<br/>EventSource + Sensor"]
            AW["Argo Workflows<br/>df-doc-run"]
            BF["Bifrost<br/>LLM gateway"]
        end
        subgraph kata["Kata node · c8i.2xlarge · nested virt"]
            VM["Coder micro-VM<br/>NO AWS credentials"]
        end
    end

    UP --> S3IN
    S3IN -- "ObjectCreated" --> SQS
    SQS --> AE
    AE -- "submits" --> AW
    AW -- "claims" --> VM
    AW -- "presigned PUT URL" --> VM
    VM -- "HTTPS PUT<br/>one object key" --> S3OUT
    VM -- "prompt" --> BF
    BF --> BR
    AW -- "presigned GET" --> SHARE["shareable link"]

    classDef added fill:#d5f5e3,stroke:#1e8449,color:#000
    classDef base fill:#d6eaf8,stroke:#2471a3,color:#000
    class S3IN,SQS,S3OUT,VM,SHARE added
    class AE,AW,BF,BR base
```

Both buckets are **private** — all four public-access-block flags true, SSE-AES256.
The only way out is a presigned URL.

---

## 2. Runtime flow — upload to link

```mermaid
sequenceDiagram
    autonumber
    actor U as Uploader
    participant S3I as S3 specs bucket
    participant Q as SQS
    participant ES as EventSource<br/>(argo-events)
    participant SE as Sensor
    participant WF as Workflow df-doc-run<br/>(argo, trusted)
    participant SB as Kata micro-VM
    participant BF as Bifrost
    participant S3O as S3 docs bucket

    U->>S3I: PUT specs/fleet-ops.md
    S3I->>Q: ObjectCreated notification<br/>(prefix specs/ · suffix .md)
    ES->>Q: ReceiveMessage (Pod Identity)
    ES->>SE: event
    SE->>SE: filter eventName =~ ^ObjectCreated:<br/>and key matches prefix/suffix
    SE->>WF: submit, name derived from sha1 of key + etag

    par trusted hub steps
        WF->>S3I: GetObject spec → derive title
        WF->>WF: mint presigned GET for the spec
    and
        WF->>WF: mint presigned PUT for<br/>a per-run staging key + FAILED
    end

    WF->>SB: SandboxClaim → bind warm pod,<br/>inject DF_* env (no credentials)
    SB->>S3I: GET spec via presigned URL
    loop until done or 20 min
        SB->>BF: /anthropic/v1/messages (via localhost ua-shim)
        BF->>BF: → Bedrock, 900s timeout
        BF-->>SB: generated HTML
    end
    SB->>SB: validate: size, html element, interactive markers
    SB->>S3O: PUT staging object via presigned URL
    note over SB: on failure PUTs FAILED instead

    loop every 15s, up to 30 min
        WF->>S3O: HeadObject staging / FAILED
    end
    WF->>WF: mint presigned GET (shareable link)
    WF->>SB: onExit teardown — delete claim
    WF-->>U: link in workflow output + logs
```

The hub polls S3 as the **completion bus**. In the GitHub pipeline that role is
played by GitHub itself; here S3 is the only channel the VM can write to.

---

## 3. Workflow DAG

`df-doc-run`, with per-step deadlines as deployed.

```mermaid
flowchart LR
    START(( )) --> FS["fetch-spec<br/>300s"]
    START --> PO["presign-output<br/>300s"]
    FS --> CL["claim<br/>900s"]
    PO --> CL
    CL --> AO["await-output<br/>1800s · poll 15s"]
    AO --> PL["publish-link<br/>300s"]
    PL --> DONE(( ))
    CL -.-> TD["onExit: teardown<br/>delete SandboxClaim"]
    AO -.-> TD
    PL -.-> TD

    classDef trusted fill:#d6eaf8,stroke:#2471a3,color:#000
    classDef exit fill:#fdebd0,stroke:#b9770e,color:#000
    class FS,PO,CL,AO,PL trusted
    class TD exit
```

| Step | Does | Why it is on the hub, not in the VM |
|---|---|---|
| `fetch-spec` | Validates the key, reads the spec, derives the title, mints a run-scoped presigned GET | Needs `s3:GetObject` |
| `presign-output` | Mints presigned PUTs for the per-run staging key and the `FAILED` sentinel | Minting requires credentials the VM must never hold |
| `claim` | Creates a `SandboxClaim`, binds a warm pod, injects `DF_*` | Needs cluster RBAC |
| `await-output` | `HeadObject` poll for either object | The VM cannot be trusted to report its own success |
| `publish-link` | Mints the shareable presigned GET | Credentials |
| `teardown` | Deletes the claim so the pool recycles | Runs even on failure |

`onExit` is unconditional — a failed or deadlined run still releases its sandbox.

### Naming: the spec file is the document's identity

The published key mirrors the spec's own path below the input prefix:

| Uploaded spec | Published document |
|---|---|
| `specs/fleet-ops.md` | `docs/fleet-ops.html` |
| `specs/team/dash.md` | `docs/team/dash.html` |

Re-uploading the same filename **replaces** that document, so a shared link always
resolves to the current version. The relative path is kept rather than just the
basename so two specs with the same file name in different folders cannot silently
overwrite each other.

The coder still uploads to a per-run **staging** key, and `publish-link` copies it
to the named key only after the run succeeds. Two reasons this indirection is not
optional:

- A failed or abandoned run would otherwise destroy the version people already hold
  a link to.
- `await-output` treats "object exists" as "this run finished". Pointed at a stable
  name, the *previous* run's document satisfies the very first poll, and the
  workflow would publish stale output as a success.

Accepted limitation: two runs for the same filename with different content are not
serialised, because the workflow mutex is keyed on content. The named object is
replaced by a single atomic copy, so a reader never sees a partial document, but
with overlapping runs which one lands last is not defined.

---

## 4. Trust boundary and the credential model

The single most important diagram. The red boundary is the only place untrusted
model output executes.

```mermaid
flowchart TB
    subgraph trusted["TRUSTED — hub nodes, holds AWS credentials"]
        direction TB
        WF["df-doc-run steps<br/>SA: dark-factory-doc<br/>Pod Identity → IAM"]
        PRE["presign.py<br/>stdlib SigV4 signer"]
        WF --- PRE
    end

    subgraph untrusted["UNTRUSTED — Kata micro-VM, prompt supplied by whoever uploads"]
        direction TB
        CODER["coder entrypoint.js<br/>+ claude CLI"]
        NOCRED["no serviceAccount token<br/>no IMDS reachable<br/>no GitHub token mounted"]
        CODER --- NOCRED
    end

    PRE -->|"presigned PUT URL<br/>scoped to ONE key,<br/>time-limited"| CODER
    CODER -->|"plain HTTPS PUT<br/>no signing capability"| S3OUT["S3 docs bucket<br/>private"]
    WF -->|"HeadObject poll"| S3OUT
    WF -->|"presigned GET"| LINK["shareable link"]

    classDef t fill:#d5f5e3,stroke:#1e8449,color:#000
    classDef u fill:#fadbd8,stroke:#c0392b,color:#000
    class WF,PRE t
    class CODER,NOCRED u
```

Enforced properties, each verified live:

| Property | Mechanism |
|---|---|
| No AWS credentials in the VM | `automountServiceAccountToken: false`, no service account, IMDS unreachable from the guest |
| Cannot write anywhere but its own object | Presigned PUT is scoped to one key and expires |
| Cannot read other runs' output | No bucket-wide permission exists to delegate |
| No GitHub write token | `doc-sandbox` omits the `coder-secrets` volume **and** the coder refuses to start if `GH_TOKEN_PATH` is readable |
| Cannot reach cluster-internal services | NetworkPolicy denies RFC1918; only the Bifrost ClusterIP is allowed |
| Cannot exhaust the account | Per-step deadlines, size ceilings, one claim per run |

Why this matters: the prompt comes from **anyone who can upload a file**. A
credential in that VM would be a credential handed to an untrusted prompt.

---

## 5. Sandbox internals and the network path

```mermaid
flowchart LR
    subgraph pod["Kata pod · runtimeClass kata-clh · dnsPolicy None"]
        direction TB
        EP["entrypoint.js"]
        SHIM["ua-shim<br/>127.0.0.1:8791"]
        CLI["claude CLI"]
        WS["/workspace<br/>SPEC.md · out/index.html"]
        EP --> SHIM
        EP --> CLI
        CLI --> WS
        CLI -->|"ANTHROPIC_BASE_URL"| SHIM
    end

    SHIM -->|"rewrites User-Agent to<br/>dark-factory-coder"| BF["Bifrost ClusterIP<br/>172.20.0.100:8080<br/>/anthropic"]
    BF --> BR["Bedrock<br/>us.anthropic.claude-sonnet-4-5<br/>timeout 900s"]
    CLI -.->|"presigned PUT<br/>public S3 endpoint"| S3["S3 docs bucket"]

    classDef vm fill:#fadbd8,stroke:#c0392b,color:#000
    class EP,SHIM,CLI,WS vm
```

Three non-obvious constraints, all learned the hard way:

**`BIFROST_URL` must be the ClusterIP, never service DNS.** Kata pods run with
`dnsPolicy: None` and cannot resolve cluster DNS. The claim step's injected value
overrides the SandboxTemplate default.

**The ua-shim exists because Bifrost does User-Agent-prefix routing.** Any request
whose UA starts with `claude-cli` is run through a Claude-Code request transform
that is broken on this build and returns `400 Unexpected field type` regardless of
body. The shim rewrites the UA and transparently forwards everything, including SSE
streams. It must be a separate process — `execFileSync` blocks the Node event loop
for the whole run, so an in-process server would never accept a connection.

**Bifrost's default request timeout is 30 seconds.** Generation requests take up to
~110s, so every build failed with a 504 until `network_config.default_request_timeout_in_seconds: 900`
was set. Only trivially short completions fit the default — which is exactly why a
prose rendering succeeded and a real prototype build could not.

### Egress policy

Two NetworkPolicies apply to the doc pod, and NetworkPolicies are additive:

```mermaid
flowchart TB
    POD["doc sandbox pod<br/>labels: role=coder,<br/>sandbox-template-ref-hash=728f52d7"]
    POD -->|"allow 53/udp,tcp"| DNS["DNS"]
    POD -->|"allow 8080/tcp"| BFNS["ClusterIP 172.20.0.100<br/>+ bifrost namespace"]
    POD -->|"allow 0.0.0.0/0<br/>EXCEPT 10/8, 172.16/12,<br/>192.168/16, 169.254/16"| NET["public internet<br/>(needed for presigned PUT to S3)"]
    POD -.->|"DENIED"| PRIV["any other cluster service,<br/>IMDS at 169.254.169.254"]

    classDef deny fill:#fadbd8,stroke:#c0392b,color:#000
    class PRIV deny
```

Blocking link-local (`169.254.0.0/16`) is what denies IMDS, closing the usual
credential-theft path out of a container.

### Container hardening

`runAsNonRoot: true` · `runAsUser: 1000` · `readOnlyRootFilesystem: true` ·
`allowPrivilegeEscalation: false` · `capabilities.drop: [ALL]` · seccomp profile ·
only `workspace` and `tmp` volumes. `$HOME` is redirected to `/tmp/coder-home`
because the root filesystem is read-only.

---

## 6. Kata node provisioning stack

This is the part that took four fixes beyond flipping the feature flags. Red nodes
mark where it breaks if a prerequisite is missing.

```mermaid
flowchart TB
    FLAGS["enabled-addons.yaml<br/>kata_nodepool: true<br/>agent_sandbox_kata: true"] --> NP["Karpenter NodePool<br/>kata-nested"]
    NP --> NC["EC2NodeClass kata-nested<br/>subnet discovery by tag"]
    NC -->|"FIX 1<br/>kro-ack VPCs tag subnets<br/>services.k8s.aws/namespace,<br/>not platform.gitops.io/cluster"| SUB["subnets resolved"]
    SUB --> NODE["c8i.2xlarge · nested virt<br/>labels kata-enabled=true,<br/>katacontainers.io/kata-runtime=true<br/>taint kata=true:NoSchedule"]
    NODE -->|"FIX 2<br/>Auto Mode gives self-managed<br/>nodes no CNI or kube-proxy"| ADDONS["vpc-cni + kube-proxy<br/>EKS addons"]
    ADDONS --> READY["node Ready"]
    READY --> KD["kata-deploy DaemonSet<br/>nodeSelector kata-enabled=true"]
    KD -->|"FIX 3<br/>nydus snapshotter setup restarts<br/>containerd, drops CNI, crashloops"| SNAP["snapshotter.setup: []"]
    SNAP --> SHIMS["installs clh + qemu shims,<br/>removes runtime-not-ready taint"]
    SHIMS --> RC["RuntimeClasses<br/>kata-clh · kata-qemu · kata-fc<br/>(owned by agent-sandbox chart)"]
    RC --> WP["SandboxWarmPools<br/>coder-warmpool · doc-warmpool"]
    WP -->|"FIX 4<br/>idle loop only released on<br/>DF_ISSUE_NUMBER, not DF_MODE"| BOUND["warm pod binds a claim<br/>and starts the coder"]

    classDef fix fill:#fadbd8,stroke:#c0392b,color:#000
    class SUB,ADDONS,SNAP,BOUND fix
```

Each failure is silent in a different way: a missing subnet tag leaves the
NodeClass `SubnetsNotFound` with no node ever requested; missing CNI leaves the
node `NotReady` forever; the nydus crashloop leaves the `runtime-not-ready` taint
in place so pods stay `Pending`; and the idle-loop gap lets the pod bind
**successfully** and then idle while the hub polls for a file nobody is writing.

---

## 7. Identity and IAM

```mermaid
flowchart LR
    subgraph k8s["Kubernetes service accounts"]
        SAE["argo-events/<br/>dark-factory-specs-eventsource"]
        SAW["argo/<br/>dark-factory-doc"]
        SAS["crossplane-system/<br/>provider-aws-s3 · provider-aws-sqs"]
    end

    subgraph pi["EKS Pod Identity"]
        PIA["PodIdentityAssociation"]
    end

    subgraph iam["IAM roles"]
        RE["oap-hub-dark-factory-doc-events"]
        RW["oap-hub-dark-factory-doc-workflow"]
        RP["oap-hub-dark-factory-provider-s3 / -sqs"]
    end

    SAE --> PIA --> RE
    SAW --> PIA --> RW
    SAS --> PIA --> RP

    RE -->|"sqs:ReceiveMessage, DeleteMessage,<br/>GetQueueUrl, GetQueueAttributes"| Q["the one queue ARN"]
    RW -->|"s3:GetObject"| IN["specs bucket /specs/*"]
    RW -->|"s3:GetObject, PutObject, DeleteObject"| OUT["docs bucket /docs/*"]
    RP -->|"manage"| CR["the pipeline's own S3/SQS resources"]
```

Every policy is prefix-scoped — no bucket-wide or queue-wide grants exist, so no
bucket-wide capability can be delegated into a presigned URL. `DeleteObject` is
granted solely to clear a stale `FAILED` sentinel before a rerun.

---

## 8. GitOps control plane

How a commit becomes running infrastructure. This chain has **three independent
repo pointers**; missing any one leaves part of the stack pointed at upstream.

```mermaid
flowchart TB
    FORK["fork: jjdeo/open-agentic-platform<br/>branch feat/doc-pipeline"]

    subgraph sm["Secrets Manager · oap-hub/config metadata"]
        P2["fleetRepoURL / fleetRepoRevision"]
        P3["addons_repo_url / addons_repo_revision"]
    end
    P1["config.local.yaml · agenticRepo<br/>(local only, gitignored)"]

    FORK --- P1
    FORK --- P2
    FORK --- P3

    P1 -->|"task agentic:bootstrap"| APP1["Application<br/>agent-platform-addons"]
    P2 -->|"ExternalSecret<br/>fleet-secret-oap-hub"| CS["cluster secret<br/>enable_* labels from<br/>enabled-addons.yaml"]
    P3 -->|"registry/_defaults.yaml<br/>repoURLGit"| GEN["generated per-addon<br/>Applications"]

    APP1 --> GEN
    CS --> GEN
    GEN --> SYNC["ArgoCD syncs the BRANCH,<br/>so pushes flow through"]
    SYNC --> CLUSTER["live cluster"]

    classDef warn fill:#fdebd0,stroke:#b9770e,color:#000
    class P1,P2,P3 warn
```

After editing Secrets Manager the ExternalSecret must be nudged, or the change sits
until its refresh interval:

```bash
kubectl --context oap-hub annotate externalsecret fleet-secret-oap-hub \
  -n argocd force-sync=$(date +%s) --overwrite
```

ArgoCD here is the **EKS managed capability**, not an in-cluster install — there
are no ArgoCD pods to exec into, and it is reached at its AWS-managed endpoint
rather than through the platform CloudFront distribution.

---

## 9. AWS resources created by the chart

```mermaid
flowchart TB
    subgraph in["Input"]
        B1["Bucket oap-dark-factory-specs"]
        PAB1["PublicAccessBlock<br/>all four flags true"]
        SSE1["SSE-AES256"]
        NOT["BucketNotification<br/>prefix specs/ · suffix .md"]
        B1 --- PAB1 --- SSE1 --- NOT
    end
    subgraph q["Event transport"]
        QQ["Queue oap-hub-dark-factory-specs<br/>visibility 300s · retention 4d"]
        QP["QueuePolicy<br/>allows s3.amazonaws.com from this bucket"]
        QQ --- QP
    end
    subgraph out["Output"]
        B2["Bucket oap-dark-factory-docs"]
        PAB2["PublicAccessBlock<br/>all four flags true"]
        SSE2["SSE-AES256"]
        B2 --- PAB2 --- SSE2
    end
    NOT --> QQ
```

All nine resources are Crossplane managed resources and **must** carry
`argocd.argoproj.io/sync-options: SkipDryRunOnMissingResource=true` — without it a
first sync fails dry-run validation before the providers have installed their CRDs.

Argo Events has no AWS-S3 event source (its `s3` source speaks MinIO's protocol),
which is why SQS sits in the middle rather than S3 notifying the cluster directly.

---

## 10. Output validation and failure handling

The VM cannot be trusted to report its own success, so validation happens on both
sides of the boundary.

```mermaid
flowchart TB
    GEN["engine writes /workspace/out/index.html"] --> V1{"≥ 200 bytes?"}
    V1 -- no --> F["PUT FAILED sentinel<br/>with truncated reason"]
    V1 -- yes --> V2{"≤ 5 MiB?"}
    V2 -- no --> F
    V2 -- yes --> V3{"contains &lt;html?"}
    V3 -- no --> F
    V3 -- yes --> V4{"has interactive markers?<br/>script·button·input·select<br/>textarea·form·details·dialog"}
    V4 -- no --> F
    V4 -- yes --> PUT["PUT index.html"]

    PUT --> HEAD["hub HeadObject sees staging object<br/>→ copy to the named key, publish link"]
    F --> HEAD2["hub HeadObject sees FAILED<br/>→ workflow Failed, reason surfaced"]

    classDef bad fill:#fadbd8,stroke:#c0392b,color:#000
    class F,HEAD2 bad
```

The interactive-marker check exists because a model given a prose-heavy spec will
happily return **the spec itself** as a styled article, which satisfies every other
check. That shipped once: a 3,419-byte document passed validation and published a
working link while being entirely the wrong kind of artifact. The marker set is
deliberately broad — a CSS-only prototype using `details`/`:checked` is legitimate,
so requiring `<script>` alone would reject valid work.

---

## 11. Verified reference run

| Property | Value |
|---|---|
| Workflow | `df-doc-5bfb411dca837811` |
| Spec | three-view "Fleet Ops Console" — summary tiles, bar chart, sortable/filterable table with detail panel, maintenance kanban |
| Output | **34,888 bytes**, `text/html` |
| Contents | 1 script block, 12 `addEventListener`, ~50 functions, 1 table, 5 inline SVGs, all three views, every spec-named feature |
| External references | **0** |
| Link | presigned GET, HTTP 200 |
| Longest gateway request | 109 s (would have 504'd under the 30 s default) |

For contrast, the earlier prose rendering was 3,419 bytes with zero interactive
elements.

---

## 12. Known gaps

| Gap | Consequence |
|---|---|
| `dark-factory-oap-hub` reports `OutOfSync/Degraded` on the SQS Queue only | Cosmetic — queue is `Synced`/`Ready` and the pipeline runs through it, but this app's health is not a usable signal until fixed |
| No bucket lifecycle rule | One output directory per run, kept forever |
| No SQS redrive/DLQ | A repeatedly failing message is retried until retention expires |
| A sandbox dying without writing either object | Not detected early; the run waits out `await-output` |
| Presigned link lifetime capped by the Pod Identity credential | Hours, not the configured 12h. CloudFront + OAC is the durable alternative |
| Model-authored HTML served from an S3 origin | No CSP can be set on an S3 object; treat output as untrusted content |
| Coder failures are hard to diagnose | The CLI runs with `stdio: inherit`, so `err.stderr` is null and the sentinel carries no reason; the real error lives in a pod log that podGC deletes |
