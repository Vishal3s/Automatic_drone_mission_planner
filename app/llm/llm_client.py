"""
Phase 3 -- LLM client abstraction.

Only this file talks to an LLM library directly. Every other module
either talks to the RAG index (`app/rag/`) or works with this project's
own `Mission`/`MissionItem` Pydantic models (`app/mission/`) -- nothing
downstream of `generate_structured()` needs to know or care which
provider produced a mission. That's the point of `LLM_PROVIDER` in
`.env`: swapping providers later means adding one new class here, not
touching the RAG layer, the schema, the validator, or the converter.

Reliability approach: every provider client uses that provider's native
schema-constrained generation (Ollama's `format=<json schema>`, Groq's
`response_format={"type": "json_schema", ..., "strict": True}`) so the
model is structurally prevented from emitting a field of the wrong type
or a `command` value outside the allowed enum -- see
`app/mission/schema.py`'s docstring for why that schema lives there.
Constrained decoding still isn't a 100% guarantee with every model (a
small/undertrained model can occasionally produce a schema-shaped-but-
semantically-broken response, or the provider call itself can fail), so
`generate_structured()` retries a small, fixed number of times with the
raw failure appended to the prompt before giving up.

Two providers are implemented:
- `groq` (default) -- a free hosted API, so it needs no local RAM/VRAM
  and sidesteps the kind of out-of-memory failure `ollama` hit on a
  memory-constrained laptop during this project's own testing.
- `ollama` -- fully local/offline, no API key, but needs enough RAM to
  load the model (see the README's Phase 3 section for sizing guidance).
"""

from __future__ import annotations

import logging
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 3


def _strict_json_schema(schema_cls: Type[BaseModel]) -> dict:
    """
    Build a JSON schema from a Pydantic model that satisfies strict
    structured-output mode (OpenAI's and Groq's included, and this is a
    safe no-op for Ollama's more lenient grammar-based mode too).

    The gotcha this works around: Pydantic only lists a field in
    `"required"` if it has no default -- an `Optional[float] = None`
    field (like `MissionItem.latitude`) is *not* required, it's just
    nullable. But OpenAI/Groq-style strict mode requires every property
    to appear in `"required"`, even a nullable one (the model must
    still emit the key, with value `null`, rather than omit it). This is
    a widely-hit issue, not specific to this project -- see e.g.
    https://community.openai.com/t/wrapper-for-structured-outputs-with-non-required-fields/913246
    Fix: recursively force every object's `"required"` list to include
    every one of its `"properties"`, both at the top level and inside
    `"$defs"` (where Pydantic puts nested models like `MissionItem`).
    """
    schema = schema_cls.model_json_schema()

    def _force_required(node: dict) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                # Planner-only fields (e.g. MissionItem camera yaw) are set by code, never by the LLM.
                for key in [k for k, v in node["properties"].items() if isinstance(v, dict) and v.get("planner_only")]:
                    del node["properties"][key]
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                if isinstance(value, dict):
                    _force_required(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            _force_required(item)

    _force_required(schema)
    for defn in schema.get("$defs", {}).values():
        _force_required(defn)

    return schema


class LLMGenerationError(Exception):
    """
    Raised when the LLM could not be reached, or could not be coaxed
    into a schema-valid response within MAX_ATTEMPTS tries.
    """


class BaseLLMClient:
    """Interface every provider client implements."""

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Type[T],
        temperature: float = 0.0,
    ) -> T:
        raise NotImplementedError


class OllamaLLMClient(BaseLLMClient):
    """
    Talks to a local Ollama server via the official `ollama` Python
    package. Requires `ollama serve` to be running and the configured
    model to already be pulled (`ollama pull <model>`) -- see the
    README's Phase 3 section for exact commands.
    """

    def __init__(self, model: str | None = None, host: str | None = None):
        # Imported lazily so importing this module doesn't require the
        # `ollama` package (or a running server) unless an
        # OllamaLLMClient is actually constructed -- same pattern as the
        # lazy imports in app/rag/embeddings.py and app/rag/retriever.py.
        from ollama import Client

        self.model = model or settings.llm_model
        self.host = host or settings.ollama_host
        self._client = Client(host=self.host)

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Type[T],
        temperature: float = 0.0,
    ) -> T:
        from ollama import ResponseError

        json_schema = _strict_json_schema(schema)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.chat(
                    model=self.model,
                    messages=messages,
                    format=json_schema,
                    options={"temperature": temperature},
                    think=False,  # keep response.message.content pure JSON, no chain-of-thought text mixed in
                )
            except ConnectionError as exc:
                raise LLMGenerationError(
                    f"Could not reach Ollama at {self.host}. Is `ollama serve` running, "
                    f"and has `ollama pull {self.model}` been run? Original error: {exc}"
                ) from exc
            except ResponseError as exc:
                # e.g. model not pulled -- fail fast, retrying won't help.
                raise LLMGenerationError(
                    f"Ollama returned an error for model {self.model!r}: {exc}. "
                    f"If this says the model wasn't found, run: ollama pull {self.model}"
                ) from exc

            raw_content = response["message"]["content"]
            try:
                return schema.model_validate_json(raw_content)
            except ValidationError as exc:
                last_error = exc
                log.warning(
                    "Attempt %d/%d: LLM output failed schema validation: %s\nRaw output: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    raw_content,
                )
                # Feed the failure back to the model for the retry -- a
                # simple, effective form of self-correction for small
                # local models that occasionally produce a schema-shaped
                # but subtly invalid response (e.g. a string where a
                # number was required).
                messages.append({"role": "assistant", "content": raw_content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That response did not match the required schema. "
                            f"Validation error: {exc}\n"
                            "Return the corrected structured mission now."
                        ),
                    }
                )

        raise LLMGenerationError(
            f"LLM did not produce a schema-valid {schema.__name__} after {MAX_ATTEMPTS} attempts. "
            f"Last validation error: {last_error}"
        )


