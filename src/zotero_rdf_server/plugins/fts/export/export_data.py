from typing import Any, Mapping
from urllib.parse import quote


def make_item_iri(
    item: Mapping[str, Any],
    *,
    base_iri: str = "urn:ingest:item:",
) -> str:
    """Return an explicit item IRI or derive a stable IRI from _id."""
    explicit = (
        item.get("item_iri")
        or item.get("_iri")
        or item.get("file")
    )
    if explicit:
        return str(explicit)

    doc_id = item.get("_id")
    if doc_id in (None, ""):
        raise ValueError(
            "Export requires either 'item_iri', '_iri', 'file' or '_id'"
        )

    return f"{base_iri}{quote(str(doc_id), safe='')}"


def make_page_iri(
    item_iri: str,
    page_no: int,
) -> str:
    """Return the canonical page IRI for one item page."""
    return f"{item_iri}/page/{quote(str(page_no), safe='')}"


def make_item_data(
    item: Mapping[str, Any],
    *,
    item_iri: str | None = None,
    base_iri: str = "urn:ingest:item:",
) -> dict[str, Any]:
    """Build the shared, format-neutral item export dictionary."""
    item_iri = item_iri or make_item_iri(
        item,
        base_iri=base_iri,
    )

    data = dict(item)
    data.update(
        {
            "item": dict(item),
            "item_iri": item_iri,
            "id": item.get("_id"),
            "label": item.get("_label", ""),
            "input": item.get("_input") or item.get("_url") or "",
        }
    )
    return data


def make_page_data(
    item_data: Mapping[str, Any],
    *,
    page_no: int,
    text: str,
) -> dict[str, Any]:
    """Build the shared, format-neutral page export dictionary."""
    item_iri = str(item_data["item_iri"])
    page_no = int(page_no)
    item_id = item_data.get("_id") or item_data.get("id")
    page_iri = make_page_iri(
        item_iri,
        page_no,
    )

    data = dict(item_data)
    data.update(
        {
            "page": page_no,
            "page_no": page_no,
            "text": text,
            "page_iri": page_iri,
            "page_id": (
                f"{item_id}:{page_no}"
                if item_id
                else f"page:{page_no}"
            ),
        }
    )
    return data
