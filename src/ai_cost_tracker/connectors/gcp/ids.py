"""Identifiers shared by the GCP modules, so billing rows, token metrics and inventory join.

A GCP "resource" is Vertex AI in one project and location, the counterpart of an Azure
OpenAI account; a "deployment" is a model served there (a publisher model such as
``gemini-2.5-flash``, or a self-deployed endpoint, ``endpoints/<id>``).
"""

PREFIX = "//aiplatform.googleapis.com/projects/"
GLOBAL = "global"


def resource_id(project_id: str, location: str | None) -> str:
    """``//aiplatform.googleapis.com/projects/<project>/locations/<location>``.

    A missing location (billing rows without one) is ``global``. Lowercased so billing,
    metrics and inventory agree whatever casing each API returns.
    """
    loc = (location or "").strip().lower() or GLOBAL
    return f"{PREFIX}{project_id.strip().lower()}/locations/{loc}"


def parse_resource_id(value: str) -> tuple[str, str]:
    """(project, location) from a ``resource_id``; ValueError for anything else."""
    if not value.startswith(PREFIX):
        raise ValueError(f"not a GCP resource id: {value!r}")
    parts = value[len(PREFIX) :].split("/")
    if len(parts) != 3 or parts[1] != "locations" or not parts[0] or not parts[2]:
        raise ValueError(f"not a GCP resource id: {value!r}")
    return parts[0], parts[2]
