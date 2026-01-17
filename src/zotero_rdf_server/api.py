from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from typing import Literal as TypeLiteral
import logging
from pathlib import Path
# import asyncio
from .store import *
from .rdf import *
from .logging_config import logger, LogLevel
from .config import *
from .models import ZoteroLibrary
from .utils import *

router = APIRouter()
plugin_router = APIRouter(
    prefix="/plugin",
    tags=["Plugins"],
)

@router.get("/export", summary="Create export", description=f"Exports the store or a named graph to {EXPORT_DIRECTORY}", tags=["data"])
async def export_graph(
    format: str = Query("trig"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional)")
):
    from .store import store
    checked_graph, all_graphs = get_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")

    EXPORT_DIRECTORY.mkdir(parents=True,exist_ok=True)

    rdf_format = ensure_rdf_format(format=format) # RdfFormat.from_extension(format.lower())
    if rdf_format is None:
        raise ValueError(f"Unsupported RDF format: {format}")

    extension = rdf_format.file_extension
    filename_base = iri_to_filename(graph) if graph else "zotero_store"
    
    path = EXPORT_DIRECTORY / f"{filename_base}.{extension}"
    logger.info(f"Create {path}")

    kwargs = {}
    if graph:
        kwargs["from_graph"] = checked_graph
        logger.info(f"Export from graph: {checked_graph}")
    elif not rdf_format.supports_datasets:        
        kwargs["from_graph"] = DefaultGraph()
        logger.info("Export from DefaultGraph")
    else:
        logger.info(f"Export from graphs: {list(store.named_graphs())}")
        
    logger.info(f"Checked graph: {checked_graph!r}")
    logger.info(f"Graph triples: {len(list(store.quads_for_pattern(None,None,None,checked_graph)))}")
    store.dump(output=path, format=rdf_format, prefixes=PREFIXES, from_graph=checked_graph, base_iri= str(checked_graph.value).rstrip('/') + '/' if checked_graph else None) #
    return {"success":f"Export to: {path}"}
    # return FileResponse(path, filename=os.path.basename(path))

@router.get("/backup", summary="Create backup", description=f"Creates a complete backup of the store to {BACKUP_DIRECTORY}", tags=["data"])
async def backup_store():
    from .store import store
    backup_root = Path(BACKUP_DIRECTORY).resolve()
    backup_path = backup_root / "Store"
    log_file = backup_root / "backup.log"

    try:
        store_path = Path(STORE_DIRECTORY).resolve()
    except AttributeError:
        return {"error": "The current store was not found in {STORE_DIRECTORY} (maybe in-memory DB?)"}

    if backup_path == store_path or backup_path in store_path.parents:
        raise RuntimeError("Cannot backup into the current store's own directory")

    if backup_path.exists():
        shutil.rmtree(backup_path, ignore_errors=True)
        log_file.write_text(f"[{datetime.now().isoformat()}] Deleted old Store backup\n", encoding="utf-8")

    store.backup(str(backup_path))
    backup_store = Store(str(backup_path))
    graphs = [str(g) for g in backup_store.named_graphs()]
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] Created new backup in {backup_path}\n")

    return {"status": "success", "backup store":{"path": backup_path,"named_graphs":graphs, "len":len(store)}}

@router.get("/reload", summary="Reload app", description="Will trigger a reload, even if not set in config.", tags=["data"])
async def reload_store(logging_level: LogLevel = Query(default=log_level, description="Sets log level")):
    if logging_level:
        current_level = logger.level
        new_level = getattr(logging, logging_level.upper(), None)
        if not isinstance(new_level, int):
            return {"error": f"Invalid log level: {logging_level}"}
        
        logger.setLevel(new_level)
        try:
            refresh_store(True)
        finally:
            logger.setLevel(current_level)
    else:
        refresh_store(True)
    from .store import store
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}

@router.get("/optimize", summary="Optimize Store", description="Will optimize the oxigraph store", tags=["data"])
async def optimize_store():
    from .store import store
    store.optimize()
    return {"success":"Store optimized"}


