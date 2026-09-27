"""Thin Ollama client that only ever exposes cloud models."""
from __future__ import annotations

import httpx

CLOUD_HOST = "https://ollama.com"


class OllamaError(RuntimeError):
    pass


def is_cloud_entry(entry: dict) -> bool:
    """True for a local-daemon /api/tags entry that is backed by Ollama Cloud."""
    name = entry.get("name") or entry.get("model") or ""
    return bool(entry.get("remote_host")) or name.endswith("-cloud") or name.endswith(":cloud")


def _base_and_headers(mode: str, host: str, api_key: str) -> tuple[str, dict]:
    if mode == "direct":
        if not api_key:
            raise OllamaError("Direct mode needs an Ollama API key (https://ollama.com/settings/keys).")
        return CLOUD_HOST, {"Authorization": f"Bearer {api_key}"}
    return host.rstrip("/"), {}


def _filter_models(mode: str, payload: dict) -> list[str]:
    models = payload.get("models", [])
    if mode == "direct":
        return sorted(m.get("name") or m.get("model") for m in models)
    return sorted(m.get("name") or m.get("model") for m in models if is_cloud_entry(m))


def list_cloud_models_sync(mode: str, host: str, api_key: str) -> list[str]:
    base, headers = _base_and_headers(mode, host, api_key)
    try:
        r = httpx.get(f"{base}/api/tags", headers=headers, timeout=15)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise OllamaError(f"Could not list models from {base}: {e}") from e
    return _filter_models(mode, r.json())


class OllamaClient:
    def __init__(self, mode: str, host: str, api_key: str):
        self.mode = mode
        base, headers = _base_and_headers(mode, host, api_key)
        self._http = httpx.AsyncClient(
            base_url=base, headers=headers, timeout=httpx.Timeout(300, connect=15)
        )

    async def list_cloud_models(self) -> list[str]:
        r = await self._http.get("/api/tags")
        r.raise_for_status()
        return _filter_models(self.mode, r.json())

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """One non-streaming chat round. Returns the assistant message dict."""
        body = {"model": model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        try:
            r = await self._http.post("/api/chat", json=body)
        except httpx.HTTPError as e:
            raise OllamaError(f"{model}: request failed: {e}") from e
        if r.status_code >= 400:
            raise OllamaError(f"{model}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json().get("message", {})

    async def aclose(self) -> None:
        await self._http.aclose()
