# JobPilot

**An MCP-powered job search agent with a production-style harness, verified tailoring and persistent memory.**

JobPilot finds roles from free job boards, scores them against your real profile, drafts a tailored CV without inventing experience, and tracks every application. It remembers preferences across sessions, shows every tool call, and never performs an external action without explicit approval.

The interface is a self-contained Flask app: no Node build step, no CDNs. Every account has its own workspace and private SQLite database. Chat is powered by OpenAI and requires an `OPENAI_API_KEY`; if it is missing, the app shows a setup page instead of faking an AI answer.

---

## Why this project exists

Searching for an entry-level AI engineering role is repetitive work: refreshing boards, comparing every description to your CV, rewriting bullets, remembering follow-ups. JobPilot automates the repetitive parts while keeping a human in control of anything that leaves the machine.

It is also a demonstration of four things:

| Pillar | What is implemented |
|---|---|
| **MCP** | Six typed stdio servers exposing 23 tools, discovered and invoked at runtime |
| **Harness** | Agent loop, budgets, permissions, approvals, guardrails, tracing, evals |
| **Context & memory** | Deliberate context assembly and a persistent memory policy you can inspect |
| **Multi-user isolation** | Password accounts, per-account databases, scoped MCP processes and sessions |

---

## Live-only by design

JobPilot has no demo mode, no sample jobs in the runtime and no deterministic chat fallback. A fresh account starts with a blank profile; job data comes from live public APIs and feeds.

| Situation | Behaviour |
|---|---|
| `OPENAI_API_KEY` missing | A setup page explains how to add it; page routes redirect, API routes return `503 model_not_configured` |
| No account signed in | Pages redirect to `/register` or `/login`; API routes return `401 auth_required` |
| Live source fails | The failure is reported on the System page; other sources continue |
| Model call fails | The error surfaces in the chat; it is never replaced by a fake answer |
| No profile facts | Tailoring has nothing to invent from — fill in the Profile page first |

The deterministic planner still exists, but only inside `evals/`. It is never imported by the runtime agent loop.

---

## Architecture

```text
Browser (Flask + Jinja + vanilla JS)
        │  JSON + SSE
        ▼
Flask app  ──  account session  ──  REST API  ──  approval bridge
        │
        ▼
Per-account workspace (ContextVar scopes every storage call)
  ├── data/users/<id>/jobpilot.db
  ├── data/users/<id>/traces/
  └── data/users/<id>/uploads/
        │
        ▼
Agent harness
  ├── planner .............. OpenAI tool-calling loop
  ├── context manager ...... profile + recalled memory + bounded history
  ├── permissions .......... read / write_local / external
  ├── approval broker ...... blocking human gate for external actions
  ├── guardrails ........... injection wrapping + CV claim verifier
  ├── tool executor ........ typed tool calls, summaries, traces
  ├── budget + tracing ..... per-run caps, JSONL traces, usage rows
  └── MCP manager .......... one stdio process group per account
        │
        ▼
MCP servers (23 tools)
  jobs · profile · memory · match · tailor · tracker
        │
        ▼
SQLite (WAL + FTS5)  ·  JSONL traces  ·  generated Markdown documents
```

### Request flow

1. A message arrives over `POST /api/chat` and is streamed back as SSE.
2. The Flask app resolves the signed-in account and scopes the request to that user.
3. The agent asks the model for tool calls using only that account's tools and data.
4. Every tool call is checked against its permission tier.
5. External tools block on an approval request shown in the chat timeline.
6. Results are summarized in the UI and written to the account's trace directory.
7. The final message and tool metadata are persisted to that account's SQLite database.

---

## Accounts and data isolation

- Accounts live in a shared SQLite file at `data/auth.db`; passwords are hashed with Werkzeug's PBKDF2 implementation.
- Every other table — profile, jobs, scores, chats, memories, applications, approvals, traces and model usage — lives in `data/users/<user_id>/jobpilot.db`.
- Flask sessions are signed cookies (`HttpOnly`, `SameSite=Lax`, `Secure` when `JOBPILOT_ENV=production`).
- Auth forms use a session-bound CSRF token; JSON writes require the `X-JobPilot` header.
- Each account gets its own MCP subprocess group, with database, trace and upload paths injected into the subprocess environment.
- A process-scoped `ContextVar` names the active account, so storage code cannot silently fall back to another user's data.

