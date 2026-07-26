from pathlib import Path
from zotero_rdf_server.utils import ensure_import, load_text_like
from fastapi import Body, APIRouter, HTTPException, Query
from zotero_rdf_server.config import IMPORT_DIRECTORY
from zotero_rdf_server.logging_config import logger
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

here = Path(__file__).resolve().parent
requirements = here / "requirements.txt"

router = APIRouter(tags=["Citation Extraction"])

ALLOWED_LOCAL_ROOTS = (
    IMPORT_DIRECTORY.resolve(),
)

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30.0


@router.post(
    "/zotero/import",
    summary="Extract Zotero citations",
    description=(
        "Extract Zotero citations from a local or remote ODT/DOCX "
        "document and return them as JSON-LD."
    ),
    response_class=JSONResponse,
)
async def import_zotero_document(
    payload: dict[str, Any] = Body(
        ...,
        description=(
            "Import definition containing the document source, document IRI, "
            "optional graph IRI, and JSON-LD context."
        ),
        openapi_examples={
            "local_file": {
                "summary": "Import a local document",
                "value": {
                    "source": {
                        "kind": "path",
                        "path": "/app/import/files/test.odt",
                    },
                    "document_uri": (
                        "https://zotero-rdf-server.org/documents/test"
                    ),
                    "graph_uri": (
                        "https://zotero-rdf-server.org/graphs/citations"
                    ),
                    "context": {
                        "z": "https://zotero-rdf-server.org/plugin/zc2rdf/",
                        "owl": "http://www.w3.org/2002/07/owl#",

                        "Document": "z:Document",
                        "Citation": "z:Citation",
                        "CitationItem": "z:CitationItem",

                        "citation": {
                            "@id": "z:citation",
                            "@type": "@id"
                        },
                        "item": {
                            "@id": "z:item",
                            "@type": "@id"
                        },
                        "target": {
                            "@id": "z:target",
                            "@type": "@id"
                        },
                        "sameAs": {
                            "@id": "owl:sameAs",
                            "@type": "@id"
                        },

                        "order": "z:order",
                        "locator": "z:locator",
                        "label": "z:label",
                        "prefix": "z:prefix",
                        "suffix": "z:suffix"
                        },
                },
            },
            "remote_file": {
                "summary": "Download and import a document",
                "value": {
                    "source": {
                        "kind": "url",
                        "url": "https://example.org/article.odt",
                    },
                    "document_uri": (
                        "https://zotero-rdf-server.org/documents/test"
                    ),
                    "graph_uri": None,
                    "context": {
                        "z": "https://zotero-rdf-server.org/plugin/zc2rdf/",
                        "owl": "http://www.w3.org/2002/07/owl#",

                        "Document": "z:Document",
                        "Citation": "z:Citation",
                        "CitationItem": "z:CitationItem",

                        "citation": {
                            "@id": "z:citation",
                            "@type": "@id"
                        },
                        "item": {
                            "@id": "z:item",
                            "@type": "@id"
                        },
                        "target": {
                            "@id": "z:target",
                            "@type": "@id"
                        },
                        "sameAs": {
                            "@id": "owl:sameAs",
                            "@type": "@id"
                        },

                        "order": "z:order",
                        "locator": "z:locator",
                        "label": "z:label",
                        "prefix": "z:prefix",
                        "suffix": "z:suffix"
                        },
                },
            },
        },
    ),
) -> JSONResponse:
    source = payload.get("source")
    document_uri = payload.get("document_uri")
    graph_uri = payload.get("graph_uri")
    context = payload.get("context")

    if not isinstance(source, dict):
        raise HTTPException(422, "source must be an object")

    if not isinstance(document_uri, str) or not document_uri.strip():
        raise HTTPException(
            422,
            "document_uri must be a non-empty string",
        )

    if graph_uri is not None and (
        not isinstance(graph_uri, str) or not graph_uri.strip()
    ):
        raise HTTPException(
            422,
            "graph_uri must be a string or null",
        )

    if not isinstance(context, dict):
        raise HTTPException(
            422,
            "context must be a JSON object",
        )

    source_kind = source.get("kind")

    try:
        from .citation2rdf import index_document

        if source_kind == "path":
            path = _resolve_local_path(source.get("path"))

            result = await run_in_threadpool(
                index_document,
                path,
                document_uri=document_uri,
                graph_uri=graph_uri,
                context=context,
            )

        elif source_kind == "url":
            url = source.get("url")

            if not isinstance(url, str) or not url.strip():
                raise HTTPException(
                    422,
                    "source.url must be a non-empty string",
                )

            with tempfile.TemporaryDirectory(
                prefix="zotero-jsonld-download-"
            ) as temporary_directory:
                path = Path(temporary_directory) / "document.bin"

                await run_in_threadpool(
                    _download_file,
                    url,
                    path,
                )

                result = await run_in_threadpool(
                    index_document,
                    path,
                    document_uri=document_uri,
                    graph_uri=graph_uri,
                    context=context,
                )

        else:
            raise HTTPException(
                422,
                "source.kind must be either 'path' or 'url'",
            )

    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    return JSONResponse(
        content=result,
        media_type="application/ld+json",
    )

def _resolve_local_path(value: Any) -> Path:
    """Resolve a local path and enforce the configured root directories."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(422, "source.path must be a non-empty string")

    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(404, "Local document was not found") from exc

    if not path.is_file():
        raise HTTPException(422, "Local source is not a regular file")

    if not any(path.is_relative_to(root) for root in ALLOWED_LOCAL_ROOTS):
        raise HTTPException(
            403,
            "Local source is outside the allowed directories",
        )

    return path


def _download_file(url: str, destination: Path) -> None:
    """Download a document with urllib and enforce a maximum size."""
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Download URL must use HTTP or HTTPS")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ZoteroCitationIndexer/1.0",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    ) as response:
        content_length = response.headers.get("Content-Length")

        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None

            if (
                declared_size is not None
                and declared_size > MAX_DOWNLOAD_BYTES
            ):
                raise ValueError("Remote document exceeds the size limit")

        total = 0

        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break

                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        "Remote document exceeds the size limit"
                    )

                output.write(chunk)