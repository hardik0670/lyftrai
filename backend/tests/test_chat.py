import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import litellm
import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


@pytest.fixture
def provider():
    """A small stand-in for the provider. The app still uses the real LiteLLM Router."""
    calls = []
    failures = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers["Authorization"].removeprefix("Bearer ")
            calls.append((key, request["messages"]))
            status = failures.get(key, 200)
            if status == 200:
                sleep(0.035)
                content = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-4o-mini",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "Reply from the test provider."},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 10 * len(request["messages"]),
                        "completion_tokens": 6,
                        "total_tokens": 10 * len(request["messages"]) + 6,
                    },
                }
            else:
                content = {"error": {
                    "message": "Provider unavailable",
                    "code": "insufficient_quota" if status == 429 else "server_error",
                }}
            body = json.dumps(content).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}/v1", calls, failures
        server.shutdown()
        thread.join()


@pytest.fixture
def settings(tmp_path, provider):
    return Settings(
        database_path=tmp_path / "app.db",
        api_keys=("test-key-a", "test-key-b"),
        model="openai/gpt-4o-mini",
        allowed_origins=("http://localhost:3000",),
        simulate_failures=True,
        api_base=provider[0],
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def send(client, session, message, failure=None, **extra):
    return client.post(
        "/chat",
        json={"session_id": session, "message": message, **extra},
        headers={"X-Simulate-Failure": failure} if failure else {},
    )


def test_health_and_cors(client):
    assert client.get("/health").json() == {
        "status": "ok", "keys_configured": 2, "cache_enabled": True,
    }
    response = client.options("/chat", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-simulate-failure",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cache_and_usage(client, provider):
    live = send(client, "one", "What is the capital of France?").json()
    cached = send(client, "two", "What is the capital of France?").json()
    different = send(client, "three", "What is the capital of India?").json()
    assert live["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert live["metadata"]["key_id"] == "key_1"
    assert cached["metadata"]["resolution_source"] == "LLM_CACHE"
    assert cached["metadata"]["key_id"] is None
    assert cached["metadata"]["fallback_attempts"] == 0
    assert cached["reply"] == live["reply"]
    for field in ("model", "input_tokens", "output_tokens"):
        assert cached["metadata"][field] == live["metadata"][field]
    assert cached["metadata"]["latency_ms"] < live["metadata"]["latency_ms"]
    assert different["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert len(provider[1]) == 2


def test_full_history_and_system_prompt_are_used(client, provider):
    first = send(client, "one", "Remember my name is Ash.", system_prompt="Be brief.").json()
    second = send(client, "one", "What is my name?").json()
    outsider = send(client, "two", "What is my name?").json()
    other_prompt = send(client, "three", "Remember my name is Ash.", system_prompt="Be formal.").json()
    assert first["metadata"]["input_tokens"] == 20
    assert second["metadata"]["input_tokens"] == 40
    assert outsider["metadata"]["input_tokens"] == 20
    assert other_prompt["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert provider[1][1][1] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Remember my name is Ash."},
        {"role": "assistant", "content": first["reply"]},
        {"role": "user", "content": "What is my name?"},
    ]
    history = client.get("/sessions/one/messages").json()["messages"]
    assert [item["role"] for item in history] == ["user", "assistant", "user", "assistant"]
    assert "metadata" not in history[0]
    assert history[1]["metadata"] == first["metadata"]
    assert history[3]["metadata"] == second["metadata"]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_simulated_failure_uses_next_key_even_with_warm_cache(client, provider, status):
    assert send(client, "warm", "Hello").status_code == 200
    response = send(client, "simulated", "Hello", f"key_1={status}")
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["key_id"] == "key_2"
    assert metadata["fallback_attempts"] == 1
    assert metadata["resolution_source"] == "LIVE_INFERENCE"
    assert len(provider[1]) == 2
    assert provider[1][-1][0] == "test-key-b"
    assert send(client, "next", "New message").json()["metadata"]["key_id"] == "key_1"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_provider_errors_automatically_fail_over(client, provider, status):
    provider[2]["test-key-a"] = status
    response = send(client, "live-failover", "Hello")
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["key_id"] == "key_2"
    assert response.json()["metadata"]["fallback_attempts"] == 1
    assert [key for key, _messages in provider[1]] == ["test-key-a", "test-key-b"]


def test_two_failed_keys_before_success(settings, provider):
    settings = replace(settings, api_keys=(*settings.api_keys, "test-key-c"))
    with TestClient(create_app(settings)) as client:
        response = send(client, "one", "Hello", "key_1=429,key_2=500")
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["key_id"] == "key_3"
    assert response.json()["metadata"]["fallback_attempts"] == 2
    assert [key for key, _messages in provider[1]] == ["test-key-c"]


def test_all_keys_failed_leaves_history_unchanged(client):
    send(client, "one", "Keep this.")
    before = client.get("/sessions/one/messages").json()
    for session in ("one", "fresh"):
        response = send(client, session, "Do not save this.", "*=429")
        assert response.status_code == 503
        assert response.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/one/messages").json() == before
    missing = client.get("/sessions/fresh/messages")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "session not found"}


@pytest.mark.parametrize("body", [
    {"session_id": "invalid"},
    {"session_id": "invalid", "message": "   "},
    {"session_id": "invalid", "message": "x" * 4001},
    {"session_id": "", "message": "Okay"},
    {"session_id": "x" * 129, "message": "Okay"},
])
def test_validation_leaves_no_history(client, body):
    response = client.post("/chat", json=body)
    assert response.status_code == 422
    assert "detail" in response.json()
    assert client.get("/sessions/invalid/messages").status_code == 404


def test_malformed_json_and_failure_header(client):
    response = client.post("/chat", content="{", headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert send(client, "one", "Hello", "key_1=404").status_code == 422
    assert client.get("/sessions/one/messages").status_code == 404


def test_failure_header_can_be_disabled(settings):
    with TestClient(create_app(replace(settings, simulate_failures=False))) as client:
        response = send(client, "one", "Hello", "*=429")
        assert response.json()["metadata"]["key_id"] == "key_1"


def test_history_survives_app_restart(settings):
    with TestClient(create_app(settings)) as client:
        assert send(client, "saved", "Remember this.").status_code == 200
    with TestClient(create_app(settings)) as client:
        history = client.get("/sessions/saved/messages")
        assert history.status_code == 200
        assert history.json()["messages"][0]["content"] == "Remember this."


def test_temporary_chat_keeps_context_without_saving_or_caching(client, provider):
    first = send(client, "temporary", "My name is Hale.", temporary_history=[]).json()
    history = [
        {"role": "user", "content": "My name is Hale."},
        {"role": "assistant", "content": first["reply"]},
    ]
    follow_up = send(client, "temporary", "What is my name?", temporary_history=history)
    assert follow_up.status_code == 200
    assert provider[1][-1][1][1:3] == history
    assert client.get("/sessions/temporary/messages").status_code == 404

    regular = send(client, "regular", "My name is Hale.").json()
    assert regular["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    repeated = send(client, "temporary", "My name is Hale.", temporary_history=[]).json()
    assert repeated["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_history_delete_removes_only_selected_browser_session(client):
    target = str(uuid4())
    other = str(uuid4())
    assert send(client, target, "Conversation to delete").status_code == 200
    assert send(client, other, "Conversation to keep").status_code == 200

    path = f"/sessions/{target}/messages"
    origin = "http://localhost:3000"
    preflight = client.options(path, headers={
        "Origin": origin,
        "Access-Control-Request-Method": "DELETE",
    })
    assert preflight.status_code == 200
    deletion = client.delete(path, headers={"Origin": origin})
    assert deletion.status_code == 204
    assert deletion.headers["access-control-allow-origin"] == origin
    assert client.get(path).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.get(f"/sessions/{other}/messages").status_code == 200
    assert client.delete("/sessions/guessable/messages").status_code == 422


def test_search_returns_live_sources_without_saving_or_caching(settings, monkeypatch):
    regular_completion = litellm.Router.acompletion
    search_queries = []

    def reply(content, *, tool_calls=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
            model="gpt-4o-mini",
            usage=SimpleNamespace(prompt_tokens=25, completion_tokens=8),
            _hidden_params={"model_id": "key_1"},
        )

    async def completion(router, **request):
        question = request["messages"][-1]["content"]
        if request["model"] != "live-search":
            assert request["tools"][0]["function"]["name"] == "lookup_web"
            if question == "What's the weather?":
                return reply("Which city or postal code should I check?")
            if question == "Mumbai" or question.startswith("Previous lookup request:"):
                query = "Current weather in Mumbai?" if question == "Mumbai" else "Tomorrow's weather in Mumbai?"
                return reply(None, tool_calls=[SimpleNamespace(function=SimpleNamespace(
                    name="lookup_web", arguments=json.dumps({"query": query})
                ))])
            return await regular_completion(router, **request)
        assert request["tools"] == [{"googleSearch": {}}]
        assert len(request["messages"]) == 2
        query = question
        search_queries.append(query)
        chunks = [] if query == "No sources" else [
            {"web": {"title": "Weather service", "uri": "https://weather.example/forecast"}},
            {"web": {"title": "Ignored", "uri": "javascript:example"}},
        ]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Sunny in Mumbai."))],
            model="gemini-2.5-flash-lite",
            usage=SimpleNamespace(prompt_tokens=33, completion_tokens=8),
            _hidden_params={
                "model_id": "search:key_1",
                "vertex_ai_grounding_metadata": [{
                    "groundingChunks": chunks,
                    "searchEntryPoint": {"renderedContent": "<div>Google Search</div>"},
                }],
            },
        )

    monkeypatch.setattr(litellm.Router, "acompletion", completion)
    settings = replace(settings, search_model="gemini/gemini-2.5-flash-lite")
    with TestClient(create_app(settings)) as client:
        session_id = str(uuid4())
        question = "Current weather in Mumbai?"
        assert send(client, session_id, "Remember this").status_code == 200
        history_path = f"/sessions/{session_id}/messages"
        initial = client.get(history_path).json()

        first = send(client, session_id, question, search=True)
        again = send(client, session_id, question, search=True)
        assert first.status_code == again.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["search"] == {
            "sources": [{"title": "Weather service", "url": "https://weather.example/forecast"}],
            "suggestions_html": "<div>Google Search</div>",
            "query": question,
        }
        assert again.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
        assert first.json()["metadata"]["model"] == "gemini/gemini-2.5-flash-lite"
        assert client.get(history_path).json() == initial

        no_sources = send(client, session_id, "No sources", search=True)
        assert no_sources.status_code == 502
        assert "no source links" in no_sources.json()["detail"]
        assert search_queries == [question, question, "No sources"]

        clarification = send(client, session_id, "What's the weather?")
        assert clarification.json()["reply"] == "Which city or postal code should I check?"
        assert "search" not in clarification.json()
        assert search_queries == [question, question, "No sources"]
        saved = client.get(history_path).json()
        automatic = send(client, session_id, "Mumbai")
        assert automatic.status_code == 200
        assert automatic.headers["cache-control"] == "no-store"
        assert automatic.json()["search"]["query"] == question
        follow_up = send(client, session_id, "And tomorrow?", search_context=question)
        assert follow_up.json()["search"]["query"] == "Tomorrow's weather in Mumbai?"
        assert client.get(history_path).json() == saved

        normal = send(client, str(uuid4()), "Remember this")
        assert "search" not in normal.json()
        assert normal.json()["metadata"]["resolution_source"] == "LLM_CACHE"
