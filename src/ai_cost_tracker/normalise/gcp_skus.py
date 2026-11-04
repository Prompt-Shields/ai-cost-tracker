"""Parse Vertex AI SKU descriptions into model, direction and deployment type.

Minimal first cut written with the M3 foundation; W5 replaces it with a rules file. The
spellings are assumptions until ``act spike-gcp`` lists real SKUs (Assumption (spike-gcp)).
"""

import re

from ai_cost_tracker.normalise.azure_meters import MeterInfo

_VERSION = re.compile(r"-(\d{3})$")
_MODEL = re.compile(r"^(gemini|text-embedding|textembedding|imagen|veo|claude|llama)")


def canonical_model(name: str) -> str:
    """``gemini-2.5-flash-001``, ``Gemini 2.5 Flash`` and ``publishers/google/models/
    gemini-2.5-flash`` all become ``gemini-2.5-flash``."""
    text = name.strip().lower().rsplit("/", 1)[-1]
    text = re.sub(r"[\s_]+", "-", text)
    return _VERSION.sub("", text)


def model_version(name: str) -> str | None:
    """The ``001`` in ``gemini-2.0-flash-001``; None when the name carries no version."""
    m = _VERSION.search(name.strip().lower().rsplit("/", 1)[-1])
    return m.group(1) if m else None


def parse_sku(description: str) -> MeterInfo | None:
    words = description.strip().lower().replace(" - ", " ").split()
    stop = {
        "text",
        "input",
        "output",
        "image",
        "audio",
        "video",
        "predictions",
        "batch",
        "tokens",
        "characters",
        "cached",
        "online",
        "long",
        "short",
    }
    model_words: list[str] = []
    for w in words:
        if w in stop:
            break
        model_words.append(w)
    model = canonical_model(" ".join(model_words))
    if not model or not _MODEL.match(model):
        return None
    tokens = set(words)
    if "cached" in tokens:
        direction = "cached_input"
    elif "input" in tokens:
        direction = "input"
    elif "output" in tokens:
        direction = "output"
    else:
        direction = "other"
    modality = next((m for m in ("text", "image", "audio", "video") if m in tokens), None)
    return MeterInfo(
        model=model,
        version=None,
        direction=direction,
        deployment_type="batch" if "batch" in tokens else "global",
        modality=modality,
    )
