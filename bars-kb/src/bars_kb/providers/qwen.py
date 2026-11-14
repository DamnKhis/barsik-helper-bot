from __future__ import annotations

import base64
from pathlib import Path

from .base import LLMProvider, VisionProvider
from .http import extract_json, post_json


class QwenChat(LLMProvider, VisionProvider):
    """Qwen3.8-Flash via an OpenAI-compatible endpoint.

    Works with OpenRouter and DashScope. One model handles text/JSON
    enrichment and image analysis. The same API key is used for embeddings
    (a separate embedding model, because chat models do not expose an
    embeddings endpoint).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        enable_thinking: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enable_thinking = enable_thinking

    def complete_json(self, system: str, user: str) -> dict:
        return self._request_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def describe_image(self, image_path: Path, prompt: str) -> dict:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return self._request_json(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ]
        )

    def _request_json(self, messages: list[dict]) -> dict:
        last_error: Exception | None = None
        for attempt in range(2):
            response = post_json(
                f"{self.base_url}/chat/completions",
                self._payload(messages, strict=attempt > 0),
                self._headers(),
            )
            content = response["choices"][0]["message"]["content"]
            try:
                return extract_json(content)
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    messages = messages + [
                        {
                            "role": "user",
                            "content": (
                                "Предыдущий ответ не был валидным JSON. "
                                "Верни только валидный JSON-объект без markdown, "
                                "комментариев и пояснений."
                            ),
                        }
                    ]
        raise last_error or RuntimeError("Model did not return a JSON object")

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://github.com/DamnKhis/barsik-helper-bot"
            headers["X-Title"] = "BARS KB"
        return headers

    def _payload(self, messages: list[dict], strict: bool = False) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0 if strict else 0.1,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        if not self.enable_thinking:
            # Qwen3.8 defaults to reasoning; extraction does not need it.
            # OpenRouter uses the OpenAI-style "reasoning" field.
            if "openrouter.ai" in self.base_url:
                payload["reasoning"] = {"enabled": False}
            else:
                payload["enable_thinking"] = False
        return payload


class QwenEmbeddings:
    def __init__(self, api_key: str, base_url: str, model: str, dim: int) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for start in range(0, len(texts), 10):
            batch = texts[start : start + 10]
            response = post_json(
                f"{self.base_url}/embeddings",
                {
                    "model": self.model,
                    "input": batch,
                    "dimensions": self.dim,
                    "encoding_format": "float",
                },
                {"Authorization": f"Bearer {self.api_key}"},
            )
            data = sorted(response["data"], key=lambda item: item.get("index", 0))
            result.extend([item["embedding"] for item in data])
        return result

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
