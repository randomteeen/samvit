"""
GeminiModelManager
==================
Multi-key, multi-model Gemini router with real-time quota awareness,
automatic key rotation on 429s, and an async call interface designed
to minimise total API round-trips through batched, context-rich prompts.

Model tiers (see HEAVY_MODELS / MEDIUM_MODELS / LIGHT_MODELS for the
authoritative, verified-against-the-API rosters)
-----------
  HEAVY   → gemini-3-flash-preview        (rank 1)
             gemini-2.5-flash             (rank 2)
  MEDIUM  → gemini-3.1-flash-lite-preview (rank 1)
             gemini-2.5-flash-lite        (rank 2)
  LIGHT   → gemma-4-31b-it | gemma-4-26b-a4b-it | gemini-2.0-flash-lite | gemini-2.0-flash

Key rotation rules
------------------
  * All registered keys are tried in round-robin order per tier.
  * A key is skipped for DEFAULT_HARD_BLOCK_SECS after a live 429.
  * Hard→medium downgrade is allowed; medium→light is NEVER allowed.

Request batching guidance
-------------------------
  Callers should embed the full pipeline context — requirements,
  current design state, all issues, metrics — into a SINGLE prompt so
  that plan + critique + repair decisions happen in one network call.
  The `call_gemini` method enforces a minimum token budget and will
  warn if the prompt is suspiciously short (likely under-batched).
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _extract_text(response: Any, model: str) -> str:
    """Pull text out of a generate_content response WITHOUT crashing.

    The `response.text` quick accessor raises ("requires a valid Part")
    whenever the candidate has no plain-text part — which happens for
    "thinking" models when the output budget is consumed by reasoning
    (finish_reason=MAX_TOKENS), on a safety stop, or on a recitation block.
    A bare `return response.text` therefore turns a recoverable empty
    response into an exception that callers (p03 architecture, p24 reviewer)
    catch and silently downgrade to a hardcoded heuristic stub.

    Here we (1) try the quick accessor, (2) fall back to manually joining any
    text parts, and (3) raise a *clear, descriptive* error (so the retry /
    key-and-model rotation chain can try another model) only when there is
    genuinely no usable text.
    """
    try:
        text = response.text
        if text:
            return text
    except Exception:
        pass

    candidates = getattr(response, "candidates", None) or []
    if candidates:
        cand = candidates[0]
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        joined = "".join(getattr(p, "text", "") or "" for p in parts)
        if joined.strip():
            return joined
        finish = getattr(cand, "finish_reason", None)
        raise RuntimeError(
            f"{model} returned no text part (finish_reason={finish}). "
            "Likely the output token budget was exhausted by reasoning "
            "(MAX_TOKENS) or the response was blocked."
        )

    feedback = getattr(response, "prompt_feedback", None)
    raise RuntimeError(
        f"{model} returned no candidates"
        + (f" (prompt_feedback={feedback})" if feedback else "")
    )


# ──────────────────────────────────────────────────────────────────────────────
# Quota snapshot per (api_key, model)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class QuotaSnapshot:
    """Tracks rate-limit state for one (api_key × model) pair."""
    hard_blocked_until: float = 0.0   # monotonic; 0 = not blocked
    consecutive_429s:   int   = 0

    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.hard_blocked_until

    @property
    def seconds_blocked(self) -> float:
        return max(0.0, self.hard_blocked_until - time.monotonic())

    def mark_429(self, retry_after: int = 60) -> None:
        self.hard_blocked_until = time.monotonic() + retry_after
        self.consecutive_429s  += 1

    def mark_success(self) -> None:
        self.consecutive_429s = 0


# ──────────────────────────────────────────────────────────────────────────────
# Manager
# ──────────────────────────────────────────────────────────────────────────────

class GeminiModelManager:
    """
    Central Gemini router.

    Startup
    -------
        manager = GeminiModelManager(api_keys=["KEY_A", "KEY_B"])

    Usage
    -----
        response_text = await manager.call_gemini(prompt, task="heavy")
        response_text = await manager.call_gemini(prompt, task="medium")
    """

    # ── Model rosters ────────────────────────────────────────────────────────
    HEAVY_MODELS: List[str] = [
        "gemini-3-flash-preview",      # heavy rank 1 — Gemini 3 Flash (5 RPM / 20 RPD)
        "gemini-2.5-flash",            # heavy rank 2 — Gemini 2.5 Flash (5 RPM / 20 RPD)
    ]
    MEDIUM_MODELS: List[str] = [
        "gemini-3.1-flash-lite-preview", # medium rank 1 — Gemini 3.1 Flash Lite (15 RPM / 500 RPD)
        "gemini-2.5-flash-lite",         # medium rank 2 — Gemini 2.5 Flash Lite (10 RPM / 250K TPM)
    ]
    LIGHT_MODELS: List[str] = [
        # Verified against the Gemini API model list. The previous roster listed
        # gemma-3-* ids and "gemma-4-27b-it", none of which resolve (404
        # NOT_FOUND) — that made the entire light tier unusable. These are the
        # cheap models the API actually exposes for generateContent.
        "gemma-4-31b-it",              # light rank 1 — Gemma 4 31B Dense
        "gemma-4-26b-a4b-it",          # light rank 2 — Gemma 4 26B MoE A4B
        "gemini-2.0-flash-lite",       # light rank 3 — reliable cheap fallback
        "gemini-2.0-flash",            # light rank 4 — reliable cheap fallback
    ]

    TIER_MAP: Dict[str, List[str]] = {
        "heavy":  HEAVY_MODELS,
        "medium": MEDIUM_MODELS,
        "light":  LIGHT_MODELS,
    }

    DEFAULT_HARD_BLOCK_SECS: int  = 65
    MAX_RETRIES:              int  = 6
    MIN_PROMPT_CHARS:         int  = 200   # warn if prompt is too short (under-batched)
    DEFAULT_CALL_TIMEOUT_SECS: float = 120.0  # hard cap on a single Gemini round-trip

    # Tier downgrade chain. When every (key × model) in the primary tier is
    # rate-limited / exhausted, fall through to the next tier instead of just
    # sleeping. heavy → medium is allowed; medium → light is intentionally NOT
    # (a too-weak model produces unusable repair plans).
    TIER_FALLBACK: Dict[str, List[str]] = {
        "heavy":  ["heavy", "medium"],
        "medium": ["medium"],
        "light":  ["light"],
    }

    def __init__(
        self,
        api_keys: List[str],
        default_tier: str = "heavy",
        max_output_tokens: int = 8192,
        call_timeout_secs: float = DEFAULT_CALL_TIMEOUT_SECS,
    ) -> None:
        if not api_keys:
            raise ValueError("At least one Gemini API key is required.")

        self.api_keys          = list(api_keys)
        self.default_tier      = default_tier
        self.max_output_tokens = max_output_tokens
        self.call_timeout_secs = call_timeout_secs

        # (api_key, model) → QuotaSnapshot
        self._quotas: Dict[Tuple[str, str], QuotaSnapshot] = {}
        self._lock   = threading.Lock()

        all_models = self.HEAVY_MODELS + self.MEDIUM_MODELS + self.LIGHT_MODELS
        for key in self.api_keys:
            for model in all_models:
                self._quotas[(key, model)] = QuotaSnapshot()

        # Round-robin pointer per tier
        self._rr: Dict[str, int] = {"heavy": 0, "medium": 0, "light": 0}

        logger.info(
            "GeminiModelManager ready — %d key(s), default_tier=%s",
            len(self.api_keys), self.default_tier,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _quota(self, key: str, model: str) -> QuotaSnapshot:
        with self._lock:
            return self._quotas[(key, model)]

    def _mark_429(self, key: str, model: str, retry_after: int) -> None:
        with self._lock:
            self._quotas[(key, model)].mark_429(retry_after)
        logger.warning(
            "429 on key=...%s model=%s — blocked for %ds",
            key[-4:], model, retry_after,
        )

    def _mark_success(self, key: str, model: str) -> None:
        with self._lock:
            self._quotas[(key, model)].mark_success()

    def _tier_chain(self, task: str) -> List[str]:
        """Ordered list of tiers to try for a requested task (primary first)."""
        return self.TIER_FALLBACK.get(task, [task])

    def _pick(self, task: str) -> Optional[Tuple[str, str, str]]:
        """
        Pick the next available (api_key, model, tier) for the requested task,
        walking the tier-fallback chain (e.g. heavy → medium) so a fully
        rate-limited primary tier transparently downgrades instead of stalling.
        Rotates through keys in round-robin; tries each model in priority order.
        Returns None only if every model in every fallback tier is blocked.
        """
        n_keys = len(self.api_keys)
        for tier in self._tier_chain(task):
            models = self.TIER_MAP.get(tier, self.HEAVY_MODELS)
            for model in models:
                start = self._rr.get(tier, 0)
                for offset in range(n_keys):
                    idx = (start + offset) % n_keys
                    key = self.api_keys[idx]
                    snap = self._quota(key, model)
                    if snap.is_available:
                        self._rr[tier] = (idx + 1) % n_keys
                        return key, model, tier

        return None  # everything blocked across all fallback tiers

    def _make_client(self, api_key: str) -> Any:
        """Lazily import google.generativeai and return a configured client."""
        try:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=api_key)
            return genai
        except ImportError:
            raise RuntimeError(
                "google-generativeai is not installed. "
                "Run: pip install google-generativeai"
            )

    # ── Public API ───────────────────────────────────────────────────────────

    async def call_gemini(
        self,
        prompt: str,
        task: str = "heavy",
        system_instruction: str = "",
        temperature: float = 0.2,
        model_override: Optional[str] = None,
    ) -> str:
        """
        Send a prompt to Gemini and return the response text.

        Parameters
        ----------
        prompt:             The full user prompt. Include ALL context needed
                            so the model can complete multiple sub-tasks in
                            one call (plan + critique + repair = 1 request).
        task:               Tier to use: "heavy" | "medium" | "light".
        system_instruction: Optional system prompt prepended to the call.
        temperature:        Sampling temperature (0.0–1.0).
        model_override:     Force a specific model name regardless of tier.

        Returns
        -------
        Response text string.
        """
        if len(prompt) < self.MIN_PROMPT_CHARS:
            logger.warning(
                "Prompt is very short (%d chars). Consider batching more "
                "context into a single call to reduce total API requests.",
                len(prompt),
            )

        last_error: Optional[Exception] = None
        t_overall = time.monotonic()

        for attempt in range(self.MAX_RETRIES):
            tier_used = task
            if model_override:
                # Try each key in order for the overridden model
                pair = None
                for key in self.api_keys:
                    snap = self._quota(key, model_override)
                    if snap.is_available:
                        pair = (key, model_override, task)
                        break
            else:
                pair = self._pick(task)

            if pair is None:
                # Everything blocked across all fallback tiers — wait for the
                # shortest block to expire, reporting WHY so it's clearly a
                # rate-limit wait and not a hang.
                tiers = self._tier_chain(task)
                min_wait = min(
                    self._quota(k, m).seconds_blocked
                    for k in self.api_keys
                    for t in tiers
                    for m in self.TIER_MAP.get(t, self.HEAVY_MODELS)
                )
                wait = max(min_wait, 2.0)
                logger.warning(
                    "⏳ Gemini: all keys/models in tier(s) %s rate-limited "
                    "(attempt %d/%d) — waiting %.1fs for quota to free up …",
                    tiers, attempt + 1, self.MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            api_key, model, tier_used = pair
            if tier_used != task:
                logger.warning(
                    "⬇️  Gemini downgrade: requested tier '%s' is exhausted — "
                    "falling back to '%s' model=%s",
                    task, tier_used, model,
                )

            logger.info(
                "→ Gemini request: tier=%s model=%s key=...%s attempt=%d/%d "
                "prompt=%d chars timeout=%.0fs",
                tier_used, model, api_key[-4:], attempt + 1, self.MAX_RETRIES,
                len(prompt), self.call_timeout_secs,
            )
            t_call = time.monotonic()

            try:
                response_text = await asyncio.wait_for(
                    self._call_once(api_key, model, prompt, system_instruction, temperature),
                    timeout=self.call_timeout_secs,
                )
                self._mark_success(api_key, model)
                logger.info(
                    "✅ Gemini call OK — model=%s key=...%s attempt=%d  %.1fs "
                    "(%d chars in / %d chars out)",
                    model, api_key[-4:], attempt + 1, time.monotonic() - t_call,
                    len(prompt), len(response_text or ""),
                )
                return response_text

            except asyncio.TimeoutError:
                # Not a hang — the call exceeded the per-request budget. Log it
                # explicitly and retry (with backoff) on a fresh key/model.
                last_error = TimeoutError(
                    f"Gemini call exceeded {self.call_timeout_secs:.0f}s")
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "⏱️  Gemini TIMEOUT after %.0fs — model=%s key=...%s "
                    "attempt %d/%d — retrying in %ds (will rotate key/model)",
                    self.call_timeout_secs, model, api_key[-4:],
                    attempt + 1, self.MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            except Exception as exc:
                last_error = exc
                exc_str    = str(exc).lower()
                elapsed    = time.monotonic() - t_call

                if "429" in exc_str or "quota" in exc_str or "resource_exhausted" in exc_str:
                    retry_after = self._parse_retry_after(exc) or self.DEFAULT_HARD_BLOCK_SECS
                    self._mark_429(api_key, model, retry_after)
                    logger.warning(
                        "🚦 Gemini RATE-LIMIT (429) — model=%s key=...%s after %.1fs "
                        "— blocking %ds and retrying on another key/model "
                        "(may downgrade tier)",
                        model, api_key[-4:], elapsed, retry_after,
                    )
                    # Immediately retry — _pick will select a different key/model/tier
                    continue

                if "400" in exc_str or "invalid" in exc_str:
                    logger.error("❌ Bad request to Gemini (model=%s): %s", model, exc)
                    raise

                # Transient network / server error — exponential backoff
                wait = 2 ** attempt
                logger.warning(
                    "⚠️  Gemini error attempt %d/%d (model=%s, %.1fs): %s "
                    "— retrying in %ds",
                    attempt + 1, self.MAX_RETRIES, model, elapsed, exc, wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"Gemini call failed after {self.MAX_RETRIES} attempts "
            f"({time.monotonic() - t_overall:.0f}s total). Last error: {last_error}"
        )

    async def _call_once(
        self,
        api_key: str,
        model: str,
        prompt: str,
        system_instruction: str,
        temperature: float,
    ) -> str:
        """Single blocking Gemini call wrapped in asyncio.to_thread."""
        def _sync_call() -> str:
            import google.generativeai as genai  # type: ignore
            genai.configure(api_key=api_key)

            config = {"temperature": temperature, "max_output_tokens": self.max_output_tokens}

            if system_instruction:
                gen_model = genai.GenerativeModel(
                    model_name=model,
                    system_instruction=system_instruction,
                    generation_config=config,
                )
            else:
                gen_model = genai.GenerativeModel(
                    model_name=model,
                    generation_config=config,
                )

            response = gen_model.generate_content(prompt)
            return _extract_text(response, model)

        return await asyncio.to_thread(_sync_call)

    @staticmethod
    def _parse_retry_after(exc: Exception) -> Optional[int]:
        """Extract retry-after seconds from a 429 error if present."""
        try:
            msg = str(exc)
            import re
            m = re.search(r"retry.?after[^\d]*(\d+)", msg, re.IGNORECASE)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None

    def status_report(self) -> Dict[str, Any]:
        """Return a dict summarising current quota state for all key×model pairs."""
        report: Dict[str, Any] = {}
        with self._lock:
            for (key, model), snap in self._quotas.items():
                label = f"...{key[-4:]}/{model}"
                report[label] = {
                    "available":         snap.is_available,
                    "blocked_for_secs":  snap.seconds_blocked,
                    "consecutive_429s":  snap.consecutive_429s,
                }
        return report