@router.get("/libs", summary="List of all libraries", description="Returns all available libraries with configuration.", tags=["config"])
async def get_libs():
    result = [ZoteroLibrary(cfg) for cfg in ZOTERO_LIBRARIES_CONFIGS]
    return {"success": result}

@router.get("/graphs", summary="List of all named graphs", description="Returns all available named graphs.", tags=["RDF"])
async def list_graphs():
    from .store import store
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}



@router.get("/csv", summary="Export CSV", description="Exports a named graph or the entire store as CSV or loads a CSV as RDF into the store", tags=["RDF"])
async def get_csv(
    graph: str | None = Query(default=None, description="Named graph IRI (optional)"),
    load_csv: str | Path | None = Query(default=None, description="Load a CSV file into the store"),
    delete: bool | None = Query(default=False, description="Removes triples from graph if true, done before loading triples (you may only use subject IRIs to just delete)")
    ):
    from collections import defaultdict
    import csv

    EXPORT_DIRECTORY.mkdir(parents=True,exist_ok=True)
    output_file = EXPORT_DIRECTORY / "export.csv"
    delimiter = " | "

    from .store import store

    checked_graph, all_graphs = get_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")
    
    # subject → { predicate → [objects...] }
    # NamedNodes as objects are wrapped in <> for both export and import
    records = defaultdict(lambda: defaultdict(list))
    all_predicates = set()
    for quad in store.quads_for_pattern(None, None, None, checked_graph):
        subj = (quad.subject.value)
        pred = (quad.predicate.value)
        obj = quad.object.value if isinstance(quad.object,Literal) else str(quad.object)
        records[subj][pred].append(obj)
        all_predicates.add(pred)
    columns = ["IRI"] + sorted(all_predicates)
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for subj, preds in sorted(records.items()):
            row = [subj]
            for pred in columns[1:]:
                values = preds.get(pred, [])
                row.append(delimiter.join(values))
            writer.writerow(row)

    load_csv = safe_path(load_csv)
    if load_csv and load_csv.is_file() and load_csv is not output_file:
        if delete:
            subjects = set()
            with open(load_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    subj_iri = row["IRI"].strip()
                    if subj_iri:
                        subjects.add(safeNamedNode(subj_iri))
            for subj in subjects:
                for quad in store.quads_for_pattern(subj, None, None, checked_graph):
                    store.remove(quad)

        with open(load_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                subj_raw = row.get("IRI", "").strip("<>").strip()
                if not subj_raw:
                    continue
                subj = safeNamedNode(subj_raw)

                for pred_label, cell in row.items():
                    if pred_label == "IRI" or not cell.strip():
                        continue
                    pred_raw = pred_label.strip("<>").strip()
                    if not pred_raw:
                        continue
                    predicate = safeNamedNode(pred_raw)

                    for value in cell.split(delimiter):
                        value = value.strip()
                        if not value:
                            continue

                        if value.startswith("<") and value.endswith(">") and value.startswith("http"):
                            obj = safeNamedNode(value.strip("<>"))
                        else:
                            obj = Literal(value)

                        if subj and predicate and obj:
                            quad = Quad(subj, predicate, obj, checked_graph)
                            store.add(quad)
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}

### PLUGINS ###

@plugin_router.get(
    "/znotes2rdf",
    summary="Zotero Notes to RDF",
    description="Writes all RDF blocks in Zotero Notes HTML into RDF, either Store or file.",
    tags=["RDF", "Plugins"]
)
async def znotes2rdf(
    graph: str | None = Query(default=None, description="Named graph IRI (optional) to write RDF resources to. Will use this named graph to detect Zotero library sync configuration to read from collection if no config parameters are given to the endpoint."),
    input_format: str = Query(default="ttl", description="RDF format used in notes (e.g. ttl, nt)"),
    output_format: str | None = Query(default=None, description="Optional RDF format to export (e.g. json-ld). Defaults to trig."),
    push: bool = Query(default=True, description=f"Push loaded triples to main store (default: true). Otherwise writes file in {EXPORT_DIRECTORY}"),
    clear_graph: bool = Query(default=True, description="Deletes existing graph in Store, otherwise extends (only relevant if push)"),
    api_key: str | None = Query(default=None, description="Zotero API key (overrides config)"),
    library_id: str | None = Query(default=None, description="Zotero Library ID (overrides config)"),
    library_type: str | None = Query(default=None, description="Zotero Library type (user or group, overrides config)"),
    collection_id: str | None = Query(default=None, description="Zotero Collection ID (overrides config)")
):
    from zotero_rdf_server.plugins.rdf_notes.rdf_znotes import znotes_to_rdf
    from .store import store

    checked_graph, all_graphs = get_graph(graph)    

    if graph and not checked_graph:
        logger.warning(f"Graph IRI not yet in store!")

    graph = checked_graph if checked_graph else safeNamedNode(graph)

    def process_config(api_key, library_id, library_type, collection_id=None, xgraph = graph):
        return znotes_to_rdf(
            api_key=api_key,
            library_id=library_id,
            library_type=library_type,
            collection_id=collection_id,
            input_format=input_format,
            output_format=None, # to return always a Store
            prefixes=PREFIXES,
            graph = xgraph or DefaultGraph()
        )

    def save_result(input, target_graph):
        target_graph = safeNamedNode(target_graph)
        if target_graph and isinstance(target_graph, NamedNode):            
            if isinstance(input, Store):
                if push:
                    if clear_graph:
                        store.clear_graph(target_graph)
                    store.bulk_extend(input) # .quads_for_pattern(None, None, None, target_graph))
                else:
                    EXPORT_DIRECTORY.mkdir(parents=True,exist_ok=True)
                    rdf_format = ensure_rdf_format(format=output_format) # RdfFormat.from_extension(output_format.lower())
                    if rdf_format is None:
                        raise ValueError(f"Unsupported RDF format: {output_format}")

                    extension = rdf_format.file_extension
                    filename_base = iri_to_filename(target_graph) if target_graph else "zotero_notes"                    

                    path = EXPORT_DIRECTORY / f"{filename_base}.{extension}"

                    input.dump(output=path, format=rdf_format, prefixes=PREFIXES, from_graph = target_graph, base_iri= str(target_graph.value).rstrip('/') + '/' if target_graph else None)

                return len(input)
        else:
            logger.error("No graph given to load Zotero Notes to")
            return 0

    loaded_total = 0
    try:
        # Case 1: Direct API parameters provided
        if api_key and library_id and library_type and checked_graph:
            loaded_total += save_result(
                process_config(api_key, library_id, library_type, collection_id, graph), graph
            )

        else:
            # Case 2: Try config-based libraries
            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)
                if (
                    (not graph or graph == lib.base_url) and
                    lib.sync and
                    lib.sync.get("api_key") and
                    lib.sync.get("library_id") and
                    lib.sync.get("library_type")
                ):
                    loaded_total += save_result(
                        process_config(
                            lib.sync["api_key"],
                            lib.sync["library_id"],
                            lib.sync["library_type"],
                            lib.sync.get("collection_id"),
                            graph
                        ), graph or lib.sync.get('base_uri', DefaultGraph())
                    )
                    
        if loaded_total == 0:
            raise HTTPException(status_code=404, detail="Nothing found to load")

        return {"success": f"{loaded_total} {'triples' if loaded_total > 1 else 'resource'} loaded"}
    
    except Exception as e:
        logger.exception("Error during Zotero Notes to RDF export")
        raise HTTPException(status_code=500, detail=str(e))