The CLI resolves an account automatically when exactly one exists. With several accounts, pass `--user EMAIL` (or the user id) to any account-scoped command.

---

## What you can do

- **Job board** - search live or filter the stored snapshot (`live=0`), with structured occupation, city/province/country, workplace, seniority, employment and posted-date filters. Remote cards state whether eligibility is confirmed, restricted to a country/region/city, or unknown.
- **Chat** — streamed tool timeline, interpreted filters, per-source status, job cards, approvals and memory proposals. The agent and the board share the same discovery pipeline.
- **Resumes** — upload PDF, DOCX or TXT during signup or from the Profile page, review and edit the extracted fields, apply them to the profile, switch between versions, download or delete. Text-based PDFs are parsed locally; scanned PDFs ask for manual text. Personalization is opt-in.
- **Applications** — Kanban pipeline with drag/drop stages, follow-up dates, notes and a full event history.
- **Memory** — inspect, add, confirm, forget and test recall.
- **Profile** — structured identity, preferences, experience, education, projects and skills.
- **System** — mode, spend, database, MCP health, source probes, traces and token usage.

---

## Quickstart

Requirements: Python 3.11+ (tested on 3.12), Windows/macOS/Linux.

### Windows PowerShell

```powershell
git clone https://github.com/your-username/jobpilot.git
cd jobpilot

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,eval]"

Copy-Item .env.example .env
notepad .env   # set OPENAI_API_KEY=sk-... and a real JOBPILOT_SECRET_KEY
jobpilot serve --open
```

### macOS / Linux

```bash
git clone https://github.com/your-username/jobpilot.git
cd jobpilot

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,eval]"

cp .env.example .env
nano .env   # set OPENAI_API_KEY=sk-... and a real JOBPILOT_SECRET_KEY
jobpilot serve --open
```

The app starts on `http://127.0.0.1:5000`.

1. Register the first account at `/register` (optionally attaching a resume).
2. Fill in the Profile page, or review the resume extraction and apply it. Nothing is prefilled.
3. Open **Job board** and fetch live jobs, or ask the chat: **“Find AI Engineer jobs”**.

On first run the app creates `data/auth.db` and the account's private workspace. No profile, job or application data is seeded.

Useful commands:

```bash
jobpilot serve --port 5055          # custom port
jobpilot doctor --sources           # probe the free job sources
jobpilot doctor --mcp               # discover the MCP tools
jobpilot search "AI Engineer"       # fetch + rank from the CLI
jobpilot eval                       # offline evaluation, no API key needed
```

---

## Configuration

Copy `.env.example` to `.env`. Every setting can also be supplied as an environment variable.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | empty | **Required for chat.** Get it at `platform.openai.com/api-keys` |
| `OPENAI_BASE_URL` | empty | Optional OpenAI-compatible endpoint |
| `JOBPILOT_MODEL_FAST` | `gpt-4o-mini` | Routing and cheap scoring |
| `JOBPILOT_MODEL_STRONG` | `gpt-4o` | Tailoring and complex reasoning |
| `JOBPILOT_EMBEDDING_PROVIDER` | `auto` | `auto`/`hashing` are local and free; `openai` is opt-in and paid |
| `JOBPILOT_SECRET_KEY` | dev value | Signs session cookies; set a real random value |
| `JOBPILOT_SESSION_DAYS` | `14` | Signed-in session lifetime |
| `JOBPILOT_ALLOW_REGISTRATION` | `true` | Allow new accounts from `/register` |
| `JOBPILOT_MAX_BUDGET_USD` | `2.00` | Lifetime development cap |
| `JOBPILOT_DAILY_BUDGET_USD` | `1.00` | Per-day cap; the gateway refuses calls above it |
| `JOBPILOT_MAX_STEPS_PER_RUN` | `12` | Tool-loop guard |
| `JOBPILOT_DATA_DIR` | `data` | Root for auth storage and per-account workspaces |
| `JOBPILOT_AUTH_DB_PATH` | `data/auth.db` | Shared account database |
| `JOBPILOT_DB_PATH` | `data/jobpilot.db` | Base database path used by the CLI/tests |
| `JOBPILOT_TRACES_DIR` | `data/traces` | Base trace path |
| `JOBPILOT_UPLOADS_DIR` | `data/uploads` | Base upload path |
| `JOBPILOT_PRELOAD_MCP` | `false` | Warm MCP servers for existing accounts at startup |
| `JOBPILOT_ENABLE_REMOTIVE` / `ARBEITNOW` / `REMOTEOK` / `JOBICY` / `HIMALAYAS` / `RSS` | `true` | Free source adapters |
| `JOBPILOT_ENABLE_LINKEDIN` | `true` | Public LinkedIn guest search, no sign-in |
| `JOBPILOT_ENABLE_MUSTAKBIL` | `true` | Mustakbil Pakistan category/city pages |
| `JOBPILOT_ENABLE_TALENT` | `true` | Talent.com Pakistan search |
| `JOBPILOT_ENABLE_INDEED` | `true` | Best-effort Indeed RSS/HTML; usually blocked by Cloudflare |
| `JOBPILOT_HTTP_TIMEOUT` | `20` | Source request timeout in seconds |

