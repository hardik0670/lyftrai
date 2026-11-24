import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import litellm
from litellm.caching.caching import Cache
from litellm.integrations.custom_logger import CustomLogger
from openai import APIError

from .config import Settings

SIMULATED_ERRORS = {
    "429": litellm.RateLimitError,
    "500": litellm.InternalServerError,
    "502": litellm.BadGatewayError,
    "503": litellm.ServiceUnavailableError,
}
SEARCH_GROUP = "live-search"
SEARCH_KEY_PREFIX = "search:"
SEARCH_PROMPT = "Use Google Search to answer the question. Give a concise answer based on the results."
LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_web",
        "description": "Look up current information or verify facts about a named person, company, or product.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The current question as a standalone search request with any relevant place or subject.",
                },
            },
            "required": ["query"],
        },
    },
}
LOOKUP_INSTRUCTIONS = (
    "Use lookup_web for current news, weather and forecasts, prices, schedules, recent events, "
    "explicit web searches, and questions about unfamiliar named people, companies, or products. "
    "For local weather, ask which city or postal code to check if the conversation doesn't identify "
    "a location. Never assume the user's location. Answer ordinary explanations, writing requests, "
    "and summaries of supplied text directly. Include only the information needed for the public lookup."
)


class ProvidersUnavailable(Exception):
    pass


class SearchUnavailable(Exception):
    pass


class SearchSourcesUnavailable(Exception):
    pass


def parse_simulated_failures(header: str, key_count: int) -> dict[str, str]:
    valid_ids = {f"key_{number}" for number in range(1, key_count + 1)}
    valid_ids.add("*")
    failures = {}
    for pair in header.split(","):
        key_id, separator, status = pair.strip().partition("=")
        if not separator or key_id not in valid_ids or status not in SIMULATED_ERRORS:
            raise ValueError("Invalid X-Simulate-Failure header. Use key_1=429 or *=503.")
        failures[key_id] = status
    return failures


