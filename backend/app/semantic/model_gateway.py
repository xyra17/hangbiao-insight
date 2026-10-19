"""Provider-neutral model boundary with OpenAI and local LM Studio adapters."""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .config import ProviderConfig
from .prompt_builder import PromptPackage
from .schemas import GatewayResponse, ReviewInput
from .structured_response import StructuredOutputError


def bind_extraction_payload(payload, prompt):
    """Expand only the explicit frozen pair; preserve raw provider output elsewhere."""
    from .structured_response import StructuredOutputError, parse_structured_object, validate_wire_value

    value, normalized, _normalization = parse_structured_object(payload)
    try:
        validate_wire_value(value, prompt.response_schema)
    except ValueError as exc:
        raise StructuredOutputError("structured_output_schema_invalid", str(exc)) from None
    if prompt.topic_bindings is None:
        return normalized
    canonical = []
    for claim in value["claims"]:
        primary, secondary = prompt.topic_bindings[claim["topic_ref" if prompt.prompt_version == "semantic-prompt-v1.7" else "topic_pair"]]
        canonical.append({"primary_topic": primary, "secondary_topic": secondary,
            "polarity": claim["polarity"], "evidence_span": (prompt.evidence_bindings[claim["evidence_ref"]]
                if prompt.evidence_bindings is not None else claim["evidence_span"])})
    return json.dumps({"claims": canonical}, ensure_ascii=False)


class ModelGatewayError(RuntimeError):
    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class SemanticResponseError(ModelGatewayError):
    """A received response is unusable; never redispatch to repair it."""

    def __init__(self, code: str, response: Any) -> None:
        super().__init__(code, attempts=1)
        self.code = code
        self.response_id = getattr(response, "id", None)
        self.raw_response = response.model_dump(mode="json") if hasattr(response, "model_dump") else None


class ModelGateway(ABC):
    @property
    @abstractmethod
    def config(self) -> ProviderConfig:
        """Return immutable provider/runtime configuration."""

    @abstractmethod
    def extract(self, review: ReviewInput, prompt: PromptPackage) -> GatewayResponse:
        """Return a structured extraction payload without applying business guards."""


class _RetryingGateway(ModelGateway):
    def __init__(self, *, config: ProviderConfig, sleep: Callable[[float], None] = time.sleep) -> None:
        self._config = config
        self._sleep = sleep

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def _run_with_retry(self, operation: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:  # provider SDKs do not share one stable exception base
                last_error = exc
                if attempt < self.config.max_attempts:
                    self._sleep(min(2 ** (attempt - 1), 4))
        raise ModelGatewayError(
            f"{self.config.provider} request failed after {self.config.max_attempts} attempts: {last_error}",
            attempts=self.config.max_attempts,
        ) from last_error

    def _response(
        self,
        *,
        prompt: PromptPackage,
        payload: str,
        response_id: str | None,
        raw_response: dict[str, Any] | None,
    ) -> GatewayResponse:
        return GatewayResponse(
            payload=payload,
            provider=self.config.provider,
            api_name=self.config.api_name,
            model_name=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
            prompt_version=prompt.prompt_version,
            taxonomy_version=self.config.taxonomy_version,
            raw_response_ref=response_id,
            raw_response=raw_response,
        )


class OpenAIResponsesGateway(_RetryingGateway):
    """Official OpenAI Responses API with strict JSON Schema output."""

    def __init__(
        self,
        *,
        config: ProviderConfig,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.provider != "openai" or config.api_name != "responses":
            raise ValueError("OpenAIResponsesGateway requires provider=openai and api_name=responses")
        if client is None:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is required for the OpenAI provider")
            from openai import OpenAI

            client = OpenAI(api_key=api_key, max_retries=0, timeout=config.timeout_seconds)
        super().__init__(config=config, sleep=sleep)
        self.client = client

    def extract(self, review: ReviewInput, prompt: PromptPackage) -> GatewayResponse:
        def request() -> Any:
            return self.client.responses.create(
                model=self.config.model,
                instructions=prompt.system,
                input=prompt.user,
                reasoning={"effort": self.config.reasoning_effort},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "review_semantic_extraction",
                        "strict": self.config.strict,
                        "schema": prompt.response_schema,
                    }
                },
                metadata={
                    "review_id": review.review_id,
                    "prompt_version": prompt.prompt_version,
                    "taxonomy_version": self.config.taxonomy_version,
                },
                store=False,
            )

        response = self._run_with_retry(request)
        raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else None
        try:
            payload = bind_extraction_payload(response.output_text, prompt)
        except StructuredOutputError as exc:
            raise SemanticResponseError(exc.code, response) from None
        return self._response(
            prompt=prompt,
            payload=payload,
            response_id=getattr(response, "id", None),
            raw_response=raw,
        )


