"""Turn narration segments into drawable image prompts with MPT's configured LLM."""

import json
import re

from loguru import logger


class ScenePromptError(RuntimeError):
    pass


def build_prompt(script: str, narrations: list[str], entity_names: list[str],
                 problem: str = "", guidance: str = "") -> str:
    lines = "\n".join(f"{i}. {text}" for i, text in enumerate(narrations, 1))
    names = (
        "\nFixed names you must reuse verbatim whenever a scene involves them: "
        + ", ".join(entity_names) + "."
    ) if entity_names else ""
    extra = (
        f"\nAdditional guidance from the author of this series:\n{guidance.strip()}"
    ) if guidance.strip() else ""
    retry = (
        f"\nYour previous answer was rejected ({problem}). Answer again."
    ) if problem else ""
    return (
        "You are a storyboard artist for a short vertical video.\n"
        "Full narration script (for context only):\n"
        f"\"\"\"\n{script.strip()}\n\"\"\"\n\n"
        f"The narration is cut into {len(narrations)} scenes:\n{lines}\n\n"
        "For EVERY scene write exactly ONE concrete, drawable shot description in English: "
        "subject, setting, composition and lighting, at most 40 words. Describe only what can be "
        "seen. No abstract concepts, no on-screen text, captions, logos or letters, and do not "
        "mention the narration or the scene number."
        f"{names}{extra}{retry}\n\n"
        f"Return ONLY JSON in this shape, with exactly {len(narrations)} items in scene order:\n"
        "{\"scenes\": [{\"scene\": 1, \"prompt\": \"...\"}]}"
    )


def parse_response(text: str, expected: int) -> list[str]:
    """Validate the LLM answer and return one prompt per scene, in order."""
    from app.services import llm

    cleaned = llm._strip_code_fence(text or "")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            data = llm._extract_embedded_json(cleaned, "{")
        except ValueError:
            try:
                data = llm._extract_embedded_json(cleaned, "[")
            except ValueError:
                raise ScenePromptError("response is not valid JSON") from None
    items = data.get("scenes") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ScenePromptError("JSON must contain a 'scenes' list")
    if len(items) != expected:
        raise ScenePromptError(f"expected {expected} scenes, got {len(items)}")
    prompts = []
    for position, item in enumerate(items, 1):
        if isinstance(item, dict):
            number = item.get("scene", position)
            if number != position:
                raise ScenePromptError(f"scene numbers out of order at item {position}")
            item = item.get("prompt")
        if not isinstance(item, str) or not item.strip():
            raise ScenePromptError(f"scene {position} has no prompt text")
        prompts.append(" ".join(item.split()))
    return prompts


def substitute_entities(prompt: str, entities: dict[str, str]) -> str:
    """Replace every mention of an entity name with its fixed visual description."""
    if not entities:
        return prompt
    names = sorted(entities, key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.IGNORECASE)
    lookup = {n.lower(): d for n, d in entities.items()}
    return pattern.sub(lambda m: lookup[m.group(1).lower()], prompt)


def generate_prompts(script: str, narrations: list[str], entities: dict[str, str],
                     ask=None, guidance: str = "") -> list[str]:
    """One LLM call for the whole script; retry once on malformed output."""
    if ask is None:
        from app.services import llm

        def ask(prompt):
            return llm._generate_response(prompt)

    problem = ""
    for attempt in (1, 2):
        answer = ask(build_prompt(script, narrations, list(entities), problem, guidance))
        if isinstance(answer, str) and answer.startswith("Error: "):
            raise ScenePromptError(answer.removeprefix("Error: ").strip())
        try:
            prompts = parse_response(answer, len(narrations))
            return [substitute_entities(p, entities) for p in prompts]
        except ScenePromptError as exc:
            problem = str(exc)
            logger.warning(f"studio: scene prompt attempt {attempt} rejected: {problem}")
    raise ScenePromptError(f"LLM returned invalid scene prompts twice: {problem}")
