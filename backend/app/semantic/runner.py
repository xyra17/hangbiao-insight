"""Small orchestration boundary for one offline review extraction."""

from __future__ import annotations

from .guard import SemanticGuard
from .model_gateway import ModelGateway
from .parser import SemanticParser
from .prompt_builder import PromptBuilder
from .schemas import GuardResult, ReviewInput


class SemanticExtractionService:
    def __init__(
        self,
        *,
        prompt_builder: PromptBuilder,
        gateway: ModelGateway,
        parser: SemanticParser,
        guard: SemanticGuard,
    ) -> None:
        self.prompt_builder = prompt_builder
        self.gateway = gateway
        self.parser = parser
        self.guard = guard

    def extract(self, review: ReviewInput) -> GuardResult:
        prompt = self.prompt_builder.build(review)
        response = self.gateway.extract(review, prompt)
        predictions = self.parser.parse(review, response)
        return self.guard.apply(review, predictions)