class GroqLLMClient(BaseLLMClient):
    """
    Talks to the hosted Groq API via the official `groq` Python package
    (OpenAI-compatible chat-completions shape). Requires `GROQ_API_KEY`
    in `.env` -- get a free key at https://console.groq.com/keys
    """

    def __init__(self, model: str | None = None, api_key: str | None = None):
        # Lazy import -- same reasoning as OllamaLLMClient above.
        from groq import Groq

        self.model = model or settings.llm_model
        key = api_key or settings.groq_api_key
        if not key:
            raise LLMGenerationError(
                "GROQ_API_KEY is not set in .env. Get a free key at "
                "https://console.groq.com/keys and add it as GROQ_API_KEY=... "
            )
        self._client = Groq(api_key=key)

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Type[T],
        temperature: float = 0.0,
    ) -> T:
        import groq  # for the exception classes below

        json_schema = _strict_json_schema(schema)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": json_schema,
            },
        }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format=response_format,
                    temperature=temperature,
                )
            except groq.AuthenticationError as exc:
                raise LLMGenerationError(
                    f"Groq rejected the API key (401). Check GROQ_API_KEY in .env. "
                    f"Original error: {exc}"
                ) from exc
            except groq.RateLimitError as exc:
                raise LLMGenerationError(
                    f"Groq free-tier rate limit hit (429) for model {self.model!r}. "
                    f"Wait a bit and retry, or check limits at https://console.groq.com/settings/limits. "
                    f"Original error: {exc}"
                ) from exc
            except groq.APIConnectionError as exc:
                raise LLMGenerationError(
                    f"Could not reach the Groq API -- check your internet connection. "
                    f"Original error: {exc}"
                ) from exc
            except groq.APIStatusError as exc:
                # e.g. model id not found/decommissioned, or the schema was rejected -- fail fast.
                raise LLMGenerationError(
                    f"Groq returned an error (status {exc.status_code}) for model {self.model!r}: {exc}. "
                    f"If this mentions the model, check current model ids at "
                    f"https://console.groq.com/docs/models -- Groq deprecates models over time."
                ) from exc

            raw_content = response.choices[0].message.content
            try:
                return schema.model_validate_json(raw_content)
            except ValidationError as exc:
                last_error = exc
                log.warning(
                    "Attempt %d/%d: LLM output failed schema validation: %s\nRaw output: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    raw_content,
                )
                messages.append({"role": "assistant", "content": raw_content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That response did not match the required schema. "
                            f"Validation error: {exc}\n"
                            "Return the corrected structured mission now."
                        ),
                    }
                )

        raise LLMGenerationError(
            f"LLM did not produce a schema-valid {schema.__name__} after {MAX_ATTEMPTS} attempts. "
            f"Last validation error: {last_error}"
        )


def get_llm_client() -> BaseLLMClient:
    """
    Factory that reads `LLM_PROVIDER` from settings and returns the
    matching client. To add a provider: write a new `BaseLLMClient`
    subclass above and add one `elif` branch here -- nothing else in
    the project needs to change.
    """
    provider = settings.llm_provider.lower()
    if provider == "groq":
        return GroqLLMClient()
    if provider == "ollama":
        return OllamaLLMClient()
    raise NotImplementedError(
        f"LLM_PROVIDER={provider!r} is not implemented yet. 'groq' and 'ollama' are "
        f"supported as of Phase 3. requirements.txt already includes the `openai` "
        f"package for a future OpenAILLMClient, but it is not wired up here yet."
    )
