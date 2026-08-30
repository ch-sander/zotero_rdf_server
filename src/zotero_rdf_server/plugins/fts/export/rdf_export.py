import gzip
import json
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from pyoxigraph import Literal, NamedNode, RdfFormat, Store

from zotero_rdf_server.plugins.fts.export.export_data import (
    make_item_data,
    make_item_iri,
    make_page_data,
    make_page_iri,
)
from zotero_rdf_server.rdf import load_rdf_from_spec, XSD_NS
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


def make_item_rdf_data(
    item: Mapping[str, Any],
    *,
    item_iri: str | None = None,
    base_iri: str = "urn:ingest:item:",
) -> dict[str, Any]:
    """Build RDF-safe bindings from shared item data.

    The signature remains compatible with the previous implementation.
    """
    data = (
        dict(item)
        if item_iri is None and "item_iri" in item
        else make_item_data(
            item,
            item_iri=item_iri,
            base_iri=base_iri,
        )
    )

    data.update(
        {
            "rdf_item": safeNamedNode(data["item_iri"]),
            "rdf_id": safeLiteral(data.get("_id")),
            "rdf_label": safeLiteral(data.get("_label", "")),
            "rdf_input": safeLiteral(
                data.get("_input")
                or data.get("_url")
                or ""
            ),
            "rdf_item_json": rdf_json_literal(
                dict(data.get("item", item))
            ),
        }
    )
    return data

def make_page_rdf_data(
    item_data: Mapping[str, Any],
    *,
    page_no: int | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Add RDF-safe item and page bindings to shared page data."""

    data = (
        dict(item_data)
        if page_no is None and text is None and "page_iri" in item_data
        else make_page_data(
            item_data,
            page_no=int(page_no),
            text="" if text is None else text,
        )
    )

    # Page templates may also use item-level RDF bindings.
    data = make_item_rdf_data(data)

    data.update(
        {
            "rdf_page": safeNamedNode(data["page_iri"]),
            "rdf_page_id": safeLiteral(data["page_id"]),
            "rdf_page_no": Literal(
                str(int(data["page"])),
                datatype=NamedNode(f"{XSD_NS}int"),
            ),
            "rdf_text": safeLiteral(data["text"]),
            "rdf_text_len": Literal(
                            str(len(data["text"])),
                            datatype=NamedNode(f"{XSD_NS}int"),
                        ),
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
        append: bool = False,
    ) -> None:
        self.path = Path(path)
        self.spec = spec
        self.compresslevel = compresslevel
        self.append = append
        self._output: BinaryIO | None = None

    def __enter__(self) -> "RdfNqGzipSink":
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._output = gzip.open(
            self.path,
            mode="ab" if self.append else "wb",
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


__all__ = [
    "RDF_JSON",
    "RdfNqGzipSink",
    "make_item_data",
    "make_item_iri",
    "make_item_rdf_data",
    "make_page_data",
    "make_page_iri",
    "make_page_rdf_data",
    "rdf_json_literal",
]
