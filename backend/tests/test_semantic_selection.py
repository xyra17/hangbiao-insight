from pathlib import Path

import pytest

from app.semantic.config import PRIMARY_SEMANTIC_CONFIG, QWEN_FALLBACK_CONFIG
from app.semantic.factory import build_semantic_gateway, build_semantic_service
from app.semantic.model_gateway import ModelGateway


TAXONOMY_PATH = (
    Path(__file__).resolve().parents[2]
    / "dataset"
    / "data"
    / "03_黄金评论集"
    / "主题分类体系_v1_1.json"
)


class FrozenGateway(ModelGateway):
    @property
    def config(self):
        return PRIMARY_SEMANTIC_CONFIG

    def extract(self, review, prompt):  # pragma: no cover - composition test only
        raise AssertionError("No model call expected")


def test_frozen_semantic_selection_uses_v1_0_for_primary_and_fallback():
    assert PRIMARY_SEMANTIC_CONFIG.provider == "deepseek"
    assert PRIMARY_SEMANTIC_CONFIG.model == "deepseek-v4-flash"
    assert PRIMARY_SEMANTIC_CONFIG.prompt_version == "semantic-prompt-v1.0"
    assert PRIMARY_SEMANTIC_CONFIG.taxonomy_version == "taxonomy-v1.1"
    assert QWEN_FALLBACK_CONFIG.provider == "lmstudio"
    assert QWEN_FALLBACK_CONFIG.prompt_version == "semantic-prompt-v1.0"


def test_semantic_gateway_factory_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unsupported semantic provider"):
        build_semantic_gateway("unknown")


def test_semantic_service_composes_frozen_prompt_taxonomy_and_gateway():
    service = build_semantic_service(TAXONOMY_PATH, gateway=FrozenGateway())
    assert service.gateway.config.model == "deepseek-v4-flash"
    assert service.prompt_builder.prompt_version == "semantic-prompt-v1.0"
    assert service.guard.taxonomy.version == "taxonomy-v1.1"
