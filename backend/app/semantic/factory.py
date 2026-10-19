"""Construction boundary for the frozen offline semantic model selection."""

from __future__ import annotations

import os
from pathlib import Path

from .config import PRIMARY_SEMANTIC_CONFIG, QWEN_FALLBACK_CONFIG
from .guard import SemanticGuard
from .model_gateway import DeepSeekResponsesGateway, LMStudioChatGateway, ModelGateway
from .parser import SemanticParser
from .prompt_builder import PromptBuilder
from .runner import SemanticExtractionService
from .structured_response import dispatch, reasoning_effort, structured_request
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
    selected_model = os.getenv("INVESTIGATION_MODEL", "deepseek-flash").strip()
    if not selected_model:
        raise ValueError("explicit planner model required")
    selected_effort = reasoning_effort("INVESTIGATION_REASONING_EFFORT")
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        timeout=runtime.planner_timeout_seconds,
        http_client=httpx.Client(trust_env=False, timeout=runtime.planner_timeout_seconds),
    )

    class Gateway:
        reasoning_effort = selected_effort
        provenance = "deepseek-agent-planner-v3"
        execution_origin = "provider_response"
        timeout_seconds = runtime.planner_timeout_seconds
        last_usage = {}
        last_model_receipt = {}

        def plan(self, *, system, user, schema):
            self.last_usage, self.last_model_receipt = {}, {}
            from insight.investigation.model_limits import planner_request_limits
            limits = planner_request_limits(system, user, schema)
            request = structured_request(model=selected_model, effort=selected_effort,
                name="agent_planner", system=system, user=user, schema=schema,
                max_output_tokens=limits["max_output_tokens"])
            return dispatch(self, client, request)

    return LLMPlanner(Gateway())


def build_semantic_gateway(
    provider: str | None = None, *, runtime_overrides: dict | None = None
) -> ModelGateway:
    """Build a provider adapter without exposing SDK clients to business code."""

    selected = (provider or os.getenv("SEMANTIC_PROVIDER", "deepseek")).strip().lower()
    if selected == "deepseek":
        # Preserve frozen experiment constants; current factory selection is explicit.
        token_limit = os.getenv("SEMANTIC_MAX_OUTPUT_TOKENS")
        overrides = {
            "model": os.getenv("SEMANTIC_MODEL", "deepseek-flash"),
            "reasoning_effort": os.getenv("SEMANTIC_REASONING_EFFORT", PRIMARY_SEMANTIC_CONFIG.reasoning_effort),
            "max_output_tokens": int(token_limit) if token_limit is not None else None,
            **(runtime_overrides or {}),
        }
        if not str(overrides["model"]).strip():
            raise ValueError("explicit semantic model required")
        return DeepSeekResponsesGateway(config=PRIMARY_SEMANTIC_CONFIG.model_copy(update=overrides))
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
        prompt_builder=PromptBuilder(taxonomy, prompt_version=selected_gateway.config.prompt_version),
        gateway=selected_gateway,
        parser=SemanticParser(),
        guard=SemanticGuard(taxonomy),
    )


def build_relevance_gateway():
    """Opt-in approved provider; no retries, capped output and bounded timeout."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for relevance adjudication")
    import httpx
    from openai import OpenAI

    selected_model = os.getenv("EVIDENCE_RELEVANCE_MODEL", "deepseek-flash").strip()
    if not selected_model:
        raise ValueError("explicit relevance model required")
    timeout = min(30, max(1, int(os.environ.get("EVIDENCE_RELEVANCE_TIMEOUT_SECONDS", "10"))))
    selected_effort = reasoning_effort("EVIDENCE_RELEVANCE_REASONING_EFFORT")
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        timeout=timeout,
        http_client=httpx.Client(trust_env=False, timeout=timeout),
    )

    class RelevanceGateway:
        reasoning_effort = selected_effort
        provenance = "deepseek-relevance-v2"
        execution_origin = "provider_response"
        timeout_seconds = timeout
        last_usage = {}

        last_model_receipt = {}

        def plan(self, *, system, user, schema, max_output_tokens):
            self.last_usage, self.last_model_receipt = {}, {}
            request = structured_request(model=selected_model, effort=selected_effort,
                name="evidence_relevance", system=system, user=user, schema=schema,
                max_output_tokens=max_output_tokens)
            return dispatch(self, client, request)

    return RelevanceGateway()
