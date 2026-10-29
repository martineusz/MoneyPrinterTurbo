"""Image generation backends (OpenAI-compatible via MPT's helpers, or ComfyUI) + hash cache."""

import copy
import hashlib
import json
import os
import time
import uuid

from loguru import logger


class ImageBackendError(RuntimeError):
    pass


def cache_key(prompt: str, negative: str, seed: int, width: int, height: int, backend: str,
              extra: str = "") -> str:
    payload = json.dumps([prompt, negative, seed, width, height, backend, extra],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class OpenAIBackend:
    """Uses the openai_image_* settings and request helpers already in material.py."""

    name = "openai"

    def __init__(self, aspect="9:16"):
        from app.services import material

        self._material = material
        self.aspect = aspect

    def fingerprint(self) -> str:
        m = self._material
        _, model = m._openai_image_endpoint()
        return f"{model}|{m._openai_image_size(self.aspect)}"

    def generate(self, prompt: str, negative: str, seed: int, width: int, height: int) -> bytes:
        from app.models.schema import VideoAspect
        from app.services import generation_progress

        m = self._material
        endpoint, model = m._openai_image_endpoint()
        text = prompt + (f"\n\nAvoid: {negative}" if negative else "")
        payload = {
            "model": model, "prompt": text, "n": 1,
            "size": m._openai_image_size(VideoAspect(self.aspect)),
        }
        generation_progress.submitting("openai_image")
        data, detail = m._request_openai_image(endpoint, payload)
        if data is None:
            raise ImageBackendError(f"image generation failed: {detail}")
        return data


def fill_workflow(workflow: dict, prompt: str, negative: str, seed: int, width: int,
                  height: int) -> dict:
    """Replace {prompt} {negative} {seed} {width} {height} placeholders in a workflow dict."""
    ints = {"{seed}": seed, "{width}": width, "{height}": height}
    texts = {"{prompt}": prompt, "{negative}": negative}

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            if node in ints:
                return ints[node]  # whole-value placeholder keeps the JSON number type
            for key, value in texts.items():
                node = node.replace(key, value)
        return node

    return walk(copy.deepcopy(workflow))


class ComfyBackend:
    """POST a workflow to ComfyUI, poll /history, download the first output image."""

    name = "comfyui"

    def __init__(self, url: str, workflow_path: str, timeout: float = 300, http=None,
                 poll_interval: float = 1.0):
        if not workflow_path:
            raise ImageBackendError(
                "image_backend = 'comfyui' needs 'comfyui_workflow' in the preset's style.json")
        with open(workflow_path, encoding="utf-8-sig") as handle:
            self.workflow = json.load(handle)
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        if http is None:
            import requests as http  # noqa: N813
        self.http = http

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.workflow, sort_keys=True).encode()).hexdigest()[:16]

    def generate(self, prompt: str, negative: str, seed: int, width: int, height: int) -> bytes:
        workflow = fill_workflow(self.workflow, prompt, negative, seed, width, height)
        response = self.http.post(f"{self.url}/prompt", json={
            "prompt": workflow, "client_id": uuid.uuid4().hex}, timeout=30)
        if response.status_code != 200:
            raise ImageBackendError(f"ComfyUI rejected the workflow: {response.text[:300]}")
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise ImageBackendError("ComfyUI did not return a prompt_id")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            history = self.http.get(f"{self.url}/history/{prompt_id}", timeout=30).json()
            entry = history.get(prompt_id)
            if entry and entry.get("outputs"):
                for output in entry["outputs"].values():
                    for image in output.get("images", []):
                        got = self.http.get(f"{self.url}/view", params={
                            "filename": image["filename"], "subfolder": image.get("subfolder", ""),
                            "type": image.get("type", "output")}, timeout=60)
                        if got.status_code != 200:
                            raise ImageBackendError(f"ComfyUI /view failed: {got.status_code}")
                        return got.content
                raise ImageBackendError("ComfyUI finished without an output image")
            if entry and entry.get("status", {}).get("status_str") == "error":
                raise ImageBackendError("ComfyUI reported an execution error")
            time.sleep(self.poll_interval)
        raise ImageBackendError(f"ComfyUI timed out after {self.timeout:.0f}s")


def create_backend(cfg: dict, style: dict | None, aspect: str):
    if cfg["image_backend"] == "comfyui":
        return ComfyBackend(cfg["comfyui_url"], (style or {}).get("comfyui_workflow", ""),
                            cfg["comfyui_timeout"])
    return OpenAIBackend(aspect)


def cache_dir() -> str:
    from app.utils import utils

    return utils.storage_dir("studio_cache", create=True)


def image_path_for(key: str, directory: str | None = None) -> str:
    return os.path.join(directory or cache_dir(), f"{key}.png")


def fetch_image(backend, prompt: str, negative: str, seed: int, width: int, height: int,
                force: bool = False, directory: str | None = None) -> tuple[str, str, bool]:
    """Return (path, key, generated). Reuses the cache unless ``force`` is set."""
    key = cache_key(prompt, negative, seed, width, height, backend.name, backend.fingerprint())
    path = image_path_for(key, directory)
    if os.path.isfile(path) and not force:
        return path, key, False
    logger.info(f"studio: generating image ({backend.name}) seed={seed}")
    data = backend.generate(prompt, negative, seed, width, height)
    directory = directory or cache_dir()
    os.makedirs(directory, exist_ok=True)
    from app.services import material

    try:
        saved, _, _ = material._save_openai_image_file(data, directory)  # validates + PNG-normalises
    except material._OpenAIImageDecodeError as exc:
        raise ImageBackendError(f"backend returned an unreadable image: {exc}") from exc
    os.replace(saved, path)
    return path, key, True
