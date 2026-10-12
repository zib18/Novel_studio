"""One SDK invocation per specialist; no writer self-review or in-browser key."""
from dataclasses import dataclass
from typing import Any
from agents import Agent, Runner, RunConfig, ModelSettings
from agents.models.openai_provider import OpenAIProvider
from openai import AsyncOpenAI
from pydantic import BaseModel


@dataclass
class ProviderResult:
    output: BaseModel
    input_tokens: int
    output_tokens: int
    requests: int


class SDKProvider:
    def __init__(self, settings: Any):
        key = settings.openai_api_key
        if hasattr(key, "get_secret_value"):
            key = key.get_secret_value()
        # Disable invisible SDK retries: the durable pipeline budgets every retry.
        self.client = AsyncOpenAI(api_key=key, max_retries=0,
                                  timeout=settings.ai_timeout_seconds)
        self.model = settings.openai_model
        self.provider = OpenAIProvider(openai_client=self.client)

    async def call(self, name: str, rules: str, payload: str,
                   output_type: type[BaseModel], max_output_tokens: int) -> ProviderResult:
        agent = Agent(name=name, instructions=rules, model=self.model,
                      model_settings=ModelSettings(max_tokens=max_output_tokens, store=False),
                      output_type=output_type)
        result = await Runner.run(agent, payload, max_turns=1,
                                  run_config=RunConfig(model_provider=self.provider,
                                                       tracing_disabled=True))
        usage = result.context_wrapper.usage
        return ProviderResult(output=result.final_output,
                              input_tokens=usage.input_tokens,
                              output_tokens=usage.output_tokens,
                              requests=usage.requests)

    async def close(self) -> None:
        await self.client.close()