Set a real `JOBPILOT_SECRET_KEY` before exposing the app beyond localhost. The web app redirects the base storage paths into the signed-in account's workspace; the base paths are mainly for CLI and test runs.

---

## CLI reference

```text
jobpilot serve   [--host H] [--port P] [--debug] [--open]
jobpilot doctor  [--sources] [--mcp] [--user EMAIL]
jobpilot chat    [--user EMAIL]
jobpilot search  [QUERY] [--location L] [--remote] [--limit N] [--no-refresh] [--user EMAIL]
jobpilot tailor  JOB_ID [--no-llm] [--user EMAIL]
jobpilot track   JOB_ID [--status STATUS] [--notes TEXT] [--user EMAIL]
jobpilot eval
```

`serve` uses the Flask development server with `--debug` and Waitress otherwise.

---

## MCP servers

Each server is a separate Python process launched over stdio with the official MCP SDK. Sessions are persistent on a private asyncio loop, and a failed server does not take down the rest. Processes are isolated per account.

| Server | Tools |
|---|---|
| `jobs` | `search_jobs`, `get_job`, `refresh_jobs`, `add_job_from_url` |
| `profile` | `get_profile`, `search_experience`, `list_skills`, `get_active_resume` |
| `memory` | `remember`, `recall`, `list_memories`, `forget`, `confirm_memory` |
| `match` | `score_job`, `rank_jobs`, `skill_gap` |
| `tailor` | `tailor_cv`, `draft_cover_letter` |
| `tracker` | `add_application`, `update_status`, `list_applications`, `list_followups`, `mark_submitted` |

Inspect live discovery:

```bash
jobpilot doctor --mcp
```

The System page shows the same count and any failed server.

---

## Search pipeline

Chat and the job board call the same `DiscoveryService`. A request is interpreted first (occupation, location, workplace, seniority, employment), then live sources are fetched when the cached snapshot is stale, and the same filter/ranking code produces both surfaces. There is no separate board-only filter that the agent can bypass.

- Results are cached per interpreted request for 10 minutes; failed retrievals are cached for 90 seconds so a broken source is not hammered.
- Every response reports `retrieved_at`, whether it came from cache, how many postings were fetched, and per-source status (ok / empty / error with the reason).
- Occupation classification is title-first. A company description mentioning accounting cannot turn an engineering role into an accounting match, and ambiguous multi-role titles become `other` rather than a false exact hit.
- Related occupations are labelled `related` and are only shown when the query is broad or the posting matches every meaningful query term.
- Location data is structured: `workplace_type`, `job_location`, `company_location`, `eligible_locations`, `remote_scope`, `location_confidence`. A remote job with no stated eligibility is **not** treated as a match for a specific city.
- The board passes `live=0` while the user types, so keystrokes filter the stored snapshot instead of hitting every source. Pressing **Search live jobs** performs a real retrieval with the same filters.

---

## Ranking

Matching is deterministic by default and combines:

- embedding similarity between the job and the profile;
- skill overlap and missing-skill penalties;
- title-family fit, which recognises AI/ML/LLM/RAG/NLP variants and caps roles that only share a generic word like "Engineer";
- location and remote preference;
- seniority fit;
- salary fit;
- exclusions from the profile (`excluded_companies`, `excluded_keywords`, `skills_to_avoid`).

Scores are cached per profile version. Editing the profile invalidates them naturally because the version is a content hash. Ranking considers a wide candidate pool rather than only the newest postings, so publication time cannot decide the winner. When a model key is configured, live scoring can add an LLM assessment without losing the transparent breakdown.

