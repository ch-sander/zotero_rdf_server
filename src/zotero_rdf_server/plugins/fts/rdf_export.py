import gzip
import json
from pathlib import Path
from typing import Any, BinaryIO, Mapping
from urllib.parse import quote

from pyoxigraph import Literal, NamedNode, RdfFormat, Store

from zotero_rdf_server.rdf import load_rdf_from_spec
from zotero_rdf_server.utils import safeLiteral, safeNamedNode


RDF_JSON = NamedNode(
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON"
)


def rdf_json_literal(value: Any) -> str:
    """Return a complete rdf:JSON literal token."""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        default=str,
    )
    return str(
        Literal(
            serialized,
            datatype=RDF_JSON,
        )
    )


def make_item_iri(
    item: Mapping[str, Any],
    *,
    base_iri: str = "urn:ingest:item:",
) -> str:
    """Use _iri when present, otherwise derive a stable IRI from _id."""
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
            "RDF export requires either '_iri' or '_id'"
        )

    return f"{base_iri}{quote(str(doc_id), safe='')}"


def make_item_rdf_data(
    item: Mapping[str, Any],
    *,
    item_iri: str,
) -> dict[str, Any]:
    """Build raw and RDF-safe bindings for the item template."""
    data = dict(item)

    data.update(
        {
            "item": dict(item),
            "item_iri": item_iri,
            "rdf_item": safeNamedNode(item_iri),
            "rdf_id": safeLiteral(item.get("_id")),
            "rdf_label": safeLiteral(
                item.get("_label", "")
            ),
            "rdf_input": safeLiteral(
                item.get("_input")
                or item.get("_url")
                or ""
            ),
            "rdf_item_json": rdf_json_literal(
                dict(item)
            ),
        }
    )

    return data


def make_page_rdf_data(
    item_data: Mapping[str, Any],
    *,
    page_no: int,
    text: str,
) -> dict[str, Any]:
    """Add raw and RDF-safe page/OCR bindings."""
    item_iri = str(item_data["item_iri"])
    item_id = item_data.get("_id")
    page_iri = (
        f"{item_iri}/page/"
        f"{quote(str(page_no), safe='')}"
    )
    page_id = f"{item_id}:{page_no}" if item_id else f"page:{page_no}"
    data = dict(item_data)

    data.update(
        {
            "page": int(page_no),
            "text": text,
            "page_iri": page_iri,
            "page_id": page_id,
            "rdf_page": safeNamedNode(page_iri),
            "rdf_page_id": safeLiteral(page_id),
            "rdf_page_no": safeLiteral(
                int(page_no)
            ),
            "rdf_text": safeLiteral(text),
        }
    )

    return data


class RdfNqGzipSink:
    """Append rendered RDF stores to one N-Quads gzip stream."""

    def __init__(
        self,
        path: str | Path,
        *,
        spec: Mapping[str, Any],
        compresslevel: int = 6,
    ) -> None:
        self.path = Path(path)
        self.spec = spec
        self.compresslevel = compresslevel
        self._output: BinaryIO | None = None

    def __enter__(self) -> "RdfNqGzipSink":
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._output = gzip.open(
            self.path,
            mode="wb",
            compresslevel=self.compresslevel,
        )

        return self

    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def emit(
        self,
        *,
        context: str,
        data: Mapping[str, Any],
        store: Store,
        node_value: Any = None,
        default_graph_uri: Any = None,
    ) -> bool:
        """Render one RDF context into an existing store."""
        self._ensure_open()

        return load_rdf_from_spec(
            self.spec,
            context=context,
            data=data,
            node_value=(
                data
                if node_value is None
                else node_value
            ),
            store=store,
            default_graph_uri=default_graph_uri,
        )

    def dump(self, store: Store) -> None:
        """Append the complete store to the open N-Quads stream."""
        output = self._ensure_open()

        store.dump(
            output=output,
            format=RdfFormat.N_QUADS,
        )

    def _ensure_open(self) -> BinaryIO:
        if self._output is None:
            raise RuntimeError(
                "RdfNqGzipSink must be used "
                "as a context manager"
            )

        return self._output