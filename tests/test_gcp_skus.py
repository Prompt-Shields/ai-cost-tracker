import json
from importlib import resources

import pytest

from ai_cost_tracker.normalise import canonical_model as cloud_canonical_model
from ai_cost_tracker.normalise import parse_meter
from ai_cost_tracker.normalise.azure_meters import MeterInfo
from ai_cost_tracker.normalise.gcp_skus import (
    _compile,
    _parse,
    canonical_model,
    is_token_priced,
    model_version,
    parse_sku,
)


def M(model, direction, dtype="global", modality=None, version=None) -> MeterInfo:
    return MeterInfo(
        model=model,
        version=version,
        direction=direction,
        deployment_type=dtype,
        modality=modality,
    )


# The six SKUs the fake GCP connector bills: they must map onto the deployments' models.
DEMO = {
    "Gemini 2.5 Flash Text Input - Predictions": ("gemini-2.5-flash", "input"),
    "Gemini 2.5 Flash Text Output - Predictions": ("gemini-2.5-flash", "output"),
    "Gemini 2.5 Pro Text Input - Predictions": ("gemini-2.5-pro", "input"),
    "Gemini 2.5 Pro Text Output - Predictions": ("gemini-2.5-pro", "output"),
    "Gemini 2.0 Flash Text Input - Predictions": ("gemini-2.0-flash", "input"),
    "Gemini 2.0 Flash Text Output - Predictions": ("gemini-2.0-flash", "output"),
}


@pytest.mark.parametrize(("sku", "expected"), DEMO.items())
def test_demo_skus(sku: str, expected: tuple[str, str]) -> None:
    info = parse_meter("gcp", sku)
    assert info is not None
    assert (info.model, info.direction, info.deployment_type) == (*expected, "global")
    assert info.modality == "text"


def test_demo_models_match_deployment_names() -> None:
    for name in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash-001"):
        assert cloud_canonical_model("gcp", name) in {m for m, _ in DEMO.values()}
    assert canonical_model("gemini-2.0-flash-001") == canonical_model("Gemini 2.0 Flash")
    assert canonical_model("Gemini 2.0 Flash") == "gemini-2.0-flash"


@pytest.mark.parametrize(
    ("sku", "expected"),
    [
        (
            "Gemini 2.0 Flash Lite Text Input - Predictions",
            M("gemini-2.0-flash-lite", "input", modality="text"),
        ),
        (
            "Gemini 2.0 Flash-Lite Text Output - Batch Predictions",
            M("gemini-2.0-flash-lite", "output", "batch", "text"),
        ),
        (
            "Gemini 2.5 Flash-Lite Text Input - Predictions",
            M("gemini-2.5-flash-lite", "input", modality="text"),
        ),
        (
            "Gemini 1.5 Pro Image Input - Predictions",
            M("gemini-1.5-pro", "input", modality="image"),
        ),
        (
            "Gemini 2.5 Pro Video Input - Predictions",
            M("gemini-2.5-pro", "input", modality="video"),
        ),
        (
            "Gemini 1.5 Flash 002 Audio Input - Predictions",
            M("gemini-1.5-flash", "input", modality="audio", version="002"),
        ),
        # Long-context tiers never become the base input/output rate.
        (
            "Gemini 1.5 Pro Long Text Input - Predictions",
            M("gemini-1.5-pro", "input_long", modality="text"),
        ),
        (
            "Gemini 2.5 Pro Text Input > 200K tokens - Predictions",
            M("gemini-2.5-pro", "input_long", modality="text"),
        ),
        (
            "Gemini 2.5 Pro Text Output > 200K tokens - Predictions",
            M("gemini-2.5-pro", "output_long", modality="text"),
        ),
        (
            "Gemini 2.5 Pro Text Input <= 200K tokens - Predictions",
            M("gemini-2.5-pro", "input", modality="text"),
        ),
        (
            "Gemini 2.5 Flash Cached Text Input - Predictions",
            M("gemini-2.5-flash", "cached_input", modality="text"),
        ),
        (
            "Gemini 2.5 Pro Long Context Cached Input",
            M("gemini-2.5-pro", "cached_input_long"),
        ),
        ("Gemini 2.5 Pro Context Cache Storage", M("gemini-2.5-pro", "cache_storage")),
        (
            "Gemini 2.5 Flash Thinking Output - Predictions",
            M("gemini-2.5-flash", "output_thinking"),
        ),
        ("Gemini 2.5 Pro Priority Text Input", M("gemini-2.5-pro", "input", "priority", "text")),
        ("Gemini 1.0 Pro Text Input Characters", M("gemini-1.0-pro", "input", modality="text")),
        (
            "Vertex AI Gemini 2.5 Flash GA Text Input",
            M("gemini-2.5-flash", "input", modality="text"),
        ),
        (
            "Gemini 2.5 Pro Preview Text Input",
            M("gemini-2.5-pro-preview", "input", modality="text"),
        ),
        (
            "Text Embedding 004 Online Predictions",
            M("text-embedding-004", "input", modality="text"),
        ),
        ("Gemini Embedding Input Tokens", M("gemini-embedding", "input", modality="text")),
        ("Imagen 3 Generation", M("imagen-3", "output", modality="image")),
        ("Imagen 3 Fast Image Generation", M("imagen-3-fast", "output", modality="image")),
        ("Veo 3 Fast Video Generation", M("veo-3-fast", "output", modality="video")),
        ("Claude 3.5 Sonnet v2 Input Tokens", M("claude-3.5-sonnet-v2", "input")),
        ("Claude 3.7 Sonnet Cache Hit Input Tokens", M("claude-3.7-sonnet", "cached_input")),
        ("Claude Sonnet 4 Cache Write Input Tokens", M("claude-sonnet-4", "cache_write")),
        ("Claude Opus 4.1 Output Tokens", M("claude-opus-4.1", "output")),
        ("Llama 3.1 405B Input Tokens", M("llama-3.1-405b", "input")),
        ("Mistral Large (24.11) Output Tokens", M("mistral-large-2411", "output")),
        ("Prediction Online Node n1-standard-4 running in Americas", None),
        ("Provisioned Throughput GSU", None),
        ("Some Unrelated SKU", None),
        ("", None),
    ],
)
def test_parse_sku(sku: str, expected: MeterInfo | None) -> None:
    assert parse_sku(sku) == expected