class SimulateFailures(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        metadata = kwargs.get("metadata") or {}
        failures = metadata.get("simulated_failures")
        if not failures:
            return

        key_id = kwargs["model_info"]["id"].removeprefix(SEARCH_KEY_PREFIX)
        status = failures.get(key_id, failures.get("*"))
        if status is not None:
            error_type = SIMULATED_ERRORS[status]
            raise error_type("Simulated provider failure", "simulation", kwargs["model"])


litellm.callbacks.append(SimulateFailures())


@dataclass(frozen=True)
class SearchSource:
    title: str
    url: str


@dataclass(frozen=True)
class SearchResult:
    sources: list[SearchSource]
    suggestions_html: str
    query: str


def get_search_result(grounding: list[dict], query: str) -> SearchResult:
    sources = {}
    suggestions_html = ""
    for item in grounding:
        entry = item.get("searchEntryPoint") or {}
        suggestions_html = suggestions_html or entry.get("renderedContent") or ""
        for chunk in item.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            url = web.get("uri") or ""
            parsed_url = urlsplit(url)
            if parsed_url.scheme == "https" and parsed_url.hostname:
                sources[url] = SearchSource(title=web.get("title") or parsed_url.hostname, url=url)
    if not sources:
        raise SearchSourcesUnavailable
    return SearchResult(sources=list(sources.values()), suggestions_html=suggestions_html, query=query)


def requested_lookup(response) -> str | None:
    if not response.choices:
        return None
    for tool in getattr(response.choices[0].message, "tool_calls", None) or []:
        if tool.function.name != "lookup_web":
            continue
        try:
            args = json.loads(tool.function.arguments)
        except (TypeError, ValueError):
            continue
        query = args.get("query") if isinstance(args, dict) else None
        if isinstance(query, str) and query.strip():
            return query.strip()[:4000]
    return None


@dataclass(frozen=True)
class Answer:
    reply: str
    model: str
    input_tokens: int
    output_tokens: int
    cached: bool = False
    key_id: str | None = None
    fallback_attempts: int = 0
    search: SearchResult | None = None


class Gateway:
    def __init__(self, settings: Settings):
        self.model = settings.model
        self.search_model = settings.search_model
        self.key_ids = [f"key_{number}" for number in range(1, len(settings.api_keys) + 1)]
        self.cache = Cache(type="local", ttl=3600)
        deployments = []
        for order, (key_id, api_key) in enumerate(zip(self.key_ids, settings.api_keys), start=1):
            params = {"model": self.model, "api_key": api_key, "order": order}
            if settings.api_base:
                params["api_base"] = settings.api_base
            deployments.append(
                {"model_name": self.model, "litellm_params": params, "model_info": {"id": key_id}}
            )
            if self.search_model:
                deployments.append({
                    "model_name": SEARCH_GROUP,
                    "litellm_params": {**params, "model": self.search_model},
                    "model_info": {"id": f"{SEARCH_KEY_PREFIX}{key_id}"},
                })

        # Each request starts at key_1. A failure must not change the order for the next request.
        self.router = litellm.Router(
            model_list=deployments,
            num_retries=0,
            max_fallbacks=len(self.key_ids) - 1,
            disable_cooldowns=True,
            timeout=20,
        )

    async def answer(
        self, messages: list[dict[str, str]], failures: dict[str, str], *,
        use_cache: bool = True, search: bool = False,
    ) -> Answer:
        if search:
            return await self._lookup(messages[-1]["content"], failures)

        if self.search_model:
            system_message = messages[0]
            messages = [
                {**system_message, "content": system_message["content"] + "\n\n" + LOOKUP_INSTRUCTIONS},
                *messages[1:],
            ]
        cache_request = {"model": self.model, "messages": messages}
        # A cached reply would hide a failover demo, so simulations always call a provider.
        use_cache = use_cache and not failures
        if use_cache:
            cached = self.cache.get_cache(**cache_request)
            if cached is not None:
                return Answer(**cached, cached=True)

        request = {**cache_request, "tools": [LOOKUP_TOOL]} if self.search_model else cache_request
        response = await self._complete(request, failures)
        if self.search_model and (query := requested_lookup(response)):
            return await self._lookup(query, failures)

        answer = self._read_answer(response, self.model)
        if use_cache:
            self.cache.add_cache({
                "reply": answer.reply,
                "model": answer.model,
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
            }, **cache_request)
        return answer

    async def _lookup(self, query: str, failures: dict[str, str]) -> Answer:
        if self.search_model is None:
            raise SearchUnavailable
        try:
            response = await self._complete({
                "model": SEARCH_GROUP,
                "messages": [
                    {"role": "system", "content": SEARCH_PROMPT},
                    {"role": "user", "content": query},
                ],
                "tools": [{"googleSearch": {}}],
            }, failures)
            grounding = get_search_result(
                response._hidden_params.get("vertex_ai_grounding_metadata") or [], query
            )
            return self._read_answer(response, self.search_model, search=grounding)
        except ProvidersUnavailable as error:
            raise SearchUnavailable from error

    async def _complete(self, request: dict, failures: dict[str, str]):
        try:
            response = await self.router.acompletion(
                **request, metadata={"simulated_failures": failures} if failures else {}
            )
        except APIError as error:
            raise ProvidersUnavailable from error

        return response

    def _read_answer(self, response, model: str, *, search: SearchResult | None = None) -> Answer:
        reply = response.choices[0].message.content if response.choices else None
        usage = response.usage
        key_id = (response._hidden_params.get("model_id") or "").removeprefix(SEARCH_KEY_PREFIX)
        if not reply or not reply.strip() or usage is None or key_id not in self.key_ids:
            raise ProvidersUnavailable

        provider, _, _ = model.partition("/")
        answered_model = response.model or model
        if "/" in model and not answered_model.startswith(f"{provider}/"):
            answered_model = f"{provider}/{answered_model}"

        return Answer(
            reply=reply,
            model=answered_model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            key_id=key_id,
            fallback_attempts=self.key_ids.index(key_id),
            search=search,
        )
