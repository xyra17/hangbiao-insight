"""Explicit provider configuration for production and local Golden evaluation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import PROMPT_VERSION


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai", "lmstudio", "deepseek"]
    model: str = Field(min_length=1)
    api_name: Literal["responses", "chat_completions_compatible"]
    reasoning_effort: str = "low"
    structured_output: Literal["json_schema"] = "json_schema"
    strict: Literal[True] = True
    prompt_version: str = PROMPT_VERSION
    taxonomy_version: str = "taxonomy-v1.1"
    max_attempts: int = Field(default=3, ge=1, le=5)
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    base_url: str | None = None


OPENAI_PRODUCTION_CONFIG = ProviderConfig(
    provider="openai",
    model="gpt-5.6-sol",
    api_name="responses",
    reasoning_effort="low",
    max_attempts=3,
    timeout_seconds=120,
)


LOCAL_DEV_CONFIG = ProviderConfig(
    provider="lmstudio",
    model="stage55a-qwen3.5-9b",
    api_name="chat_completions_compatible",
    reasoning_effort="low",
    prompt_version="semantic-prompt-v1.1",
    max_attempts=2,
    timeout_seconds=180,
    base_url="http://127.0.0.1:1234/v1",
)

QWEN_FALLBACK_CONFIG = LOCAL_DEV_CONFIG.model_copy(
    update={"prompt_version": "semantic-prompt-v1.0"}
)
LOCAL_DEV_V12_CONFIG = LOCAL_DEV_CONFIG.model_copy(update={"prompt_version": "semantic-prompt-v1.2"})

DEEPSEEK_FALLBACK_CONFIG = ProviderConfig(
    provider="deepseek",
    model="deepseek-v4-flash",
    api_name="responses",
    reasoning_effort="low",
    prompt_version="semantic-prompt-v1.0",
    max_attempts=3,
    timeout_seconds=180,
    base_url="https://api.deepseek.com",
)

# Frozen Stage 5.5A engineering selection. Experiment configs remain available only
# for reproducibility and must not silently become the runtime default.
PRIMARY_SEMANTIC_CONFIG = DEEPSEEK_FALLBACK_CONFIG
