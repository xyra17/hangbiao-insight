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

    def extract(self, review: ReviewInput, *, receipt_binding: str | None = None) -> GuardResult:
        # A failed attempt must never inherit the previous row's usage receipt.
        self.last_response = None
        self.last_response_binding = None
        prompt = self.prompt_builder.build(review)
        response = self.gateway.extract(review, prompt)
        self.last_response = response
        self.last_response_binding = receipt_binding
        predictions = self.parser.parse(review, response)
        return self.guard.apply(review, predictions)