Try it:

```bash
jobpilot search "AI Engineer" --location "Islamabad, Pakistan"
jobpilot search "LLM Engineer" --remote --no-refresh
```

---

## CV tailoring and the claim verifier

Tailoring is evidence-first:

1. Select the profile bullets with the strongest overlap with the job.
2. Assemble headline, summary, experience, keywords and cover letter.
3. Run every factual claim through `ClaimVerifier`.
4. Reject unsupported claims rather than softening them.
5. Save Markdown to the account's `uploads/` directory and report `verified: true` only when all claims pass.

The model drafts the text, but the verifier checks token coverage against the profile fact corpus, numeric claims, and named technology entities. It does not trust the LLM, which is the point. If the model call fails or the budget is exhausted, tailoring falls back to evidence-selected bullets and still runs the verifier.

From the CLI:

```bash
jobpilot tailor job_cc13d5fe4dc357
jobpilot track job_cc13d5fe4dc357 --status applied
```

---

## Resumes

A resume can be uploaded when the account is created or later from **Profile → Resumes**. Files are validated by extension, content signature and size (PDF, DOCX and TXT up to 8 MB), hashed, and stored in the account's private `uploads/` directory.

Extraction runs locally: `pypdf` reads text-based PDFs, `python-docx` reads DOCX, and plain text is read directly. The review dialog shows the parsed name, headline, contact details, links, location, skills, experience, education and projects. Nothing is applied until the user confirms, and **Apply to profile** merges fields without deleting existing experience, education or projects.

- Multiple versions are supported; exactly one is active at a time.
- Activate, download and delete are available per version.
- Scanned/image-only PDFs have no text layer. When no OCR engine is available the upload is stored with an explicit error and the UI asks for pasted text — JobPilot never invents resume content.
- Resume personalization is opt-in. The active resume is exposed to the agent as `get_active_resume`, but its text is treated as user data, not instructions.

API surface (all require the signed-in account; writes need `X-JobPilot: 1`):

```text
GET    /api/resumes
POST   /api/resumes                 multipart file, optional activate
GET    /api/resumes/<id>
GET    /api/resumes/<id>/file
PUT    /api/resumes/<id>            edit reviewed fields
POST   /api/resumes/<id>/activate
POST   /api/resumes/<id>/apply      merge selected fields into the profile
DELETE /api/resumes/<id>
```

---

## Persistent memory

Memory is intentionally boring and auditable.

| Memory kind | Behaviour |
|---|---|
| Explicit (`remember that ...`) | Stored immediately as active |
| Episodic (something that happened) | Stored immediately, used as context |
| Inferred semantic / profile-affecting | Parked as `needs_confirmation` until the UI confirms it |
| Low confidence | Parked rather than silently applied |
| Profile edits | Always win over stale memories |

Recall ranks by embedding similarity, recency and importance. The Memory page exposes the full lifecycle: active, pending, forgotten, plus a recall preview showing exactly what the agent would retrieve.

---

## Source adapters

| Adapter | Notes |
|---|---|
| Remotive | Free remote-job API |
| Arbeitnow | Free job board API |
| RemoteOK | Free JSON feed |
| Jobicy | Free remote-job API, credited with a direct link to the source |
| Himalayas | Free remote-job feed |
| RSS | Public feeds, with HTML sanitisation |
| Manual / JSON-LD | Paste a URL and extract structured job data |
| LinkedIn | Public guest job-search pages only; no account, cookie or application |
| Mustakbil | Pakistani technology, city and remote category pages |
| Talent.com | Pakistan search with detail-page descriptions |
| Indeed | Best-effort public RSS/HTML; most networks receive Cloudflare HTTP 403 |

Sources fan out concurrently. Priority only breaks duplicate ties; identity comes from a normalized fingerprint of title, company and location. Job descriptions are treated as untrusted data and wrapped before they reach a model.

**Known limits:** Indeed, Rozee.pk, Bayt and Careerjet currently answer automated requests with Cloudflare 403 from this project's test network, and there is no free Indeed API. The Indeed adapter reports that block explicitly. Mustakbil's keyword endpoint returns HTTP 410 and its category pages do not expose pagination, so the adapter reads the first page of each technology/city category. LinkedIn is read through its public guest endpoints with a delay between requests; disable it with `JOBPILOT_ENABLE_LINKEDIN=false` if you prefer not to use those pages.