@plugin_router.get("/rdf2znotes", summary="RDF to Zotero Notes", description="Creates Zotero Notes with RDF dump block from Store or RDF dataset", tags=["RDF", "Plugins"])
async def rdf2znotes(
    clear_collection: bool = Query(default=False, description="Delete all existing notes in collection"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional) to read RDF resources from. Will use this named graph to detect Zotero library sync configuration to write to collection if no config parameters are given to the endpoint."),
    input_file: str = Query(default=None, description="Reads from Store if no input file specified"),
    input_format: str = Query(default="trig", description="Input RDF format (default: trig). Only relevant if input file given"),
    output_format: str = Query(default="ttl", description="Output RDF format for note display (e.g. ttl, nt, json-ld)"),
    label_predicate: str = Query(default=RDFS_LABEL, description="Label predicate used to identify the label of a resource"),
    type_predicate: str = Query(default=RDF_TYPE, description="Type predicate used to identify the RDF type of a resource"),
    api_key: str | None = Query(default=None, description="Zotero API key (overrides config)"),
    library_id: str | None = Query(default=None, description="Zotero Library ID (overrides config)"),
    library_type: str | None = Query(default=None, description="Zotero Library type (user or group, overrides config)"),
    collection_id: str | None = Query(default=None, description="Zotero Collection ID (overrides config)")
):
    
    from zotero_rdf_server.plugins.rdf_notes.rdf_znotes import describe_resources, rdf_to_znotes
    checked_graph, all_graphs = get_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI: {checked_graph}. Use one of these or None: {all_graphs}")
    from .store import store
    
    def run_export(api_key, library_id, library_type, collection_id=None):
        logger.info(f"Exporting RDF to Zotero Notes for library_id={library_id}, collection={collection_id}")
        source = open(input_file) if input_file else store
        blocks = describe_resources(
            source=source,
            input_format=input_format,
            output_format=output_format,
            prefixes=PREFIXES,
            label_predicate=label_predicate,
            type_predicate=type_predicate,
            graph=checked_graph
        )
        logger.info(f"Found {len(blocks)} blocks to write")
        return rdf_to_znotes(
            blocks=blocks,
            api_key=api_key,
            library_id=library_id,
            library_type=library_type,
            collection_id=collection_id,
            clear_collection=clear_collection
        ), len(blocks)

    try:
        # Case 1: Parameters explicitly provided
        if api_key and library_id and library_type and collection_id:
            result, _ = run_export(api_key, library_id, library_type, collection_id)
            return result

        # Case 2: Look in config
        total = 0
        for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
            lib = ZoteroLibrary(lib_cfg)
            logger.info(f"Checking config for library: {lib.name}")
            if (
                (not graph or graph == lib.base_url) and
                lib.sync and
                lib.sync.get("api_key") and
                lib.sync.get("library_id") and
                lib.sync.get("library_type")
            ):
                _, count = run_export(
                    api_key=lib.sync["api_key"],
                    library_id=lib.sync["library_id"],
                    library_type=lib.sync["library_type"],
                    collection_id=lib.sync.get("collection_id")
                )
                total += count

        if total > 0:
            return {"success": f"{total} Zotero Notes created"}
        else:
            raise HTTPException(status_code=404, detail="No Zotero Notes created")

    except Exception as e:
        logger.exception("Error during RDF to Zotero Notes export")
        raise HTTPException(status_code=500, detail=str(e))
    

