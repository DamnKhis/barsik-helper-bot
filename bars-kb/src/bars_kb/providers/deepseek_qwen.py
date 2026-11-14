from __future__ import annotations

from pathlib import Path

from .base import LLMProvider, VisionProvider
from .http import extract_json, post_json


class DeepSeekLLM(LLMProvider):
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def complete_json(self, system: str, user: str) -> dict:
        response = post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            {"Authorization": f"Bearer {self.api_key}"},
        )
        content = response["choices"][0]["message"]["content"]
        return extract_json(content)


class QwenVision(VisionProvider):
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def describe_image(self, image_path: Path, prompt: str) -> dict:
        import base64

        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [
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
                ],
                "temperature": 0.1,
            },
            {"Authorization": f"Bearer {self.api_key}"},
        )
        content = response["choices"][0]["message"]["content"]
        return extract_json(content)


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
