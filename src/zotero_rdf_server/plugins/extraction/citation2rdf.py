"""
Minimal Zotero citation extractor for ODT and DOCX documents.

The public API accepts a filesystem path or binary file-like object, detects
ODT or DOCX from ZIP contents, and returns a lightweight JSON-LD dictionary.
"""

import json
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from zotero_rdf_server.utils import ensure_import
from lxml import etree

here = Path(__file__).resolve().parent
requirements = here / "requirements.txt"

semantic_parse_note = ensure_import(
    "lxml",
    attr="etree",
    requirements=requirements,
)


ODF_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

ODF_NAME_ATTR = f"{{{ODF_TEXT_NS}}}name"
WORD_FLD_CHAR_TYPE_ATTR = f"{{{WORD_NS}}}fldCharType"
WORD_INSTR_ATTR = f"{{{WORD_NS}}}instr"
WORD_NSMAP = {"w": WORD_NS}

Source = str | PathLike[str] | BinaryIO


@dataclass(frozen=True)
class CitationItem:
    """One cited Zotero item within a citation occurrence."""

    index: int
    target: str
    locator: str | None = None
    label: str | None = None
    prefix: str | None = None
    suffix: str | None = None


@dataclass(frozen=True)
class Citation:
    """One Zotero citation occurrence."""

    index: int
    citation_id: str | None
    rendered_text: str | None
    source_part: str | None
    items: tuple[CitationItem, ...]


def index_document(
    source: Source,
    *,
    document_uri: str,
    context: Mapping[str, Any],
    graph_uri: str | None = None,
) -> dict[str, Any]:
    """
    Extract Zotero citations and return a fixed JSON-LD structure.

    Args:
        source:
            A local path or binary file-like object.
        document_uri:
            IRI assigned to the indexed document.
        context:
            JSON-LD @context object. All other keys and structure are fixed.
        graph_uri:
            Optional IRI wrapping the document in an @graph object.

    Returns:
        A JSON-serializable JSON-LD dictionary.
    """
    if not isinstance(document_uri, str) or not document_uri.strip():
        raise ValueError("document_uri must not be empty")
    if graph_uri is not None and (
        not isinstance(graph_uri, str) or not graph_uri.strip()
    ):
        raise ValueError("graph_uri must be a non-empty string or None")
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping")

    with tempfile.TemporaryDirectory(
        prefix="zotero-jsonld-index-"
    ) as temporary_directory:
        package_path = Path(temporary_directory) / "document-package.zip"
        _copy_source(source, package_path)
        document_format = detect_document_format(package_path)

        if document_format == "odt":
            citations = extract_odt_citations(package_path)
        else:
            citations = extract_docx_citations(package_path)

    document = {
        "@id": document_uri,
        "@type": "Document",
        "citation": [
            _citation_to_jsonld(document_uri, citation)
            for citation in citations
        ],
    "cites": [
        {
            "@id": target,
            "@type": "CitedDocument",
        }
        for target in dict.fromkeys(
            item.target
            for citation in citations
            for item in citation.items
            if item.target
        )
    ],
    }

    if graph_uri:
        return {
            "@context": dict(context),
            "@id": graph_uri,
            "@graph": [document],
        }

    return {
        "@context": dict(context),
        **document,
    }


def detect_document_format(path: Path) -> str:
    """Detect ODT or DOCX from ZIP package contents."""
    if not zipfile.is_zipfile(path):
        raise ValueError("Input is not a ZIP-based ODT or DOCX document")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())

        if "word/document.xml" in names and "[Content_Types].xml" in names:
            return "docx"

        if "content.xml" in names:
            mime_type = ""
            try:
                mime_type = archive.read("mimetype").decode("ascii").strip()
            except (KeyError, UnicodeDecodeError):
                pass

            if (
                mime_type == "application/vnd.oasis.opendocument.text"
                or "META-INF/manifest.xml" in names
            ):
                return "odt"

    raise ValueError("Could not identify the package as ODT or DOCX")


