"""
Drop-in replacement for the `emergentintegrations.llm.chat` module.

The Emergent platform shipped a private wrapper around OpenAI/Gemini.
After migrating off Emergent we no longer have that package, but a lot of
existing routers (ai_tools.py, astro.py) still import `LlmChat` / `UserMessage`
with a specific API shape. This shim keeps those routers working by
re-implementing the same tiny surface on top of `litellm` (already in
requirements.txt), which talks to OpenAI, Gemini, Anthropic, etc.

Required env vars (whichever provider you plan to use):
  * OPENAI_API_KEY
  * GEMINI_API_KEY   (for provider="gemini")
  * ANTHROPIC_API_KEY

Usage identical to Emergent's original:

    chat = LlmChat(api_key=key, session_id="abc", system_message="You are …")
    chat = chat.with_model("openai", "gpt-4o-mini")   # or "gemini", "gemini-3.8-flash"
    response = await chat.send_message(UserMessage(text="hello"))

If no provider key is set, `send_message` raises an HTTPException(503)
with a human-readable message instead of a cryptic SDK error.
"""

import asyncio
import os
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

try:
    import litellm
except ImportError:  # pragma: no cover
    litellm = None

logger = logging.getLogger(__name__)

# Up to 3 attempts total (1 original + 2 retries) with 1s, 2s backoff.
# Covers transient 503 / 429 / network blips without blocking UX too long.
_MAX_RETRIES = 2
_BACKOFF_BASE = 1.0  # seconds; actual wait = base * 2**attempt

# Error substrings that indicate a transient server-side failure worth
# retrying. Match litellm's error.__str__() output across providers.
_RETRYABLE_MARKERS = (
    "503", "429",
    "unavailable", "overloaded", "rate limit", "too many requests",
    "timeout", "timed out", "connection reset", "temporarily",
)


@dataclass
class UserMessage:
    """Minimal user message wrapper, mirrors the original API."""
    text: str


class LlmChat:
    """Session-less chat object. One instance per call is fine."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        session_id: str = "",
        system_message: str = "",
    ) -> None:
        self.api_key = api_key
        self.session_id = session_id
        self.system_message = system_message
        self.provider = "openai"
        self.model = "gpt-4o-mini"

    def with_model(self, provider: str, model: str) -> "LlmChat":
        self.provider = provider
        self.model = model
        return self

    def _resolve_key(self) -> Optional[str]:
        """Pick the env var that matches the configured provider."""
        if self.api_key:
            return self.api_key
        env_map = {
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        return os.environ.get(env_map.get(self.provider, ""))

    def _resolve_model_id(self) -> str:
        """Build the provider-prefixed model id that litellm expects."""
        if self.provider == "openai":
            return self.model
        return f"{self.provider}/{self.model}"

    async def send_message(self, message: UserMessage) -> str:
        """
        Send one chat turn with automatic retry on transient failures.

        Retry policy:
          - Up to 3 total attempts (1 initial + 2 retries) when the error
            looks transient (503, 429, timeout, "overloaded"...)
          - Exponential backoff: 1s, 2s between retries
          - Non-transient errors (401, 400, model not found) bail immediately
        """
        if litellm is None:
            raise HTTPException(
                status_code=503,
                detail="LLM backend not available: install `litellm`.",
            )
        key = self._resolve_key()
        if not key:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"AI features are disabled. Set the API key for "
                    f"'{self.provider}' in the server environment."
                ),
            )

        messages = []
        if self.system_message:
            messages.append({"role": "system", "content": self.system_message})
        messages.append({"role": "user", "content": message.text})

        last_error: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await litellm.acompletion(
                    model=self._resolve_model_id(),
                    messages=messages,
                    api_key=key,
                    timeout=30,
                )
                return response.choices[0].message.content or ""
            except HTTPException:
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_transient = any(marker in err_str for marker in _RETRYABLE_MARKERS)
                remaining = _MAX_RETRIES - attempt
                if is_transient and remaining > 0:
                    wait = _BACKOFF_BASE * (2 ** attempt)  # 1s, 2s
                    logger.warning(
                        "LLM call transient failure (%s/%s), retrying in %.1fs "
                        "(%d attempts left): %s",
                        self.provider, self.model, wait, remaining, e,
                    )
                    await asyncio.sleep(wait)
                    continue
                # Give up — surface a clear, user-facing message
                logger.error("LLM call failed (%s/%s) after %d attempt(s): %s",
                             self.provider, self.model, attempt + 1, e)
                break

        # Translate the final error into a specific HTTPException so the
        # frontend can show something more useful than "Request failed".
        err_str = str(last_error).lower() if last_error else ""
        if "503" in err_str or "unavailable" in err_str or "overloaded" in err_str:
            raise HTTPException(
                status_code=503,
                detail="AI service is temporarily busy. Please try again in a moment.",
            )
        if "429" in err_str or "rate limit" in err_str or "too many requests" in err_str:
            raise HTTPException(
                status_code=429,
                detail="Too many requests to the AI service. Please wait a moment and retry.",
            )
        if "timeout" in err_str or "timed out" in err_str:
            raise HTTPException(
                status_code=504,
                detail="AI service took too long to respond. Please try again.",
            )
        raise HTTPException(status_code=502, detail=f"LLM request failed: {last_error}")
