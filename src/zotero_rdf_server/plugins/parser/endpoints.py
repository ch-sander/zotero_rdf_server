from fastapi import APIRouter, Depends, Query, HTTPException, Body
from zotero_rdf_server.global_store import *
from zotero_rdf_server.rdf import *
from zotero_rdf_server.logging_config import logger, LogLevel
from zotero_rdf_server.config import *
from zotero_rdf_server.models import ZoteroLibrary
from zotero_rdf_server.utils import *
from zotero_rdf_server.api import require_writable

router = APIRouter(tags=["Semantics"])
open_router = APIRouter()


@router.get("/parse_notes", summary="Parse notes", description="Triggers the parsing of all Zotero notes with semantic-html plugin", tags=["RDF"], dependencies=[Depends(require_writable)])
async def parse_notes(
    delete: bool = Query(default=False, description="Delete all existing triples related to parsed note"),
    graph: str | None = Query(default=None, description="Named graph IRI containing the items/notes (optional)"),
    note_predicate: str | None  = Query(default=f"{ZOT_NS}note", description="predicate for note HTML"),
    query: str | None = Query(default=None, description="Query to retrieve notes, requires ?s (=note item IRI) and ?o (=Note HTML) as bindings (optional)"),
    push: bool | None = Query(default=True, description="Push triples to store (true by default)")
    ):
    from .parse_note import parse_all_notes                                
    from zotero_rdf_server import global_store
    store = global_store.get_store(force=True)
    

    checked_graph, all_graphs = get_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")
    
    if not note_predicate:
        predicate = safeNamedNode(f"{ZOT_NS}note")
    else:
        predicate = safeNamedNode(f"{note_predicate}")

    for lib_cfg in ZOTERO_LIBRARIES_CONFIGS: # TODO improve
        lib = ZoteroLibrary(lib_cfg)
        if not graph or graph == lib.base_url:
            logger.info(f"starting note parsing for {lib.base_url}...")
            result=parse_all_notes(lib, store, note_predicate=predicate, query_str=query, delete=delete,push=push) # TODO no graph given
        elif graph and graph != lib.base_url:
            logger.debug(f"{graph} skipped")
        else:
            logger.warning(f"{graph} not yet supported but defined via config")
    return {"success":f"{result} notes parsed"}

from pydantic import BaseModel, Field, ConfigDict
import asyncio

class QLeverTextIndexRequest(BaseModel):
    docs_query: str
    words_query: str
    docs_file: str
    words_file: str
    lowercase: bool = True
    use_default_graph_as_union: bool = True
    require_word_and_entity: bool = False
    max_record_id: int | None = Field(default=None, ge=0)

@router.post(
    "/qlever/text-index",
    summary="Generate QLever text index",
    dependencies=[Depends(require_writable)],
)
async def generate_qlever_text_indexes(
    library: str | None = Query(
        default=None,
        description=(
            "Optional library filter by name, base URL, "
            "library ID, or library type. Ignored when a custom request body is supplied."
        ),
    ),
    request: QLeverTextIndexRequest | None = Body(
        default=None,
        openapi_examples={
            "custom": {
                "summary": "Custom QLever index",
                "value": {
                    "docs_query": "/app/plugins/parser/queries/qlever-docsfile.rq",
                    "words_query": "/app/plugins/parser/queries/qlever-wordsfile.rq",
                    "docs_file": "Qlever/index.docsfile.tsv",
                    "words_file": "Qlever/index.wordsfile.tsv",
                    "lowercase": True,
                    "use_default_graph_as_union": True,
                    "require_word_and_entity": False,
                    "max_record_id": None,
                },
            },
        },
    ),
):
    ...
    from zotero_rdf_server import global_store
    from .qlever_helpers import create_qlever_text_index, write_qlever_text_index, _serialize_qlever_stats

    store = global_store.get_store(force=True)

    # Request
    if request is not None:
        try:
            stats = await asyncio.to_thread(
                write_qlever_text_index,
                store=store,
                config=request.model_dump(),
                load_text_like=load_text_like,
                base_dir=EXPORT_DIRECTORY,
            )
        except Exception as exc:
            logger.exception(
                "QLever text index creation from request failed"
            )
            raise HTTPException(
                status_code=500,
                detail=str(exc),
            ) from exc

        return {
            "mode": "request",
            "indexes": [_serialize_qlever_stats(stats)],
        }

    # No Request Body
    results: list[dict[str, Any]] = []
    matched_library = False
    
    for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
        lib = ZoteroLibrary(lib_cfg)
        logger.info(f"Reading {lib.name}...")
        if library is not None and library not in {
            lib.name,
            lib.base_url,
            lib.library_id,
            lib.library_type,
        }:
            continue

        matched_library = True

        parser_config = (lib.plugin or {}).get(
            "notes_parser",
            {},
        )

        if not isinstance(parser_config, dict):
            logger.warning(
                "Invalid notes_parser config for '%s'",
                lib.name,
            )
            continue

        if not parser_config.get("qlever_text_index"):
            logger.debug(
                "No QLever text index configured for '%s'",
                lib.name,
            )
            continue

        try:
            logger.info(f"qlever_text_index: {lib.qlever_text_index}")
            stats = await asyncio.to_thread(
                create_qlever_text_index,
                lib,
                store,
            )
        except Exception as exc:
            logger.exception(
                "QLever text index creation failed for '%s'",
                lib.name,
            )
            results.append({
                "library": lib.name,
                "success": False,
                "error": str(exc),
            })
            continue

        if stats is not None:
            results.append({
                "library": lib.name,
                "success": True,
                **_serialize_qlever_stats(stats),
            })

    if library is not None and not matched_library:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown library: {library}",
        )

    if not results:
        raise HTTPException(
            status_code=404,
            detail="No configured QLever text indexes found",
        )

    return {
        "mode": "configured_libraries",
        "indexes": results,
    }

# END