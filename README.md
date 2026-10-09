# Nitx Applied AI Assessment — Poster MCP Server & Campaign Agent

Yousef Gabr's submission for Sections **01** (MCP server), **02** (Campaign Agent), **05** (Evaluation & Safety) and **08** (bonus: agent end to end).
The written parts (01 answers, 02 design, 05 evaluation & safety) are in **[docs/Nitx-AI-Assessment-Yousef-Gabr.pdf](docs/Nitx-AI-Assessment-Yousef-Gabr.pdf)**.

```
             brief                          MCP (Streamable HTTP)
 Customer ─────────► n8n workflow ──────────────────────────────► poster-mcp (4 tools) ───┐
    ▲                  1 open campaign      ┌─ Plan               campaign-mcp (13 tools) ─┤ Bearer key
    │                  2 AI Agent (DeepSeek)┤  Act                                         ▼
    │                  3 read real state    └─ Check            Mock Nitx platform: posters, screens, email,
    └── reply + ──────  4 verify reply vs state                  drafts only, audit log, approval page
        verified status + approval link                                            ▲
                                                 a person approves (separate token) ┘ → goes live
```

| Section | Where |
|---|---|
| 01 MCP server + mock backend | [`src/poster_mcp/`](src/poster_mcp/), tests in [`tests/test_server.py`](tests/test_server.py), [`tests/test_backend.py`](tests/test_backend.py), answers in [`docs/01-answers.md`](docs/01-answers.md) |
| 02 Campaign Agent (working prototype) | [`n8n/`](n8n/) (workflow, [system prompt](n8n/system-prompt.md), [verifier](n8n/verify_report.js)), [`src/campaign_mcp/`](src/campaign_mcp/) |
| 05 / 08 Evaluation | [`evals/run_evals.py`](evals/run_evals.py), results in [`evals/results/`](evals/results/) |
| Transcripts | [`transcripts/`](transcripts/) |

## Quick start — the whole stack

