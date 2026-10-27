"""The single door to the language models.

Every agent calls the models through this module, so caching, retries,
JSON validation and logging live in one place.
"""
import hashlib
import json
import logging
import time
from typing import TypeVar

from diskcache import Cache
from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from specpilot import config

log = logging.getLogger("specpilot.llm")

_cache = Cache(str(config.CACHE_DIR / "llm"))
_client_instance: OpenAI | None = None

M = TypeVar("M", bound=BaseModel)

# One entry per chat() call: model, cache_hit, duration_s. Agents read the new entries to write audit rows.
calls: list[dict] = []


def _client() -> OpenAI:
    """Created on first use, so modules can be imported without a key (e.g. in tests)."""
    global _client_instance
    if _client_instance is None:
        if not config.LLM_API_KEY:
            raise RuntimeError("No LLM_API_KEY in .env. Copy .env.example to .env and add the team key.")
        # max_retries also covers rate limits (HTTP 429) with backoff
        _client_instance = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, max_retries=5, timeout=300)
    return _client_instance


def _key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def chat(model: str, messages: list[dict], use_cache: bool = True, **kwargs) -> str:
    """Send a chat request and return the reply text.

    temperature=0 keeps answers as stable as possible; the cache makes a repeated
    request return the exact same answer instantly (and keeps the demo working offline).
    """
    payload = {"model": model, "messages": messages, "temperature": 0, **kwargs}
    key = _key(payload)
    if use_cache and key in _cache:
        log.info("cache hit %s %s", model, key[:10])
        calls.append({"model": model, "cache_hit": True, "duration_s": 0.0})
        return _cache[key]

    started = time.time()
    resp = _client().chat.completions.create(**payload)
    text = resp.choices[0].message.content or ""
    duration = time.time() - started
    log.info("llm call %s %s %.1fs", model, key[:10], duration)
    calls.append({"model": model, "cache_hit": False, "duration_s": round(duration, 2)})
    _cache[key] = text
    return text


def _extract_json(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences or add a sentence around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=0)
    end = max(text.rfind("}"), text.rfind("]")) + 1
    return text[start:end] if end > start else text


def ask_json(model: str, system: str, user: str, schema: type[M], retries: int = 2) -> M:
    """Ask for an answer that must match a Pydantic schema.

    If the reply does not validate, the error is sent back to the model so it can
    correct itself, up to `retries` times.
    """
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    messages = [
        {"role": "system", "content": f"{system}\n\nAnswer ONLY with JSON that matches this JSON schema:\n{schema_json}"},
        {"role": "user", "content": user},
    ]
    fmt = {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}

    for attempt in range(retries + 1):
        text = None
        if fmt is not None:
            try:
                text = chat(model, messages, response_format=fmt)
            except BadRequestError:
                # The gateway may not support enforced JSON; the schema in the prompt still applies.
                log.info("%s rejected response_format, using prompt-only JSON", model)
                fmt = None
        if text is None:
            text = chat(model, messages)
        try:
            return schema.model_validate_json(_extract_json(text))
        except ValidationError as err:
            log.warning("invalid JSON from %s (attempt %d): %s", model, attempt + 1, err.error_count())
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"Your answer did not match the schema:\n{err}\nReturn corrected JSON only."},
            ]
    raise ValueError(f"{model} did not return valid {schema.__name__} after {retries + 1} attempts")


def list_models() -> list[str]:
    return [m.id for m in _client().models.list().data]