@pytest.mark.parametrize(
    ("name", "model", "version"),
    [
        ("gemini-2.0-flash-001", "gemini-2.0-flash", "001"),
        ("gemini-2.5-flash", "gemini-2.5-flash", None),
        ("publishers/google/models/gemini-2.5-pro", "gemini-2.5-pro", None),
        ("gemini-2.0-flash-lite-001", "gemini-2.0-flash-lite", "001"),
        ("gemini-2.5-pro-preview-05-06", "gemini-2.5-pro-preview", "05-06"),
        ("claude-3-5-sonnet-v2@20241022", "claude-3.5-sonnet-v2", "20241022"),
        ("claude-sonnet-4-5@20250929", "claude-sonnet-4.5", "20250929"),
        ("claude-opus-4-1", "claude-opus-4.1", None),
        ("claude-3-haiku@20240307", "claude-3-haiku", "20240307"),
        ("text-embedding-004", "text-embedding-004", None),
        ("gemini-embedding-001", "gemini-embedding", "001"),
        ("imagen-3.0-generate-002", "imagen-3", "002"),
        ("imagen-3.0-fast-generate-001", "imagen-3-fast", "001"),
        ("veo-3.0-fast-generate-001", "veo-3-fast", "001"),
        ("llama-3.1-405b-instruct-maas", "llama-3.1-405b", None),
        ("mistral-large-2411", "mistral-large-2411", None),
    ],
)
def test_canonical_model_and_version(name: str, model: str, version: str | None) -> None:
    assert canonical_model(name) == model
    assert model_version(name) == version


def test_sku_and_model_id_meet_for_partner_models() -> None:
    pairs = [
        ("Claude 3.5 Sonnet v2 Input Tokens", "claude-3-5-sonnet-v2@20241022"),
        ("Imagen 3 Fast Image Generation", "imagen-3.0-fast-generate-001"),
        ("Gemini Embedding Input Tokens", "gemini-embedding-001"),
        ("Llama 3.1 405B Input Tokens", "llama-3.1-405b-instruct-maas"),
    ]
    for sku, model_id in pairs:
        assert parse_sku(sku).model == canonical_model(model_id)


def test_is_token_priced() -> None:
    assert is_token_priced("Gemini 2.5 Flash Text Input - Predictions")
    assert not is_token_priced("Imagen 3 Generation")
    assert not is_token_priced("Veo 3 Video Generation")
    assert not is_token_priced("Unknown thing")


def test_rules_file_is_data_driven() -> None:
    text = resources.files("ai_cost_tracker.normalise").joinpath("gcp_sku_rules.json")
    data = json.loads(text.read_text("utf-8"))
    assert "Assumption (spike-gcp)" in data["_unverified"]
    data["model_families"].append("^grok-")
    rules = _compile(data)
    parsed = _parse("Grok 4 Text Input", rules)
    assert parsed is not None and parsed.info.model == "grok-4"
    assert parse_sku("Grok 4 Text Input") is None
