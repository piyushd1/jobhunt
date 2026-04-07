"""LLM abstraction layer using litellm — supports OpenRouter, Groq, OpenAI, Anthropic, Google."""

import asyncio
import json
import os
from typing import Any, Optional

import litellm
import structlog

logger = structlog.get_logger()

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True


def _setup_provider_keys(llm_config: dict) -> None:
    """Set env vars that litellm expects for each provider.

    litellm reads keys from env vars by convention:
      - OpenRouter: OPENROUTER_API_KEY
      - Groq: GROQ_API_KEY
      - OpenAI: OPENAI_API_KEY
      - Anthropic: ANTHROPIC_API_KEY
      - Google: GEMINI_API_KEY
    Our config loader already loads them from .env, but litellm
    needs them as actual env vars.
    """
    key_map = {
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "groq_api_key": "GROQ_API_KEY",
        "api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "google_api_key": "GEMINI_API_KEY",
    }
    for config_key, env_var in key_map.items():
        value = llm_config.get(config_key, "")
        if value and not os.getenv(env_var):
            os.environ[env_var] = value


class LLMClient:
    """Thin wrapper around litellm with per-agent model selection and cost tracking.

    Model format examples (litellm conventions):
      - OpenRouter: "openrouter/google/gemini-2.0-flash-exp:free"
      - Groq:       "groq/llama-3.1-8b-instant"
      - OpenAI:     "gpt-4o-mini"
      - Anthropic:  "claude-3-haiku-20240307"
      - Google:     "gemini/gemini-1.5-flash"
    """

    def __init__(self, config: dict):
        llm_config = config.get("llm", {})
        self.default_model = llm_config.get("default_model", "groq/llama-3.1-8b-instant")
        self.fallback_model = llm_config.get("fallback_model", "")
        self.temperature = llm_config.get("temperature", 0.3)
        self.max_retries = llm_config.get("max_retries", 3)
        self.backoff_base = llm_config.get("backoff_base_s", 5)  # seconds

        # Per-agent model + fallback chains
        # Config can be either a string (old format) or a dict with model + fallback
        self._agent_models: dict[str, str] = {}
        self._agent_fallbacks: dict[str, str] = {}
        for agent_name, agent_config in llm_config.get("agents", {}).items():
            if isinstance(agent_config, dict):
                if agent_config.get("model"):
                    self._agent_models[agent_name] = agent_config["model"]
                if agent_config.get("fallback"):
                    self._agent_fallbacks[agent_name] = agent_config["fallback"]
            elif isinstance(agent_config, str) and agent_config:
                self._agent_models[agent_name] = agent_config

        _setup_provider_keys(llm_config)

        # Track cumulative usage for the current run
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost = 0.0
        self._calls: list[dict] = []

    def model_for(self, agent: str) -> str:
        """Get the primary model configured for a specific agent, or the default."""
        return self._agent_models.get(agent, self.default_model)

    def fallback_for(self, agent: str) -> str:
        """Get the fallback model for an agent, or the global fallback."""
        return self._agent_fallbacks.get(agent, self.fallback_model)

    async def complete(
        self,
        prompt: str,
        system: str = "",
        agent: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        json_mode: bool = False,
    ) -> str:
        """Send a completion request with exponential backoff and fallback model.

        Model resolution order:
          1. Explicit `model` param (for one-off overrides)
          2. Per-agent model from config (via `agent` param)
          3. default_model from config

        On rate limit errors: retries with exponential backoff (5s, 15s, 45s),
        then falls back to fallback_model if configured.
        """
        resolved_model = model or (self.model_for(agent) if agent else self.default_model)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Try primary model with backoff, then per-agent fallback, then global fallback
        models_to_try = [resolved_model]
        agent_fb = self.fallback_for(agent) if agent else self.fallback_model
        if agent_fb and agent_fb != resolved_model:
            models_to_try.append(agent_fb)
        if self.fallback_model and self.fallback_model not in models_to_try:
            models_to_try.append(self.fallback_model)

        last_error = None
        for try_model in models_to_try:
            for attempt in range(self.max_retries + 1):
                kwargs: dict[str, Any] = {
                    "model": try_model,
                    "messages": messages,
                    "temperature": temperature if temperature is not None else self.temperature,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                try:
                    response = await litellm.acompletion(**kwargs)
                    content = response.choices[0].message.content or ""

                    # Track usage
                    usage = response.usage
                    if usage:
                        input_tokens = usage.prompt_tokens or 0
                        output_tokens = usage.completion_tokens or 0
                        self._total_input_tokens += input_tokens
                        self._total_output_tokens += output_tokens

                        try:
                            cost = litellm.completion_cost(completion_response=response)
                        except Exception:
                            cost = 0.0
                        self._total_cost += cost

                        self._calls.append({
                            "model": try_model,
                            "agent": agent or "unknown",
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cost_usd": cost,
                        })

                    logger.debug("llm_completion", model=try_model, agent=agent,
                                 tokens=f"{usage.prompt_tokens}in/{usage.completion_tokens}out" if usage else "unknown")
                    return content

                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    is_rate_limit = "rate" in error_str or "429" in error_str

                    if is_rate_limit and attempt < self.max_retries:
                        wait = self.backoff_base * (3 ** attempt)  # 5s, 15s, 45s
                        logger.warning("llm_rate_limited", model=try_model, attempt=attempt + 1,
                                       wait_s=wait)
                        await asyncio.sleep(wait)
                        continue
                    elif is_rate_limit and try_model != models_to_try[-1]:
                        logger.warning("llm_switching_to_fallback", from_model=try_model,
                                       to_model=models_to_try[-1])
                        break  # Try next model
                    else:
                        logger.error("llm_completion_failed", model=try_model, agent=agent,
                                     error=str(e))
                        if not is_rate_limit:
                            raise

        raise last_error

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Send a completion request and parse the response as JSON."""
        text = await self.complete(prompt, system=system, agent=agent, model=model, json_mode=True)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code block
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            return json.loads(text)

    def get_usage_summary(self) -> dict:
        """Return cumulative usage stats for the current run."""
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cost_usd": round(self._total_cost, 6),
            "total_calls": len(self._calls),
            "calls": self._calls,
        }

    def get_model_config_summary(self) -> dict:
        """Show which model each agent will use — useful for setup/debugging."""
        all_agents = ["resume_profiler", "parsing", "matching", "leadgen", "messaging"]
        agent_configs = {}
        for agent in all_agents:
            primary = self.model_for(agent)
            fallback = self.fallback_for(agent)
            is_default = agent not in self._agent_models
            agent_configs[agent] = {
                "primary": f"{primary}{' (default)' if is_default else ''}",
                "fallback": fallback or "(none)",
            }
        return {
            "default_model": self.default_model,
            "global_fallback": self.fallback_model,
            "agents": agent_configs,
        }

    def reset_usage(self) -> None:
        """Reset usage counters (call at start of each run)."""
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost = 0.0
        self._calls.clear()
