from dataclasses import dataclass

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


class ProvidersUnavailable(Exception):
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

        key_id = kwargs["model_info"]["id"]
        status = failures.get(key_id, failures.get("*"))
        if status is not None:
            error_type = SIMULATED_ERRORS[status]
            raise error_type("Simulated provider failure", "simulation", kwargs["model"])


litellm.callbacks.append(SimulateFailures())


@dataclass(frozen=True)
class Answer:
    reply: str
    model: str
    input_tokens: int
    output_tokens: int
    cached: bool = False
    key_id: str | None = None
    fallback_attempts: int = 0


class Gateway:
    def __init__(self, settings: Settings):
        self.model = settings.model
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

        # Each request starts at key_1. A failure must not change the order for the next request.
        self.router = litellm.Router(
            model_list=deployments,
            num_retries=0,
            max_fallbacks=len(self.key_ids) - 1,
            disable_cooldowns=True,
            timeout=20,
        )

    async def answer(self, messages: list[dict[str, str]], failures: dict[str, str]) -> Answer:
        cache_request = {"model": self.model, "messages": messages}
        # A cached reply would hide a failover demo, so simulations always call a provider.
        if not failures:
            cached = self.cache.get_cache(**cache_request)
            if cached is not None:
                return Answer(**cached, cached=True)

        try:
            response = await self.router.acompletion(
                **cache_request, metadata={"simulated_failures": failures} if failures else {}
            )
        except APIError as error:
            raise ProvidersUnavailable from error

        reply = response.choices[0].message.content if response.choices else None
        usage = response.usage
        key_id = response._hidden_params.get("model_id")
        if not reply or not reply.strip() or usage is None or key_id not in self.key_ids:
            raise ProvidersUnavailable

        provider, _, _ = self.model.partition("/")
        answered_model = response.model or self.model
        if "/" in self.model and not answered_model.startswith(f"{provider}/"):
            answered_model = f"{provider}/{answered_model}"

        result = {
            "reply": reply,
            "model": answered_model,
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
        }
        if not failures:
            self.cache.add_cache(result, **cache_request)
        return Answer(**result, key_id=key_id, fallback_attempts=self.key_ids.index(key_id))