# COMBINED ENDPOINT FOR ZOTERO NOTES TO RDF #
# Currently not used as Swagger UI does not document params #
# from typing import Optional
# from pydantic import BaseModel, Field
# from fastapi import Depends
# Parameters for writeStore (Notes → RDF)
# class ZNotesToRDFParams(BaseModel):
#     input_format: str = Field(
#         default="ttl",
#         description="Only for task=writeStore. RDF format used inside Zotero Notes (e.g. ttl, nt)"
#     )
#     output_format: Optional[str] = Field(
#         default=None,
#         description="Only for task=writeStore. Optional RDF export format (e.g. json-ld)"
#     )
#     push: bool = Field(
#         default=True,
#         description="Only for task=writeStore. Push loaded triples to store (true) or write to file (false)"
#     )
#     clear_graph: bool = Field(
#         default=True,
#         description="Only for task=writeStore. Clear existing graph before writing"
#     )

# # Parameters for writeNote (RDF → Zotero Notes)
# class RDFToZNotesParams(BaseModel):
#     input_file: Optional[str] = Field(
#         default=None,
#         description="Only for task=writeNote. Optional RDF file to read from instead of store"
#     )
#     input_format: str = Field(
#         default="trig",
#         description="Only for task=writeNote. Input RDF format (default: trig)"
#     )
#     output_format: str = Field(
#         default="ttl",
#         description="Only for task=writeNote. Format to write into Zotero Note block (e.g. ttl, nt, json-ld)"
#     )
#     clear_collection: bool = Field(
#         default=False,
#         description="Only for task=writeNote. If true, all existing notes in the target collection are deleted"
#     )
#     label_predicate: str = Field(
#         default=RDFS_LABEL,
#         description="Only for task=writeNote. Predicate to use as label for resources"
#     )
#     type_predicate: str = Field(
#         default=RDF_TYPE,
#         description="Only for task=writeNote. Predicate to use for RDF type detection"
#     )

