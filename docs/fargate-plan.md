# Dialogue QC — AWS Fargate plan

Written 2026-07-24. Companion to `docs/lambda-serverless-plan.md`. Goal: run the **heavy QC
compute on-demand** (scale from zero) instead of paying for a big always-on EC2, while keeping
the current small EC2 as the always-on API.

---

## 1. Why Fargate here (and where it beats the Lambda plan)
QC analysis is **CPU/RAM-heavy and bursty**: idle most of the day (Teams chat, availability
checks), then a heavy run. The Teams path is **one episode × all delivered languages**, which on
`t3.medium` is ~15–25 min.

| | Lambda | **Fargate** |
|---|---|---|
| Max run time | **15 min hard cap** — a 6-language episode can exceed it | **no limit** |
| RAM / task | 10 GB cap | up to 120 GB |
| Ephemeral disk | 10 GB cap (deliveries are 3–6 GB) | up to 200 GB |
| Cold start | ~5–15 s | ~30–90 s (task launch) |
| Best at | massive fan-out (270 parallel, per-ms billing) | **long, chunky jobs; simplest "run my container"** |

**Verdict:** for the project's *primary* use (Teams-triggered per-episode runs), **Fargate fits
better** — no 15-min ceiling, generous RAM/disk, same container image, simpler than Step Functions.
Lambda still wins for the *rare* full 45×6 batch (fan-out); Fargate can do that too (parallel
tasks) at slightly higher cost/latency. Recommendation: **Fargate for on-demand runs now; keep the
Lambda fan-out as a later option for whole-show batches.**

## 2. Target architecture (hybrid — keep the small EC2 as the brain)
```
 Teams / Web UI
     │  @QC run ep 42
     ▼
 EC2 (t3.medium, always on)  — API + dispatcher + result store
     │  ECS RunTask (episode, series, languages)         │ reads status/results
     ▼                                                    ▼
 Fargate task (4–8 vCPU, 16 GB, ephemeral 40 GB)     S3: output/EP42_QC.zip
   backend/job_entry.py:                                 (workbook + ref audio)
     Box S2S fetch → VAD → alignment → voices →
     build_workbook + ref-audio → UPLOAD to S3
     (writes run record to DynamoDB or an S3 json)
```
- **EC2 stays** the always-on box (cheap): Teams webhook, availability checks, the signed
  download endpoint, and dispatch. It launches a Fargate task per heavy run instead of running
  the analysis in-process.
- **Fargate task** = the same `backend/` pipeline, one-shot, sized big so a run finishes in
  minutes; scales to zero between runs (**pay only while running**).
- **S3** holds job outputs; the download endpoint serves a **presigned S3 URL** (or streams from
  S3). Run status is a small record in S3/DynamoDB so it survives (this generalises today's
  `run_store.py`).

## 3. Code changes (small, isolated)
1. **`backend/job_entry.py`** (new) — reads `EPISODE/SERIES/LANGUAGES` from env, calls the
   existing `episode_runner.run(...)`, uploads the resulting zip + report to `s3://…/output/`,
   writes a status record. This is the Fargate task's command.
2. **Dispatch** — where `_teams_fast`/`agent.py` today call `jobs.submit(episode_runner.run…)`,
   add a `DQC_COMPUTE=fargate` branch that calls **ECS `run_task`** with env overrides and returns
   the task ARN as the job id. `DQC_COMPUTE=local` keeps today's in-process behaviour (desktop / EC2-only).
3. **`run_store` → S3/DynamoDB** — persist run records off-box so status/download work regardless
   of which EC2 process (or none) is up. `agent_dl` serves the S3 object via presigned URL.
4. **Dockerfile** (done) — one image for API and job.

Everything else (parser, VAD, alignment, content_map, voices, excel_report) is **reused verbatim**,
so Fargate results match the current reports.

## 4. AWS pieces to provision
- **ECR** repo + push the image (from CI or `docker build` + `aws ecr get-login`).
- **ECS cluster** (Fargate) + **task definition** (image, 4–8 vCPU / 16 GB, ephemeral 40 GB,
  `DQC_DATA_ROOT=/data`).
- **IAM**: task **execution role** (pull image, write logs) + task **role** (S3 read/write, read
  the Box/Anthropic secrets from Secrets Manager).
- **S3 bucket** (`output/` + short-lived `work/`), lifecycle rules.
- **Networking**: a subnet + security group with internet egress (public subnet +
  `assignPublicIp=ENABLED`, or a NAT) so the task reaches **Box, Anthropic, and S3**.
- **Secrets Manager**: Box client id/secret + refresh token, Anthropic key, Teams secret — the
  task reads them at start (instead of the EC2 `.env`).

## 5. The one hard problem: Box auth (same as the Lambda plan)
The current **single-use refresh-token** rotation is fine for one task at a time but races under
parallel tasks. Fix: a Box **Client Credentials Grant (CCG) service account** — every call mints a
fresh token, no shared state (**needs Box enterprise-admin approval**). Until then, one-task-at-a-time
dispatch with the existing token works.

## 6. Cost (rough)
- One on-demand episode run: 8 vCPU × 16 GB × ~5 min ≈ **$0.04–0.08** per run (Fargate:
  ~$0.04/vCPU-h + ~$0.004/GB-h). Nothing between runs.
- The always-on EC2 `t3.medium` stays (~$30/mo) as the brain — or shrink it to `t3.small`
  once compute is off-box.
- Full 45×6 as parallel Fargate tasks (cap ~50 concurrent): a few dollars, ~15–20 min.

## 7. Phases
- **Phase 0 — prereqs:** confirm the **AWS account can create ECS/ECR/S3/IAM** (it previously had
  an identity-review block on EC2 — verify this isn't gated); decide Box auth (CCG vs single-task).
- **Phase 1 — image:** `docker build` the Dockerfile, run it locally/EC2, confirm a QC run works in
  the container. *(safe, account-independent — the make-or-break packaging step)*
- **Phase 2 — one Fargate task end-to-end:** ECR push; task def; `run_task` for one episode;
  Box→VAD→S3. Validate RAM/disk/networking/secrets.
- **Phase 3 — dispatch + results:** wire `_teams_fast` to `run_task`; `run_store`/download via S3.
  Teams `run`→task, `status`→task+S3, download→presigned URL.
- **Phase 4 — harden:** one-task concurrency guard, CCG, cost alarm, log retention, task timeout.

**Effort:** ~1.5–3 focused days once the account is confirmed. The QC logic already exists and is
reused; the work is packaging + ECS wiring + moving state/outputs to S3.

## 8. Prerequisites / decisions needed before provisioning
1. **AWS account capability** — can it create ECS/ECR/S3/IAM/Secrets Manager, and do you have CLI
   or console access with those permissions? (The EC2 identity-review block, memory `aws-account`,
   may still restrict services.)
2. **Box auth** — pursue CCG service-account approval (clean) or ship one-task-at-a-time first?
3. **Keep EC2 as the API brain** (recommended) vs move the API to Fargate/App Runner too?

Related: `docs/lambda-serverless-plan.md`, `docs/aws-hosting-box-plan-v2.md`, memory `aws-account`, `ec2-deployment`.