---

## Evals

`jobpilot eval` runs a fully offline evaluation against a hand-graded corpus of 10 synthetic roles. It uses a temporary database and never calls a paid model. The corpus lives in `src/jobpilot/evals/data/` and is not part of the runtime product.

Current report (`eval/latest.md`):

| System | Precision@5 | nDCG@5 | Spearman | MAE |
|---|---:|---:|---:|---:|
| JobPilot hybrid | 1.00 | 0.98 | 0.89 | 3.5 |
| Keyword baseline | 1.00 | 0.90 | 0.84 | 37.8 |
| TF-IDF baseline | 1.00 | 0.86 | 0.68 | 56.4 |

The harness also checks:

- every tailored claim is supported (10/10 on the top role);
- injection detection and untrusted-content escaping;
- deterministic tool selection for six common prompts;
- memory recall top-1.

```bash
jobpilot eval
# Markdown + JSON written to eval/latest.md and eval/latest.json
```

Thresholds fail the command when ranking, claim support, guardrails, tool routing or memory regress.

---

## Tests

```bash
pytest
ruff check src tests
```

The suite covers database migrations, auth and cross-account isolation, salary/skill extraction, occupation classification (title-first, with ambiguity fallback), city/province/country and postal-code disambiguation, remote eligibility, discovery caching and source failures, resume validation/extraction/activation, dedupe fingerprints, ranking, tailoring, tracking, memory policy, approvals, all page routes, JSON/SSE-adjacent API behaviour, profile round-trips and the eval harness.

---

## Project structure

```text
src/jobpilot/
  core/        settings, auth, user context, Pydantic models, SQLite schema
  sources/     concurrent free adapters + registry
  pipelines/   extraction, occupation intent, location/remote resolution, dedupe, embeddings
  services/    jobs, discovery, resume, matching, tailoring, tracking, chat, profile, doctor
  memory/      store, policy, summarizer, embedding backends
  harness/     loop, MCP client, executor, permissions, guardrails, tracing
  servers/     six MCP stdio servers
  ui/          Flask app, API, CLI, templates, CSS, JavaScript
  evals/       synthetic fixtures, dataset, baselines, metrics, runner
tests/         pytest suite
eval/          generated latest.md / latest.json
data/          auth.db + per-account workspaces (gitignored)
```

---

## Database

Each account has its own SQLite database in WAL mode with per-operation connections, so Flask threads and MCP subprocesses do not share handles. The schema is created and migrated idempotently on first use:

`profile`, `experiences`, `education`, `projects`, `skills`, `resumes`, `jobs`, `job_scores`, `applications`, `application_events`, `memories`, `vectors`, `chat_sessions`, `chat_messages`, `search_runs`, `llm_cache`, `llm_usage`, `approvals`, `source_health`, `kv`, plus `jobs_fts` for full-text search.

The shared `data/auth.db` stores only account records. See `data/README.md` for the layout and reset steps.

---

## Security and privacy

- Local-first: profiles, jobs and application history stay in per-account SQLite files.
- Passwords are hashed; sessions are signed, `HttpOnly` cookies with `SameSite=Lax`.
- Auth forms use CSRF tokens; mutating API calls require the `X-JobPilot` header.
- Account data is isolated by database, by scoped MCP processes and by an explicit request context.
- No external assets or CDNs; the CSP only allows self-hosted scripts and styles.
- External actions (`mark_submitted`) block on an explicit approval, persisted and visible after reload.
- LLM calls are budget-capped daily and per run. The app refuses to chat without a key.
- Untrusted job text is wrapped and screened for injection patterns.
- LinkedIn is read only through public guest pages; JobPilot never signs in, applies or messages on your behalf.

**Limitations:** this is a local-first, single-server application. It does not include email verification, password reset, OAuth, TLS termination or rate limiting, and it is not a hardened multi-tenant deployment for the public internet. Each active account spawns its own MCP subprocess group, which is fine locally but memory-heavy at scale. Docker is intentionally deferred until the repository is ready to publish.

---

## Roadmap

- Dockerfile and compose profile (deferred until release).
- Optional local embedding model as a default.
- More source adapters and saved searches.
- Calendar and email follow-up integrations behind the same approval gate.
- Broader eval datasets and live-model scoring comparisons.

---

## License

MIT. See `pyproject.toml` for package metadata.