# @router.get(
#     "/znotes_rdf",
#     summary="Convert between Zotero Notes and RDF",
#     description="Use task=writeStore (notes → RDF) or task=writeNote (RDF → notes)",
#     tags=["RDF", "Plugins"]
# )
# async def znotes_rdf(
#     task: TypeLiteral["writeStore", "writeNote"] = Query(..., description="writeStore or writeNote"),
#     graph: Optional[str] = Query(
#         default=None,
#         description="Optional. Named graph IRI used for both reading and writing RDF. Also used to resolve Zotero library config."
#     ),
#     api_key: Optional[str] = Query(
#         default=None,
#         description="Optional. Zotero API key (overrides config). Required if config is not used."
#     ),
#     library_id: Optional[str] = Query(
#         default=None,
#         description="Optional. Zotero library ID (overrides config). Required if config is not used."
#     ),
#     library_type: Optional[str] = Query(
#         default=None,
#         description="Optional. Zotero library type ('user' or 'group'). Required if config is not used."
#     ),
#     collection_id: Optional[str] = Query(
#         default=None,
#         description="Optional. Zotero collection ID. Only used if writing to or reading from a specific collection."
#     ),
#     write_store_params: ZNotesToRDFParams = Depends(),
#     write_note_params: RDFToZNotesParams = Depends()
# ):
#     checked_graph, all_graphs = get_graph(graph)
#     if graph and not checked_graph:
#         raise HTTPException(status_code=400, detail=f"Invalid graph IRI: {graph}. Use one of: {all_graphs}")

#     try:
#         if task == "writeStore":
#             # Lazy import
#             from zotero_rdf_server.plugins.rdf_znotes import znotes_to_rdf

#             g = checked_graph if checked_graph else safeNamedNode(graph)

#             def process_config(api_key, library_id, library_type, collection_id=None, xgraph=g):
#                 return znotes_to_rdf(
#                     api_key=api_key,
#                     library_id=library_id,
#                     library_type=library_type,
#                     collection_id=collection_id,
#                     input_format=write_store_params.input_format,
#                     output_format=None,
#                     prefixes=PREFIXES,
#                     graph=xgraph
#                 )

#             def save_result(input, target_graph):
#                 target_graph = safeNamedNode(target_graph)
#                 if target_graph:
#                     if write_store_params.push:
#                         if write_store_params.clear_graph:
#                             store.clear_graph(target_graph)
#                         store.bulk_extend(input)
#                     else:
#                         EXPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
#                         fmt = ensure_rdf_format(write_store_params.output_format)
#                         if fmt is None:
#                             raise ValueError(f"Unsupported RDF format: {write_store_params.output_format}")
#                         path = EXPORT_DIRECTORY / f"{iri_to_filename(target_graph)}.{fmt.file_extension}"
#                         input.dump(
#                             output=path,
#                             format=fmt,
#                             prefixes=PREFIXES,
#                             from_graph=target_graph,
#                             base_iri=str(target_graph.value).rstrip("/") + "/"
#                         )
#                     return len(input)
#                 return 0

#             total = 0
#             if api_key and library_id and library_type:
#                 total += save_result(process_config(api_key, library_id, library_type, collection_id), g)
#             else:
#                 for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
#                     lib = ZoteroLibrary(lib_cfg)
#                     if (
#                         (not graph or graph == lib.base_url) and
#                         lib.sync.get("api_key") and
#                         lib.sync.get("library_id") and
#                         lib.sync.get("library_type")
#                     ):
#                         total += save_result(
#                             process_config(
#                                 lib.sync["api_key"],
#                                 lib.sync["library_id"],
#                                 lib.sync["library_type"],
#                                 lib.sync.get("collection_id"),
#                                 g or lib.sync.get("base_uri")
#                             ),
#                             g
#                         )

