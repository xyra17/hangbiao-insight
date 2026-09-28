"""Construction boundary for the frozen offline semantic model selection."""

from __future__ import annotations

import os
from pathlib import Path

from .config import PRIMARY_SEMANTIC_CONFIG, QWEN_FALLBACK_CONFIG
from .model_gateway import DeepSeekResponsesGateway, LMStudioChatGateway, ModelGateway
from .guard import SemanticGuard
from .parser import SemanticParser
from .prompt_builder import PromptBuilder
from .runner import SemanticExtractionService
from .taxonomy import TaxonomyLoader


def build_agent_planner():
    """Create the production planner at the integration boundary; never in services."""
    from ..agent.planner import LLMPlanner
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for the agent planner")
    import httpx
    from openai import OpenAI
    from ..agent.runtime_config import InvestigationRuntimeConfig
    runtime = InvestigationRuntimeConfig()
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", max_retries=0,
                    timeout=runtime.planner_timeout_seconds,
                    http_client=httpx.Client(trust_env=False, timeout=runtime.planner_timeout_seconds))

    class Gateway:
        def plan(self, *, system, user, schema):
            response = client.responses.create(model="deepseek-v4-flash", input=[
                {"role": "system", "content": system}, {"role": "user", "content": user}],
                reasoning={"effort": "low"}, text={"format": {"type": "json_schema", "name": "agent_planner", "strict": True, "schema": schema}}, store=False)
            return response.output_text
    return LLMPlanner(Gateway())


def build_semantic_gateway(provider: str | None = None, *, runtime_overrides: dict | None = None) -> ModelGateway:
    """Build a provider adapter without exposing SDK clients to business code."""

    selected = (provider or os.getenv("SEMANTIC_PROVIDER", "deepseek")).strip().lower()
    if selected == "deepseek":
        return DeepSeekResponsesGateway(config=PRIMARY_SEMANTIC_CONFIG.model_copy(update=runtime_overrides or {}))
    if selected in {"qwen", "lmstudio"}:
        return LMStudioChatGateway(config=QWEN_FALLBACK_CONFIG.model_copy(update=runtime_overrides or {}))
    raise ValueError(f"Unsupported semantic provider: {selected}")


def build_semantic_service(
    taxonomy_path: Path,
    *,
    provider: str | None = None,
    gateway: ModelGateway | None = None,
) -> SemanticExtractionService:
    """Compose the offline extraction service around the frozen model contract."""

    selected_gateway = gateway or build_semantic_gateway(provider)
    taxonomy = TaxonomyLoader.load(taxonomy_path)
    if taxonomy.version != selected_gateway.config.taxonomy_version:
        raise ValueError("Gateway and taxonomy versions do not match")
    return SemanticExtractionService(
        prompt_builder=PromptBuilder(
            taxonomy, prompt_version=selected_gateway.config.prompt_version
        ),
        gateway=selected_gateway,
        parser=SemanticParser(),
        guard=SemanticGuard(taxonomy),
    )
