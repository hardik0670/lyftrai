# Lyftr Chat

A React chat app with a FastAPI backend. SQLite stores each conversation, and LiteLLM handles model calls, key failover, and response caching.

## Live URLs

| Service | URL |
| --- | --- |
| Frontend | [lyftrai.vercel.app](https://lyftrai.vercel.app) |
| Backend | [lyftrai-api.vercel.app](https://lyftrai-api.vercel.app) · [Health check](https://lyftrai-api.vercel.app/health) |

### Deploy on Vercel Hobby

The frontend and API use separate Vercel projects on Hobby. The API runs as a Python function and uses SQLite under `/tmp`. Each instance has its own database and LiteLLM cache. A cold start on a new instance, scale-out, or a redeployment can reset chat context and cache hits. The local Docker setup uses a persistent `/data` volume and meets the assignment's SQLite persistence requirement. The hosted demo has temporary storage.

1. For the API project, use **FastAPI** and the repository root. Vercel loads `app.py` and installs the backend requirements.
2. Add `LLM_API_KEYS` as a production Secret with two comma-separated Gemini keys. Set the production variables `DATABASE_URL=sqlite:////tmp/lyftr/app.db`, `ALLOWED_ORIGINS=https://lyftrai.vercel.app`, `ENABLE_FAILURE_SIMULATION=true`, and `LITELLM_LOCAL_MODEL_COST_MAP=true`. Deploy and open `/health` on the API domain.
3. For the frontend project, use **Vite**, `frontend` as the root directory, and `dist` as the output directory. Set the production `VITE_API_URL` to the API base URL, deploy a fresh build, and send a message from the deployed site.

[FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi) · [Vercel function filesystem](https://vercel.com/docs/functions/runtimes#file-system-support)

## Run locally: with Docker and without

Copy `.env.example` to `.env` in the project root and replace `LLM_API_KEYS` with two distinct keys that can access the same `LLM_MODEL`. Keys are numbered in the order you enter them. `LLM_API_BASE` is optional for a custom provider endpoint. Keep `.env` out of Git.

### With Docker

Start Docker and run this from the project root.

```sh
make up
```

Open `http://localhost:3000`. The backend is on `http://localhost:8000`. Run `make down` to stop both containers while keeping the SQLite volume.

On Windows without GNU Make, `docker compose up --build -d` and `docker compose down` run the same commands.

### Without Docker

Use Python 3.12 and Node.js 22.12 or later. From the project root in PowerShell, run the backend in one terminal.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
$env:DATABASE_URL = "sqlite:///./data/app.db"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
```

In a second terminal, start the frontend.

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

The frontend defaults to `http://localhost:8000`. Set `VITE_API_URL` in the frontend terminal before running Vite if the backend uses a different URL. In Bash, use `.venv/bin/python` and `npm` in the equivalent commands, and export `DATABASE_URL` before starting the backend.

## Design decisions: key fallback, caching, SQLite session storage

- **Fallback.** Each key is a LiteLLM Router deployment for the same model. The Router tries them in order with one attempt per key. The test header simulates a provider error just before that key would be called. When the pool fails, the API returns a JSON `503`.
- **Caching.** LiteLLM's local cache uses the model and complete message list. Session IDs stay out of the cache key so identical requests can reuse replies across sessions. Cache hits reuse the original reply and token counts and measure latency again. Simulated requests skip the cache so they always test the Router.
- **History.** The React page sends the session ID and latest message. The backend reads the previous turns and saves both new messages in one SQLite transaction after a successful reply. The optional API system prompt stays with the session and is left out of history responses. A fresh page or **New chat** generates a new ID.

The backend code is split into `main.py` for the API, `gateway.py` for LiteLLM, `db.py` for SQLite, and `config.py` for environment settings.

## Verification: how to see failover, cache hits and per-message token usage

1. Open `/health` on the backend. Check for `status: "ok"`, at least two configured keys, and `cache_enabled: true`.
2. In the UI, send a first message you haven't used before and check for `LIVE_INFERENCE`. Click **New chat** and send the exact same message. The second reply should show `LLM_CACHE`, the same text and token counts, and lower latency.
3. In one chat, say `My dog is named Milo.` and then ask `What is my dog's name?`. The reply should use the previous turn. Input and output token counts are displayed beside each reply and returned by `GET /sessions/{session_id}/messages`.
4. Run the PowerShell commands below against the backend to demonstrate failover and inspect the stored messages. Swap the URL for the public backend when recording the demo.

```powershell
$api = "http://localhost:8000"
$session = [guid]::NewGuid().ToString()
$body = @{ session_id = $session; message = "Give me one tip for learning Python." } | ConvertTo-Json

Invoke-RestMethod "$api/health"
Invoke-RestMethod "$api/chat" -Method Post -ContentType "application/json" -Body $body -Headers @{
    "X-Simulate-Failure" = "key_1=429"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod "$api/sessions/$session/messages" | ConvertTo-Json -Depth 5
```

With two keys, the simulated `429` should return a real reply from `key_2` with `fallback_attempts: 1`. Try `key_1=500` for the server-error case. Send `*=429` to see the JSON `503` with `"detail": "all providers unavailable"`. Fetch the session afterward to verify the failed turn stayed out of history.

Run the automated backend tests from the project root. They use a local test provider, so they run without API keys.

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest backend/tests
```

### Demo recording

Record a narrated 2–3 minute walkthrough using the public URLs. Show `/health`, a live reply and a follow-up in the frontend, then repeat the first prompt in a new chat to show the cache hit. Use the PowerShell commands above to show `POST /chat` failing over to `key_2` and `GET /sessions/{session_id}/messages` returning the saved exchange.

## Setup Used: your editor and AI tools

Development used Windows PowerShell, Python 3.12, Node.js, and OpenAI Codex for planning, implementation, and verification.

## Bonus features (if any): what you added beyond the requirements and why

The **New chat** button starts a fresh session without a page reload, which makes the cross-session cache demo easier. The History sidebar lets you reopen recent conversations in the same browser. Reply text supports Markdown and has a copy button.