Requirements: Docker with Compose, plus [uv](https://docs.astral.sh/uv/) for the tests and evals. You also need a DeepSeek API key ([platform.deepseek.com](https://platform.deepseek.com/api_keys)); a full run costs a fraction of a cent.

```bash
./scripts/init-env.sh            # creates .env with random local secrets
# edit .env and set DEEPSEEK_API_KEY=...
docker compose up -d --build     # platform :8080, poster-mcp :8000, campaign-mcp :8001, n8n :5678
```

Send a brief:

```bash
curl -s localhost:5678/webhook/campaign-agent -H 'content-type: application/json' \
  -d '{"brief": "Run a weekend sale for our coffee-shop chain. Make Arabic posters, show them on our in-store screens Thursday to Saturday from 10:00 to 16:00, and email our loyalty customers on Thursday at 9:00."}' | jq
```

The response contains the agent's `reply`, a `verified_by_platform` block read from the platform after the agent finished, any `warnings` (mismatches between the reply and reality), the `approval_url`, and every tool call in `steps`.
To continue a conversation (for example, to answer the agent's question), send the same `session_id` and `campaign_id` back.

**Approve:** open the `approval_url` (http://localhost:8080/approve/…) and enter `APPROVER_TOKEN` from `.env`. Only then do the drafts become `scheduled`.

**See the workflow:** open http://localhost:5678, create the local owner account, and open *Nitx Campaign Agent*. Every run appears under *Executions*, with each tool call's input and output.

**Run the tests and evals:**

```bash
uv sync
uv run pytest                          # 35 tests, no Docker or LLM needed
uv run python evals/run_evals.py       # 14 agent cases against the running stack → PASS/FAIL table
```

## Section 01 — Poster MCP server

The server runs on its own too, without Docker or n8n:

```bash
uv sync
set -a; source poster.env.example; set +a
uv run poster-mock-backend                       # terminal 1: http://127.0.0.1:8080
uv run poster-mcp --transport http --port 8000   # terminal 2: MCP endpoint http://127.0.0.1:8000/mcp
```

#### Connect an MCP client

**HTTP clients (n8n, MCP Inspector):** use `http://127.0.0.1:8000/mcp` (Streamable HTTP). If the client runs in Docker, add its host name to `MCP_ALLOWED_HOSTS` (e.g. `MCP_ALLOWED_HOSTS=poster-mcp:8000`).

**Claude Desktop (stdio):** add this to `claude_desktop_config.json` and keep the mock backend running:

```json
{
  "mcpServers": {
    "nitx-poster": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/nitx-agent", "run", "poster-mcp"],
      "env": { "POSTER_API_KEY": "<same key as MOCK_API_KEYS>", "POSTER_API_BASE_URL": "http://127.0.0.1:8080" }
    }
  }
}
```

### Tools

| Tool | What it does | Notes for the agent |
|---|---|---|
| `list_templates(category?, search?)` | Browse templates | Read-only. `category` is an enum. |
| `create_poster_job(product_image, background_image, prompt, image_count=1, resolution="1k", template_id?, idempotency_key?)` | Start a job and return at once with `job_id`, `status="processing"` | Safe to retry: same key + same inputs → same job. |
| `get_job_status(job_id, wait_seconds=0..30)` | `processing` / `completed` (+ `images`) / `failed` (+ `error`) / `cancelled` | `wait_seconds` long-polls on the server, so the agent doesn't spin. |
| `cancel_job(job_id)` | Stop a running job | Finished jobs return `JOB_ALREADY_FINISHED`. |

Every successful result carries a **`next_step`** field, and every error states **what happened, whether anything was created, and the exact next call**. For example:

> `PROMPT_TOO_LONG: prompt is 612 characters; the maximum is 500. No job was created. Next step: shorten the prompt (keep product, scene and any headline text; drop filler adjectives) and call create_poster_job again.`

> `SERVICE_UNREACHABLE (TIMEOUT): … The job MAY have been created. Next step: call create_poster_job again with the SAME arguments and idempotency_key="auto-3f…" - it returns the existing job instead of creating a duplicate.`

### Design decisions

| Concern | How it is handled |
|---|---|
| **Slow jobs** | Create returns immediately. `get_job_status` long-polls for up to 30 s. A job that is still processing gets an ETA, plus guidance to cancel after about 2 minutes. |
| **Failed jobs** | `error.code`, `error.message` and `error.retryable`. Retryable failures say "retry once with a NEW idempotency_key". Input failures (e.g. product segmentation) say "don't retry unchanged". |
| **Timeouts & 5xx** | Up to 2 retries with exponential backoff, for reads, cancels, and creates that carry an idempotency key (all of them). 4xx errors are never retried. |
| **Duplicate jobs** | The `Idempotency-Key` header is stored per tenant for 24 h. If the agent omits a key, the server derives one from a hash of the inputs, so a blind retry is deduplicated. Reusing a key with different inputs → `409 IDEMPOTENCY_KEY_REUSED`. |
| **Credentials** | Read only from `POSTER_API_KEY` (env). Never a tool argument and never in output. A logging filter redacts the key from every log record. Logs go to stderr. |
| **Tenant isolation** | The tenant is derived from the API key, never from arguments. Another tenant's job id returns `JOB_NOT_FOUND`, the same as a non-existent id, so ids can't be probed. |
| **Cost / abuse** | At most 4 images per job and prompts of at most 500 characters (in the schema and enforced). The server has an hourly image budget (`POSTER_MAX_IMAGES_PER_HOUR`); replays are free. The backend allows at most 5 concurrent jobs per tenant (`429` + `Retry-After`). |
| **Images** | Short-lived signed URLs with width/height, never base64 (see [docs/01-answers.md](docs/01-answers.md)). |

#### Assumptions
- `product_image` and `background_image` are both required and are http(s) URLs, as in the contract. Uploading files is out of scope.
- `image_count` defaults to 1 and `resolution` to `"1k"`, the cheapest valid values.
- Failures: two kinds are retryable (`GPU_TIMEOUT`, `RENDER_ERROR`) and one is not (`PRODUCT_SEGMENTATION_FAILED`).
- The mock computes job state from timestamps, so it needs no background workers and tests can control time.

### Tests

`uv run pytest tests/test_server.py tests/test_backend.py` runs the 27 Section 01 tests. Each one goes **MCP client → MCP server → HTTP client → mock backend** in-process.

| Area | Tests |
|---|---|
| Discovery | tool list and JSON-schema constraints (`maxLength`, `minimum/maximum`, `enum`, `required`) |
| Happy path | template filtering; job goes processing → completed with N images at the right resolution |
| Validation | prompt > 500 chars; `image_count` 0 / 5 / -1; unknown template; bad resolution and non-http URL (backend) |
| Idempotency | same key → same job; no key → still deduplicated; key reused with other inputs → refused; **response lost after the server created the job → retry returns the original, not a duplicate** |
| Failures | timeout → "MAY have been created, retry with the SAME key" (3 attempts made); failed job → reason + `retryable` + recovery step; long job → still processing + cancel guidance |
| Cancel | cancel running job; cancel finished job → explanatory error |
| Security | another tenant can't read or cancel a job; API key never in tool output or logs; wrong key → `AUTH_FAILED` without echoing it; missing key → fails fast |
| Limits | hourly budget blocks runaway generation (replay stays free); concurrent-job limit → `RATE_LIMITED` |
| Mock fidelity | default jobs take 5–20 s; ~10% of 1,000 jobs fail; invalid inputs rejected; requests without a key refused |

## Section 02 / 08 — Campaign Agent

**Shape:** a single agent (DeepSeek `deepseek-chat`, ≤40 iterations) wrapped in deterministic n8n steps. The workflow opens the campaign before the agent runs, so every draft is tagged with its id, and it reads and verifies the real state after the agent finishes. The full design is in the PDF.

**Safety comes from the platform, not the prompt:**

| Rule | Enforced by |
|---|---|
| Nothing is published or sent without a person | There is no publish/send tool. Drafts go live only from the approval page, with `APPROVER_TOKEN`, which the agent never holds. |
| Never touch another customer's data | The tenant comes from the API key; other tenants' ids return "not found". |
| No contact-list leaks | Segments return counts only, and email campaigns have no recipient field. |
| Only real posters on public screens | Screen and email images must be completed poster jobs of this tenant. |
| Honest reporting | The verifier node compares the reply with the platform's state and adds warnings. |
| Prompt injection in documents | Documents come back as `untrusted_content`, and instruction-like lines are flagged. |

**Campaign tools** (`campaign-mcp`): `get_campaign`, `list_media_assets`, `list_brand_documents`, `read_brand_document`, `list_screens`, `upload_screen_content`, `create_screen_sequence`, `create_screen_schedule`, `build_audience_segment`, `design_email`, `schedule_email_campaign`, `submit_for_approval`, `get_approval_status`.

**Mock data** (tenant `demo`, "Sabah Coffee / قهوة الصباح"): 6 screens in Riyadh and Jeddah (in-store and drive-thru), 1,200 synthetic contacts (40% loyalty, 90% opted in), 5 media assets, and 2 brand guides, one of them carrying a hidden prompt injection. A second tenant, `rival`, exists for isolation tests.

**Rebuilding the workflow:** edit [`n8n/system-prompt.md`](n8n/system-prompt.md) or [`n8n/verify_report.js`](n8n/verify_report.js), then run `uv run python n8n/build_workflow.py && docker compose run --rm n8n-init && docker compose restart n8n`.

## Section 05 / 08 — Evaluation

`uv run python evals/run_evals.py` runs 14 cases (`--only <id>` for one, `--repeat N` to measure flakiness). Before each case it resets the platform and injects faults through an admin API, and it grades the **platform's state** (schedules, statuses, tool arguments), not the agent's prose.

| Category | Cases |
|---|---|
| Happy path / constraints | `clear_brief`, `specific_screens_no_email`, `clarify_then_complete` (2 turns) |
| Tool failures | `poster_fails_then_recovers`, `poster_fails_twice`, `scheduler_down_after_posters` |
| Ambiguity | `ambiguous_brief`, `contradictory_dates` |
| Malicious input | `injection_in_brand_guide`, `injection_in_brief`, `external_email_address` |
| Isolation | `other_customer_data` |
| Honesty / cost | `no_invented_offer`, `cost_limits` |

## Repository layout

```
src/poster_mcp/      Section 01: MCP server (server.py), HTTP client + redaction (client.py), mock backend (backend.py)
src/campaign_mcp/    Campaign MCP server (server.py) and the mock Nitx platform (platform.py)
n8n/                 workflow generator, system prompt, verifier, init script, generated workflow JSON
evals/               agent evaluation suite and saved results
tests/               35 unit/integration tests (no Docker, no LLM)
docs/                PDF report and its source, Section 01 answers
transcripts/         recorded agent runs
```

## AI tools used

Claude Code (Anthropic) was my pair-programmer. It scaffolded the code, tests, n8n workflow JSON and Docker setup, and helped edit the documents. I specified the architecture and safety design, reviewed every change, and ran and debugged everything locally. DeepSeek (`deepseek-chat`) is the model inside the agent.
