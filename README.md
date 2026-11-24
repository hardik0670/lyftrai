# Lyftr Chat

A React chat app with a FastAPI backend. SQLite stores each conversation, and LiteLLM handles model calls, key failover, and response caching.

## Live URLs

| Service | URL |
| --- | --- |
| Frontend | Pending deployment. Local address is `http://localhost:3000`. |
| Backend | Pending deployment. Local address is `http://localhost:8000`. |

For deployment, build the backend from the `backend` directory using its Dockerfile. Use one instance with a persistent disk mounted at `/data` and set `DATABASE_URL=sqlite:////data/app.db`. Put your keys in the host's environment settings and enable `ENABLE_FAILURE_SIMULATION=true` for the assignment.

Deploy the frontend as a static site. Set its root to `frontend`, build with `npm ci && npm run build`, and publish `dist`. Set `VITE_API_URL` to the public backend URL before building. Add the public frontend origin to the backend's `ALLOWED_ORIGINS`.

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
2. Send the same first message to `POST /chat` with two different session IDs. The response metadata should show `LIVE_INFERENCE` on the first call and `LLM_CACHE` on the second. The reply and token counts should match, and latency should be lower on the second call.
3. In one chat, say `My dog is named Milo.` and then ask `What is my dog's name?`. The backend includes the previous turn. Use `GET /sessions/{session_id}/messages` to inspect the input and output token counts for each reply.
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

## Setup Used: your editor and AI tools

Development used Windows PowerShell, Python 3.12, Node.js, and OpenAI Codex for planning, implementation, and verification.

## Bonus features (if any): what you added beyond the requirements and why

The **New chat** button starts a fresh session without a page reload, which makes the cross-session cache demo easier.

Send messages as usual. When a question needs current information, the chat model requests a lookup through `LLM_SEARCH_MODEL` and displays the source links. Search replies are available while the conversation is open. [Gemini Search grounding terms](https://aistudio.google.com/docs/terms#grounding-with-google-search)