class DeepSeekResponsesGateway(_RetryingGateway):
    """DeepSeek Responses-compatible fallback; key is read only from DEEPSEEK_API_KEY."""

    def __init__(
        self,
        *,
        config: ProviderConfig,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.provider != "deepseek" or config.api_name != "responses" or not config.base_url:
            raise ValueError("DeepSeekResponsesGateway requires the DeepSeek Responses configuration")
        if config.reasoning_effort not in {"none", "low", "high", "max", "minimal", "medium", "xhigh"}:
            raise ValueError("unsupported DeepSeek Responses reasoning effort")
        if config.max_output_tokens is not None and (
            type(config.max_output_tokens) is not int or not 1 <= config.max_output_tokens <= 32768
        ):
            raise ValueError("invalid DeepSeek output token limit")
        if client is None:
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY is required for the DeepSeek provider")
            import httpx
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=config.base_url,
                max_retries=0,
                timeout=config.timeout_seconds,
                http_client=httpx.Client(trust_env=False),
            )
        super().__init__(config=config, sleep=sleep)
        self.client = client

    def extract(self, review: ReviewInput, prompt: PromptPackage) -> GatewayResponse:
        def request() -> Any:
            limits = {} if self.config.max_output_tokens is None else {"max_output_tokens": self.config.max_output_tokens}
            from .structured_response import sampling_options
            limits.update(sampling_options())
            return self.client.responses.create(
                model=self.config.model,
                input=[
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                reasoning={"effort": self.config.reasoning_effort},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "review_semantic_extraction",
                        "schema": prompt.response_schema,
                        "strict": self.config.strict,
                    }
                },
                store=False,
                **limits,
            )

        response = self._run_with_retry(request)
        # Response checks stay outside the transport retry loop. Even syntactically
        # valid text from an incomplete response must not be promoted to success.
        status = getattr(response, "status", None)
        details = getattr(response, "incomplete_details", None)
        reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
        if status == "incomplete":
            raise SemanticResponseError("output_truncated" if reason == "max_output_tokens" else "response_incomplete", response)
        if status is not None and status != "completed":
            raise SemanticResponseError("response_not_completed", response)
        if not isinstance(response.output_text, str) or not response.output_text.strip():
            raise SemanticResponseError("empty_output_text", response)
        raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else None
        try:
            payload = bind_extraction_payload(response.output_text, prompt)
        except StructuredOutputError as exc:
            raise SemanticResponseError(exc.code, response) from None
        return self._response(
            prompt=prompt,
            payload=payload,
            response_id=getattr(response, "id", None),
            raw_response=raw,
        )


class LMStudioChatGateway(_RetryingGateway):
    """Local OpenAI-compatible adapter used only for the first Dev baseline."""

    def __init__(
        self,
        *,
        config: ProviderConfig,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.provider != "lmstudio" or config.api_name != "chat_completions_compatible":
            raise ValueError("LMStudioChatGateway requires the local compatible configuration")
        if not config.base_url:
            raise ValueError("LM Studio base_url is required")
        if client is None:
            import httpx
            from openai import OpenAI

            client = OpenAI(
                base_url=config.base_url,
                api_key=os.environ.get("LM_STUDIO_API_KEY", "local-no-auth"),
                max_retries=0,
                timeout=config.timeout_seconds,
                http_client=httpx.Client(trust_env=False),
            )
        super().__init__(config=config, sleep=sleep)
        self.client = client

    def extract(self, review: ReviewInput, prompt: PromptPackage) -> GatewayResponse:
        def request() -> Any:
            return self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                reasoning_effort=self.config.reasoning_effort,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "review_semantic_extraction",
                        "strict": self.config.strict,
                        "schema": prompt.response_schema,
                    },
                },
                temperature=0,
            )

        response = self._run_with_retry(request)
        raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else None
        message = response.choices[0].message
        reasoning_content = (
            message.model_extra.get("reasoning_content", "") if getattr(message, "model_extra", None) else ""
        )
        return self._response(
            prompt=prompt,
            payload=message.content or reasoning_content,
            response_id=getattr(response, "id", None),
            raw_response=raw,
        )
