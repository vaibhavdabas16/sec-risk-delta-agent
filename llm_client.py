from __future__ import annotations
import json
import os
import re
import time
from typing import Type, TypeVar

from openai import OpenAI, RateLimitError
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_client: OpenAI | None = None

_RETRY_DELAYS = [5, 15, 30]  # seconds between retries on 429


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY is not set in environment")
        _client = OpenAI(api_key=api_key)
    return _client


def _retry_delay_from_error(exc: RateLimitError) -> float | None:
    """Parse 'Please try again in Xs' from the error message if present."""
    m = re.search(r"try again in ([0-9.]+)s", str(exc))
    return float(m.group(1)) + 1.0 if m else None


def call_llm_structured(
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T],
    model: str = "gpt-4o-mini",
    temperature: float = 0.1,
    debug_log: list | None = None,
) -> T:
    client = get_client()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    start = time.monotonic()
    last_exc: RateLimitError | None = None
    for attempt, fallback_delay in enumerate([0] + _RETRY_DELAYS):
        if attempt > 0:
            delay = _retry_delay_from_error(last_exc) or fallback_delay
            time.sleep(delay)
        try:
            response = client.beta.chat.completions.parse(
                model=model,
                messages=messages,
                response_format=response_model,
                temperature=temperature,
            )
            break
        except RateLimitError as exc:
            last_exc = exc
    else:
        raise last_exc

    elapsed_ms = int((time.monotonic() - start) * 1000)

    parsed = response.choices[0].message.parsed

    if debug_log is not None:
        debug_log.append({
            "type": "llm_call",
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_model": response_model.__name__,
            "response_raw": response.choices[0].message.content,
            "parsed": parsed.model_dump() if parsed else None,
            "elapsed_ms": elapsed_ms,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    if parsed is None:
        raw = response.choices[0].message.content or ""
        parsed = response_model.model_validate_json(raw)

    return parsed


def call_llm_text(
    system_prompt: str,
    user_prompt: str,
    model: str = "gpt-4o-mini",
    temperature: float = 0.2,
    debug_log: list | None = None,
) -> str:
    client = get_client()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    start = time.monotonic()
    last_exc2: RateLimitError | None = None
    for attempt, fallback_delay in enumerate([0] + _RETRY_DELAYS):
        if attempt > 0:
            delay = _retry_delay_from_error(last_exc2) or fallback_delay
            time.sleep(delay)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            break
        except RateLimitError as exc:
            last_exc2 = exc
    else:
        raise last_exc2

    elapsed_ms = int((time.monotonic() - start) * 1000)

    text = response.choices[0].message.content or ""

    if debug_log is not None:
        debug_log.append({
            "type": "llm_call_text",
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response": text,
            "elapsed_ms": elapsed_ms,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    return text
