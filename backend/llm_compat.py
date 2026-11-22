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
  * GROQ_API_KEY     (optional fallback — kicks in automatically when
                      the primary provider keeps returning 503 / overloaded.
                      Model: llama-3.3-70b-versatile.)

Usage identical to Emergent's original:

    chat = LlmChat(api_key=key, session_id="abc", system_message="You are …")
    chat = chat.with_model("openai", "gpt-4o-mini")   # or "gemini", "gemini-3.8-flash"
    response = await chat.send_message(UserMessage(text="hello"))

Automatic failover: if GROQ_API_KEY is set and the primary provider keeps
failing with transient errors (after its own retries), Groq Llama 3.3 70B
is tried silently. The caller gets a successful response instead of a 503.

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

    # Env var names per provider
    _ENV_MAP = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "groq": "GROQ_API_KEY",
    }

    # Fallback chain used when the primary provider keeps returning transient
    # failures. Order matters: first available key wins. Groq is cheap/free and
    # extremely fast, so it's the natural safety net.
    _FALLBACK_CHAIN = [
        ("groq", "llama-3.3-70b-versatile"),
    ]

    def _resolve_key(self, provider: Optional[str] = None) -> Optional[str]:
        """Pick the env var that matches the configured (or requested) provider."""
        provider = provider or self.provider
        # Only honor the caller-supplied api_key for the primary provider,
        # never for fallbacks (they'd need their own key).
        if provider == self.provider and self.api_key:
            return self.api_key
        return os.environ.get(self._ENV_MAP.get(provider, ""))

    def _resolve_model_id(self, provider: Optional[str] = None,
                          model: Optional[str] = None) -> str:
        """Build the provider-prefixed model id that litellm expects."""
        provider = provider or self.provider
        model = model or self.model
        if provider == "openai":
            return model
        return f"{provider}/{model}"

    def _build_provider_chain(self) -> list:
        """
        Build the ordered list of (provider, model) attempts to try.
        Starts with the primary, then any fallback whose key is available
        and that isn't already the primary.
        """
        chain = [(self.provider, self.model)]
        for fb_provider, fb_model in self._FALLBACK_CHAIN:
            if fb_provider == self.provider:
                continue  # already primary, don't duplicate
            if os.environ.get(self._ENV_MAP.get(fb_provider, "")):
                chain.append((fb_provider, fb_model))
        return chain

    async def _try_one_provider(
        self,
        provider: str,
        model: str,
        messages: list,
    ) -> tuple:
        """
        Attempt a single provider with retry on transient failures.
        Returns (success, response_or_error):
          - (True, str) on success
          - (False, Exception) on failure (after all retries)
        """
        key = self._resolve_key(provider)
        if not key:
            return (False, RuntimeError(f"No API key for provider '{provider}'"))

        model_id = self._resolve_model_id(provider, model)
        last_error: Optional[Exception] = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await litellm.acompletion(
                    model=model_id,
                    messages=messages,
                    api_key=key,
                    timeout=30,
                )
                content = response.choices[0].message.content or ""
                if attempt > 0:
                    logger.info("LLM call recovered on attempt %d (%s/%s)",
                                attempt + 1, provider, model)
                return (True, content)
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_transient = any(marker in err_str for marker in _RETRYABLE_MARKERS)
                remaining = _MAX_RETRIES - attempt
                if is_transient and remaining > 0:
                    wait = _BACKOFF_BASE * (2 ** attempt)  # 1s, 2s
                    logger.warning(
                        "LLM transient failure (%s/%s), retrying in %.1fs "
                        "(%d left): %s",
                        provider, model, wait, remaining, e,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error("LLM call failed (%s/%s) after %d attempt(s): %s",
                             provider, model, attempt + 1, e)
                break

        return (False, last_error or RuntimeError("Unknown LLM failure"))

    async def send_message(self, message: UserMessage) -> str:
        """
        Send one chat turn with automatic retry + provider failover.

        Retry policy per provider:
          - Up to 3 total attempts (1 initial + 2 retries) when the error
            looks transient (503, 429, timeout, "overloaded"...)
          - Exponential backoff: 1s, 2s between retries

        Failover: if the primary provider keeps failing with transient errors,
        try each fallback in _FALLBACK_CHAIN that has an API key set (Groq
        Llama 3.3 70B by default). User gets a successful response instead
        of a 503 as long as one provider is healthy.
        """
        if litellm is None:
            raise HTTPException(
                status_code=503,
                detail="LLM backend not available: install `litellm`.",
            )

        chain = self._build_provider_chain()

        # Sanity: at least the primary must have a key, or fail early with
        # a clear message (same behaviour as before).
        if not self._resolve_key(chain[0][0]):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"AI features are disabled. Set the API key for "
                    f"'{chain[0][0]}' in the server environment."
                ),
            )

        messages = []
        if self.system_message:
            messages.append({"role": "system", "content": self.system_message})
        messages.append({"role": "user", "content": message.text})

        last_error: Optional[Exception] = None
        for idx, (provider, model) in enumerate(chain):
            ok, result = await self._try_one_provider(provider, model, messages)
            if ok:
                if idx > 0:
                    logger.warning(
                        "LLM primary (%s/%s) exhausted retries — "
                        "served from fallback (%s/%s)",
                        self.provider, self.model, provider, model,
                    )
                return result
            last_error = result if isinstance(result, Exception) else RuntimeError(str(result))
            if idx < len(chain) - 1:
                logger.warning(
                    "LLM provider (%s/%s) failed, falling back to next in chain",
                    provider, model,
                )

        # All providers in chain exhausted — surface a specific error.
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