def extract_odt_citations(path: Path) -> list[Citation]:
    """Extract Zotero citations from an ODT package."""
    with zipfile.ZipFile(path) as archive:
        root = etree.fromstring(
            archive.read("content.xml"),
            parser=_secure_xml_parser(),
        )

    citations: list[Citation] = []
    marker_pairs = (
        ("reference-mark-start", "reference-mark-end"),
        ("bookmark-start", "bookmark-end"),
    )

    for start_local, end_local in marker_pairs:
        start_tag = f"{{{ODF_TEXT_NS}}}{start_local}"
        end_tag = f"{{{ODF_TEXT_NS}}}{end_local}"

        for parent in root.iter():
            children = list(parent)

            for start_index, child in enumerate(children):
                if child.tag != start_tag:
                    continue

                field_code = child.get(ODF_NAME_ATTR, "")
                if not is_zotero_citation(field_code):
                    continue

                visible_parts: list[str] = []
                if child.tail:
                    visible_parts.append(child.tail)

                for sibling in children[start_index + 1:]:
                    if (
                        sibling.tag == end_tag
                        and sibling.get(ODF_NAME_ATTR, "") == field_code
                    ):
                        break
                    visible_parts.extend(sibling.itertext())
                    if sibling.tail:
                        visible_parts.append(sibling.tail)
                else:
                    continue

                citation = _parse_citation(
                    field_code=field_code,
                    visible_text="".join(visible_parts).strip(),
                    source_part="content.xml",
                    index=len(citations) + 1,
                )
                if citation is not None:
                    citations.append(citation)

    return citations


def extract_docx_citations(path: Path) -> list[Citation]:
    """Extract Zotero citations from a DOCX package."""
    citations: list[Citation] = []

    with zipfile.ZipFile(path) as archive:
        for part_name, xml_bytes in _iter_docx_parts(archive):
            root = etree.fromstring(
                xml_bytes,
                parser=_secure_xml_parser(),
            )
            fields = (
                list(_iter_docx_complex_fields(root))
                + list(_iter_docx_simple_fields(root))
            )

            for field_code, visible_text in fields:
                if not is_zotero_citation(field_code):
                    continue

                citation = _parse_citation(
                    field_code=field_code,
                    visible_text=visible_text,
                    source_part=part_name,
                    index=len(citations) + 1,
                )
                if citation is not None:
                    citations.append(citation)

    return citations


def is_zotero_citation(field_code: str) -> bool:
    """Return True if a field contains a Zotero CSL citation."""
    normalized = " ".join(field_code.split())
    return "ZOTERO_ITEM" in normalized and "CSL_CITATION" in normalized


def extract_json_object(field_code: str) -> dict[str, Any]:
    """Extract the first complete JSON object from a Zotero field code."""
    start = field_code.find("{")
    if start < 0:
        raise ValueError("No JSON object found in Zotero field code")

    value, _end = json.JSONDecoder().raw_decode(field_code[start:])
    if not isinstance(value, dict):
        raise ValueError("Zotero field data is not a JSON object")
    return value


def _parse_citation(
    *,
    field_code: str,
    visible_text: str,
    source_part: str,
    index: int,
) -> Citation | None:
    """Parse one Zotero field without retaining bibliographic itemData."""
    try:
        data = extract_json_object(field_code)
    except (ValueError, json.JSONDecodeError):
        return None

    raw_items = data.get("citationItems", [])
    items: list[CitationItem] = []

    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                continue

            target = _first_item_uri(raw_item)
            if target is None:
                continue

            items.append(
                CitationItem(
                    index=len(items) + 1,
                    target=target,
                    locator=_optional_text(raw_item.get("locator")),
                    label=_optional_text(raw_item.get("label")),
                    prefix=_optional_text(raw_item.get("prefix")),
                    suffix=_optional_text(raw_item.get("suffix")),
                )
            )

    return Citation(
        index=index,
        citation_id=_optional_text(data.get("citationID")),
        rendered_text=_optional_text(visible_text),
        source_part=_optional_text(source_part),
        items=tuple(items),
    )


def _citation_to_jsonld(
    document_uri: str,
    citation: Citation,
) -> dict[str, Any]:
    """Convert one normalized citation to the fixed JSON-LD structure."""
    citation_id = f"{citation.citation_id or citation.index}"
    citation_uri = f"{document_uri.rstrip('/')}/citation/{citation_id}"
    result: dict[str, Any] = {
        "@id": citation_uri,
        "@type": "Citation",
        "order": citation.index,
        "item": [
            _item_to_jsonld(document_uri, citation_uri, item)
            for item in citation.items
        ],
    }
    _set_optional(result, "citationId", citation.citation_id)
    _set_optional(result, "text", citation.rendered_text)
    _set_optional(result, "sourcePart", citation.source_part)
    return result


