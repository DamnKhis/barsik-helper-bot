from __future__ import annotations

import base64
from pathlib import Path

from .base import LLMProvider, VisionProvider
from .http import extract_json, post_json


class QwenChat(LLMProvider, VisionProvider):
    """Qwen3.8-Flash via the OpenAI-compatible DashScope endpoint.

    One model handles text/JSON enrichment and image analysis. The same API
    key is used for embeddings (a separate embedding model, because chat
    models do not expose an embeddings endpoint).
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
        response = post_json(
            f"{self.base_url}/chat/completions",
            self._payload(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            ),
            {"Authorization": f"Bearer {self.api_key}"},
        )
        content = response["choices"][0]["message"]["content"]
        return extract_json(content)

    def describe_image(self, image_path: Path, prompt: str) -> dict:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = post_json(
            f"{self.base_url}/chat/completions",
            self._payload(
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
            ),
            {"Authorization": f"Bearer {self.api_key}"},
        )
        content = response["choices"][0]["message"]["content"]
        return extract_json(content)

    def _payload(self, messages: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        if not self.enable_thinking:
            # Qwen3.8 defaults to xhigh reasoning; extraction does not need it.
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