#             if total == 0:
#                 raise HTTPException(status_code=404, detail="No notes found to convert")
#             return {"success": f"{total} triples loaded"}

#         elif task == "writeNote":
#             from zotero_rdf_server.plugins.rdf_znotes import describe_resources, rdf_to_znotes

#             if write_note_params.input_file:
#                 with open(write_note_params.input_file, "r", encoding="utf-8") as f:
#                     source = f.read()
#             else:
#                 source = store

#             blocks = describe_resources(
#                 source=source,
#                 input_format=write_note_params.input_format,
#                 output_format=write_note_params.output_format,
#                 prefixes=PREFIXES,
#                 label_predicate=write_note_params.label_predicate,
#                 type_predicate=write_note_params.type_predicate,
#                 graph=checked_graph
#             )

#             if api_key and library_id and library_type and collection_id:
#                 result = rdf_to_znotes(
#                     blocks=blocks,
#                     api_key=api_key,
#                     library_id=library_id,
#                     library_type=library_type,
#                     collection_id=collection_id,
#                     clear_collection=write_note_params.clear_collection
#                 )
#                 return result

#             total = 0
#             for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
#                 lib = ZoteroLibrary(lib_cfg)
#                 if (
#                     (not graph or graph == lib.base_url) and
#                     lib.sync.get("api_key") and
#                     lib.sync.get("library_id") and
#                     lib.sync.get("library_type")
#                 ):
#                     rdf_to_znotes(
#                         blocks=blocks,
#                         api_key=lib.sync["api_key"],
#                         library_id=lib.sync["library_id"],
#                         library_type=lib.sync["library_type"],
#                         collection_id=lib.sync.get("collection_id"),
#                         clear_collection=write_note_params.clear_collection
#                     )
#                     total += len(blocks)

#             if total == 0:
#                 raise HTTPException(status_code=404, detail="No Zotero Notes created")
#             return {"success": f"{total} Zotero Notes created"}

#         else:
#             raise HTTPException(status_code=400, detail=f"Unknown task: {task}")

#     except Exception as e:
#         logger.exception("Zotero RDF sync error")
#         raise HTTPException(status_code=500, detail=str(e))

@plugin_router.get("/parse_notes", summary="Parse notes", description="Triggers the parsing of all Zotero notes with semantic-html plugin", tags=["RDF", "Plugins"])
async def parse_notes(
    delete: bool = Query(default=False, description="Delete all existing triples related to parsed note"),
    graph: str | None = Query(default=None, description="Named graph IRI containing the items/notes (optional)"),
    note_predicate: str | None  = Query(default=f"{ZOT_NS}note", description="predicate for note HTML"),
    query: str | None = Query(default=None, description="Query to retrieve notes, requires ?s ?p ?o as bindings (optional)"),
    push: bool | None = Query(default=True, description="Push triples to store (true by default)")
    ):

    from .store import store

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
            result=parse_all_notes(lib, store, note_predicate=predicate, query_str=query, delete=delete,push=push) # TODO no graph given
        elif graph and graph != lib.base_url:
            logger.debug(f"{graph} skipped")
        else:
            logger.warning(f"{graph} not yet supported but defined via config")
    return {"success":f"{result} notes parsed"}

