"""LLM abstraction layer using litellm — supports OpenAI, Anthropic, Google."""

import json
from typing import Any, Optional

import litellm
import structlog

logger = structlog.get_logger()

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True


class LLMClient:
    """Thin wrapper around litellm with token counting and cost tracking."""

    def __init__(self, config: dict):
        llm_config = config.get("llm", {})
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.temperature = llm_config.get("temperature", 0.3)
        self.max_retries = llm_config.get("max_retries", 2)

        # Track cumulative usage for the current run
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost = 0.0
        self._calls: list[dict] = []

    async def complete(
        self,
        prompt: str,
        system: str = "",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        json_mode: bool = False,
    ) -> str:
        """Send a completion request and return the response text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "num_retries": self.max_retries,
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

                # Estimate cost
                cost = litellm.completion_cost(completion_response=response)
                self._total_cost += cost

                self._calls.append({
                    "model": model or self.model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                })

            logger.debug("llm_completion", model=model or self.model,
                         tokens=f"{usage.prompt_tokens}in/{usage.completion_tokens}out" if usage else "unknown")
            return content

        except Exception as e:
            logger.error("llm_completion_failed", model=model or self.model, error=str(e))
            raise

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        model: Optional[str] = None,
    ) -> dict:
        """Send a completion request and parse the response as JSON."""
        text = await self.complete(prompt, system=system, model=model, json_mode=True)
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

    def reset_usage(self) -> None:
        """Reset usage counters (call at start of each run)."""
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost = 0.0
        self._calls.clear()
