# Nitx Poster MCP Server

An MCP (Model Context Protocol) server that lets an AI agent generate advertising posters, plus a mock backend that behaves like a real asynchronous poster service.

```
 AI agent / MCP client ──MCP (stdio or HTTP)──► poster-mcp ──HTTPS + Bearer key──► mock poster backend
 (Claude Desktop, n8n…)                          4 tools, retries,                   5–20 s jobs, ~10% failures,
                                                 idempotency, budget                 validation, tenants
```

## Quick start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd nitx-agent
uv sync
cp .env.example .env          # then set the same secret in MOCK_API_KEYS and POSTER_API_KEY
uv run pytest                 # 27 tests, ~15 s
```

Run the two processes in separate terminals:

```bash
set -a; source .env; set +a
uv run poster-mock-backend                       # http://127.0.0.1:8080
uv run poster-mcp --transport http --port 8000   # MCP endpoint: http://127.0.0.1:8000/mcp
```

### Connect an MCP client

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

## Tools

| Tool | What it does | Notes for the agent |
|---|---|---|
| `list_templates(category?, search?)` | Browse templates | Read-only. `category` is an enum. |
| `create_poster_job(product_image, background_image, prompt, image_count=1, resolution="1k", template_id?, idempotency_key?)` | Start a job and return at once with `job_id`, `status="processing"` | Safe to retry: same key + same inputs → same job. |
| `get_job_status(job_id, wait_seconds=0..30)` | `processing` / `completed` (+ `images`) / `failed` (+ `error`) / `cancelled` | `wait_seconds` long-polls on the server, so the agent doesn't spin. |
| `cancel_job(job_id)` | Stop a running job | Finished jobs return `JOB_ALREADY_FINISHED`. |

Every successful result carries a **`next_step`** field, and every error states **what happened, whether anything was created, and the exact next call**. For example:

> `PROMPT_TOO_LONG: prompt is 612 characters; the maximum is 500. No job was created. Next step: shorten the prompt (keep product, scene and any headline text; drop filler adjectives) and call create_poster_job again.`

> `SERVICE_UNREACHABLE (TIMEOUT): … The job MAY have been created. Next step: call create_poster_job again with the SAME arguments and idempotency_key="auto-3f…" - it returns the existing job instead of creating a duplicate.`

## Design decisions

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

### Assumptions
- `product_image` and `background_image` are both required and are http(s) URLs, as in the contract. Uploading files is out of scope.
- `image_count` defaults to 1 and `resolution` to `"1k"`, the cheapest valid values.
- Failures: two kinds are retryable (`GPU_TIMEOUT`, `RENDER_ERROR`) and one is not (`PRODUCT_SEGMENTATION_FAILED`).
- The mock computes job state from timestamps, so it needs no background workers and tests can control time.

## Tests

`uv run pytest` runs 27 tests. Each one goes **MCP client → MCP server → HTTP client → mock backend** in-process.

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

## Transcript

_A recorded transcript of an LLM agent using these tools is in [transcripts/](transcripts/)._

## Repository layout

```
src/poster_mcp/
  server.py    MCP tools, agent-facing errors, budget, transports
  client.py    HTTP client: auth, retries, error classification, log redaction
  backend.py   mock poster service (FastAPI)
tests/         27 tests
docs/          written answers
```
