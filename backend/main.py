import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated, Literal
from weakref import WeakValueDictionary

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, StringConstraints, field_validator

from .config import Settings, read_settings
from .db import Database
from .gateway import (
    Gateway,
    ProvidersUnavailable,
    SearchResult,
    SearchSourcesUnavailable,
    SearchUnavailable,
    parse_simulated_failures,
)

DEFAULT_PROMPT = "You are a helpful assistant. Answer clearly and concisely."
logger = logging.getLogger(__name__)


class TemporaryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    session_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    system_prompt: str | None = None
    temporary_history: list[TemporaryMessage] | None = None
    search: bool = False
    search_context: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @field_validator("session_id")
    @classmethod
    def reject_blank_session(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id cannot be blank")
        return value


class ReplyMetadata(BaseModel):
    resolution_source: Literal["LIVE_INFERENCE", "LLM_CACHE"]
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    metadata: ReplyMetadata
    search: SearchResult | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or read_settings()
    database = Database(settings.database_path)
    # A lock stays in memory only while a request is using it or waiting for it.
    session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
    gateway = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal gateway
        database.initialize()
        if len(set(settings.api_keys)) >= 2:
            gateway = Gateway(settings)
        else:
            logger.warning("Set LLM_API_KEYS to at least two distinct provider keys.")
        yield
        gateway = None

    app = FastAPI(title="Lyftr Chat", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-Simulate-Failure"],
    )

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_request: Request, _error: sqlite3.Error):
        logger.error("SQLite operation failed.")
        return JSONResponse(status_code=503, content={"detail": "storage unavailable"})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _error: Exception):
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/health")
    async def health():
        database.check()
        if gateway is None:
            raise HTTPException(status_code=503, detail="key pool not configured")
        return {"status": "ok", "keys_configured": len(settings.api_keys), "cache_enabled": True}

    @app.post("/chat", response_model=ChatResponse, response_model_exclude_unset=True)
    async def chat(
        request: ChatRequest,
        response_headers: Response,
        simulate_failure: Annotated[str | None, Header(alias="X-Simulate-Failure")] = None,
    ):
        started_at = perf_counter()
        failures = {}
        if settings.simulate_failures and simulate_failure is not None:
            try:
                failures = parse_simulated_failures(simulate_failure, len(settings.api_keys))
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

        if gateway is None:
            raise HTTPException(status_code=503, detail="all providers unavailable")

        async def resolve_reply(prompt: str, history: list[dict], *, use_cache: bool = True):
            messages = [{"role": "system", "content": prompt}]
            messages.extend({"role": item["role"], "content": item["content"]} for item in history)
            question = request.message
            if request.search_context and not request.search:
                question = f"Previous lookup request: {request.search_context}\n\nCurrent question: {question}"
            messages.append({"role": "user", "content": question})

            try:
                answer = await gateway.answer(messages, failures, use_cache=use_cache, search=request.search)
            except SearchSourcesUnavailable as error:
                raise HTTPException(
                    status_code=502, detail="Search returned no source links. Try a more specific query."
                ) from error
            except SearchUnavailable as error:
                raise HTTPException(status_code=503, detail="Live search is temporarily unavailable.") from error
            except ProvidersUnavailable as error:
                raise HTTPException(status_code=503, detail="all providers unavailable") from error

            metadata = ReplyMetadata(
                resolution_source="LLM_CACHE" if answer.cached else "LIVE_INFERENCE",
                model=answer.model,
                latency_ms=round((perf_counter() - started_at) * 1000),
                input_tokens=answer.input_tokens,
                output_tokens=answer.output_tokens,
                key_id=answer.key_id,
                fallback_attempts=answer.fallback_attempts,
            )
            result = ChatResponse(session_id=request.session_id, reply=answer.reply, metadata=metadata)
            if answer.search is not None:
                response_headers.headers["Cache-Control"] = "no-store"
                result.search = answer.search
            return result

        if request.search:
            return await resolve_reply(DEFAULT_PROMPT, [], use_cache=False)

        if request.temporary_history is not None:
            # The browser sends previous turns only for temporary chats.
            prompt = request.system_prompt if request.system_prompt is not None else DEFAULT_PROMPT
            history = [item.model_dump() for item in request.temporary_history]
            return await resolve_reply(prompt, history, use_cache=False)

        lock = session_locks.setdefault(request.session_id, asyncio.Lock())
        async with lock:
            saved_prompt, history = database.load_session(request.session_id)
            prompt = request.system_prompt if request.system_prompt is not None else saved_prompt
            if prompt is None:
                prompt = DEFAULT_PROMPT

            response = await resolve_reply(prompt, history)
            if response.search is None:
                database.save_turn(
                    request.session_id, prompt, request.message, response.reply, response.metadata.model_dump()
                )
            return response

    @app.get("/sessions/{session_id:path}/messages")
    async def session_messages(session_id: str):
        prompt, messages = database.load_session(session_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "messages": messages}

    @app.delete("/sessions/{session_id}/messages", status_code=204)
    async def delete_session(session_id: UUID4):
        # Only browser-generated, unguessable session IDs are eligible for deletion.
        identifier = str(session_id)
        lock = session_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            if not database.delete_session(identifier):
                raise HTTPException(status_code=404, detail="session not found")
        return Response(status_code=204)

    return app


app = create_app()