def _item_to_jsonld(
    document_uri: str,
    citation_uri: str,
    item: CitationItem,
) -> dict[str, Any]:
    """Convert one normalized citation item to JSON-LD."""
    result: dict[str, Any] = {
        "@id": f"{citation_uri}/item/{item.index}",
        "@type": "CitationItem",
        "order": item.index,
        "citingEntity": document_uri,
        "target": item.target,
        "characterization": "cito:cites",
    }
    _set_optional(result, "locator", item.locator)
    _set_optional(result, "label", item.label)
    _set_optional(result, "prefix", item.prefix)
    _set_optional(result, "suffix", item.suffix)
    return result


def _set_optional(
    target: dict[str, Any],
    key: str,
    value: Any,
) -> None:
    """Set a key only when the value is present."""
    if value is not None:
        target[key] = value


def _first_item_uri(item: Mapping[str, Any]) -> str | None:
    """Return the first usable Zotero item URI."""
    uris = item.get("uris")
    if not isinstance(uris, list):
        return None

    for uri in uris:
        if isinstance(uri, str) and "/items/" in uri:
            normalized = uri.strip()
            if normalized:
                return normalized
    return None


def _optional_text(value: Any) -> str | None:
    """Normalize an optional scalar value to text."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _copy_source(source: Source, destination: Path) -> None:
    """Copy a path or binary stream to the temporary package file."""
    if isinstance(source, (str, PathLike)):
        shutil.copy2(Path(source), destination)
        return

    if not hasattr(source, "read"):
        raise TypeError("source must be a path or binary file-like object")

    with destination.open("wb") as output:
        shutil.copyfileobj(source, output)


def _secure_xml_parser() -> etree.XMLParser:
    """Create an XML parser with external entity resolution disabled."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )


def _iter_docx_parts(
    archive: zipfile.ZipFile,
) -> Iterator[tuple[str, bytes]]:
    """Yield DOCX XML parts that may contain Zotero citations."""
    exact_parts = {
        "word/document.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
        "word/comments.xml",
    }

    for name in archive.namelist():
        is_header_or_footer = (
            name.startswith("word/header")
            or name.startswith("word/footer")
        )
        if name in exact_parts or (
            is_header_or_footer and name.endswith(".xml")
        ):
            yield name, archive.read(name)


def _iter_docx_complex_fields(
    root: etree._Element,
) -> Iterator[tuple[str, str]]:
    """Yield complex Word fields as field code and rendered text."""
    stack: list[dict[str, Any]] = []

    for element in root.iter():
        if element.tag == f"{{{WORD_NS}}}fldChar":
            field_type = element.get(WORD_FLD_CHAR_TYPE_ATTR)

            if field_type == "begin":
                stack.append(
                    {
                        "instruction": [],
                        "result": [],
                        "in_result": False,
                    }
                )
            elif field_type == "separate" and stack:
                stack[-1]["in_result"] = True
            elif field_type == "end" and stack:
                completed = stack.pop()
                field_code = "".join(completed["instruction"]).strip()
                visible_text = "".join(completed["result"]).strip()
                yield field_code, visible_text

                if stack and stack[-1]["in_result"]:
                    stack[-1]["result"].append(visible_text)
            continue

        if not stack:
            continue

        current = stack[-1]
        if element.tag == f"{{{WORD_NS}}}instrText":
            if not current["in_result"] and element.text:
                current["instruction"].append(element.text)
        elif element.tag == f"{{{WORD_NS}}}t":
            if current["in_result"] and element.text:
                current["result"].append(element.text)


def _iter_docx_simple_fields(
    root: etree._Element,
) -> Iterator[tuple[str, str]]:
    """Yield simple Word fields."""
    for field in root.xpath(".//w:fldSimple", namespaces=WORD_NSMAP):
        field_code = field.get(WORD_INSTR_ATTR, "").strip()
        visible_text = "".join(
            field.xpath(".//w:t/text()", namespaces=WORD_NSMAP)
        ).strip()
        yield field_code, visible_text


__all__ = [
    "Citation",
    "CitationItem",
    "detect_document_format",
    "extract_docx_citations",
    "extract_odt_citations",
    "index_document",
]