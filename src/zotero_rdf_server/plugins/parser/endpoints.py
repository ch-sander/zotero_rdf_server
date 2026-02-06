from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from typing import Literal as TypeLiteral
import logging
from pathlib import Path
from zotero_rdf_server.store import *
from zotero_rdf_server.rdf import *
from zotero_rdf_server.logging_config import logger, LogLevel
from zotero_rdf_server.config import *
from zotero_rdf_server.models import ZoteroLibrary
from zotero_rdf_server.utils import *

router = APIRouter(tags=["Semantics"])

@router.get("/parse_notes", summary="Parse notes", description="Triggers the parsing of all Zotero notes with semantic-html plugin", tags=["RDF"])
async def parse_notes(
    delete: bool = Query(default=False, description="Delete all existing triples related to parsed note"),
    graph: str | None = Query(default=None, description="Named graph IRI containing the items/notes (optional)"),
    note_predicate: str | None  = Query(default=f"{ZOT_NS}note", description="predicate for note HTML"),
    query: str | None = Query(default=None, description="Query to retrieve notes, requires ?s (=note item IRI) and ?o (=Note HTML) as bindings (optional)"),
    push: bool | None = Query(default=True, description="Push triples to store (true by default)")
    ):
    from .parse_note import parse_all_notes
    from zotero_rdf_server.store import store

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