@plugin_router.get("/taxonomy", summary="Parses taxonomy between Knowledge Base and Zotero", description="Creates a structured HTML from RDF taxonomies, and parses structured HTML back as RDF", tags=["RDF", "Plugins"])
async def taxonomy(
    graph: str | None = Query(default=None, description="Named graph IRI (optional) to read RDF resources from. Will use this named graph to detect Zotero library sync configuration to write to collection if no config parameters are given to the endpoint."),
    task: TypeLiteral["writeNote", "writeStore"] = Query(default="writeNote", description="Either 'writeNote' to write from Store into HTML/note target or 'writeStore' to read from HTML/note to target Store"),
    html_file: bool = Query(default=False, description=f"Stores to HTML file (instead of Zotero note) or reads from HTML file (instead of from Zotero note). Depends on task. File read from {IMPORT_DIRECTORY} and saved to {EXPORT_DIRECTORY}. Graph name must be given and will be used for file name, all library configs will be ignored. Provide a mapping or rely on defaults"),
    mapping: str = Query(default=None, description="Configuration mapping (dict or file path), loads from library config by default"),
    api_key: str | None = Query(default=None, description="Zotero API key (overrides config)"),
    library_id: str | None = Query(default=None, description="Zotero Library ID (overrides config)"),
    library_type: str | None = Query(default=None, description="Zotero Library type (user or group, overrides config)"),
    note_key: str | None = Query(default=None, description="Zotero note key, creates new if none (overrides config)")
):
    from zotero_rdf_server.plugins.rdf_notes.rdf_znotes import pipeline
    checked_graph, all_graphs = get_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI: {checked_graph}. Use one of these or None: {all_graphs}")
    from .store import store

    res = []
    if html_file:
        if graph:        
            filename_base = iri_to_filename(graph)       
            file = f"{filename_base}.html"
            cfg = {'graph':graph, 'mapping':mapping}
            res.append({"success": pipeline(lib=cfg, source_store=store, job=task, note_key=note_key, file=file)})
        else:
            res.append({"error": "graph missing"})
    else:    
        if api_key and library_id and library_type:
            lib = ZoteroLibrary({'sync':{'api_key':api_key,'library_id':library_id,'library_type':library_type}}, False)
            if mapping:
                lib.taxonomy["mapping"] = mapping
            res.append({"success": pipeline(lib=lib, source_store=store, job=task, note_key=note_key, file=None)})
        else:
            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)
                logger.info(f"Checking config for library: {lib.name}")
                if (
                    (not graph or graph == lib.base_url) and
                    lib.sync and
                    lib.sync.get("api_key") and
                    lib.sync.get("library_id") and
                    lib.sync.get("library_type") and
                    lib.taxonomy
                ):
                    if mapping:
                        lib.taxonomy["mapping"] = mapping
                    res.append({"success": pipeline(lib=lib, source_store=store, job=task, note_key=note_key, file=None)})
    return res

###LOGS###

@router.get("/logs", response_class=HTMLResponse)
def logs_page():
    try:
        import html
        with open("app.log", "r") as f:
            log_content = html.escape(f.read())
    except FileNotFoundError:
        log_content = "Log file not found."

    html_page = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Log Viewer</title>
        <style>
            body {{
                font-family: monospace;
                background: #111;
                color: #eee;
                padding: 20px;
            }}
            #log {{
                background: #222;
                border: 1px solid #444;
                border-radius: 8px;
                padding: 12px;
                white-space: pre-wrap;
                overflow-y: auto;
                max-height: 80vh;
                font-size: 13px;
                line-height: 1.4em;
            }}
            button {{
                margin-right: 10px;
                padding: 6px 12px;
                font-size: 13px;
                background: #333;
                border: 1px solid #666;
                color: #eee;
                border-radius: 4px;
                cursor: pointer;
            }}
            .button-bar {{
                margin-bottom: 10px;
            }}
        </style>
    </head>
    <body>
        <h2>Log Viewer</h2>
        <div class="button-bar">
            <form method="get" action="/logs" style="display:inline;">
                <button type="submit">⟳ Refresh</button>
            </form>
            <form method="post" action="/logs/clear" style="display:inline;">
                <button type="submit">🗑 Clear Log</button>
            </form>
        </div>
        <div id="log">{log_content}</div>

        <script>
            const logDiv = document.getElementById("log");
            logDiv.scrollTop = logDiv.scrollHeight;
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_page)


@router.post("/logs/clear")
def clear_log_file():
    try:
        with open("app.log", "w") as f:
            f.write("")  # Logdatei leeren
    except Exception as e:
        return HTMLResponse(content=f"Error clearing log file: {e}", status_code=500)

    return RedirectResponse(url="/logs", status_code=303)