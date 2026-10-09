# Section 01 - Written answers

**1. Why separate create and status tools?**
Generation takes 5–20 s and fails about 1 time in 10. A blocking tool hits client timeouts, and a dropped connection loses the result while the job keeps costing money. A job id survives retries and disconnects, so the agent can run jobs in parallel, cancel them, and report progress. `get_job_status(wait_seconds≤30)` keeps the convenience of waiting without those risks.
At **5 minutes**, the tools stay the same, but the agent saves the job id in the task state and is resumed later instead of polling in its loop. At **20 minutes (video)**, the service pushes instead of being polled: a webhook resumes the agent, the status reports progress, and a person approves before an expensive job starts.

**2. Links or image data?**
Links: short-lived signed URLs with dimensions. Base64 for four 2k images would be megabytes, flooding the model's context and wasting tokens. The next tools (screen upload, email design) take URLs anyway; expiry limits leak damage. A small thumbnail is the exception, for when the agent must check a result visually.

**3. What to limit or never expose?**
Limits: at most 4 images per job, prompts of at most 500 characters, an hourly image budget, at most 5 concurrent jobs per tenant, and HTTP 429 with a retry time. Never exposed: API keys (read from the environment and redacted from logs), the tenant (derived from the key, never from an argument), other tenants' jobs, raw model settings, billing, and bulk deletion. In production, image URLs come from an allowlist, so the server can't be used to fetch arbitrary URLs (SSRF).
