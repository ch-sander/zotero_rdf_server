from fastapi import Query, HTTPException, APIRouter, Depends
from typing import Literal as TypeLiteral
from zotero_rdf_server.store import *
from zotero_rdf_server.logging_config import logger
from zotero_rdf_server.config import *
from zotero_rdf_server.models import ZoteroLibrary
from zotero_rdf_server.utils import *
from zotero_rdf_server.api import require_writable

router = APIRouter(tags=["Notes RDF Interface"])
open_router = APIRouter()

@router.get(
    "/znotes2rdf",
    summary="Zotero Notes to RDF",
    description="Writes all RDF blocks in Zotero Notes HTML into RDF, either Store or file.",
    tags=["RDF"],
    dependencies=[Depends(require_writable)]
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
    # from zotero_rdf_server.plugins.rdf_notes.rdf_znotes import znotes_to_rdf
    znotes_to_rdf = require_symbol(
                                "zotero_rdf_server.plugins.rdf_notes.rdf_znotes",
                                "znotes_to_rdf",
                                hint="Enable/install the 'znotes_to_rdf' plugin (and its dependencies).",
                                )
    from zotero_rdf_server.store import store

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

@router.get("/rdf2znotes", summary="RDF to Zotero Notes", description="Creates Zotero Notes with RDF dump block from Store or RDF dataset", tags=["RDF"])
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
    from zotero_rdf_server.store import store
    
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
    

@router.get("/taxonomy", summary="Parses taxonomy between Knowledge Base and Zotero", description="Creates a structured HTML from RDF taxonomies, and parses structured HTML back as RDF", tags=["RDF", "Semantics"])
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
    from zotero_rdf_server.store import store

    if task == "writeStore":
        require_writable()
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
            tax_cfg = lib.plugin.get("taxonomy") or {}
            if mapping:
                tax_cfg["mapping"] = mapping
            res.append({"success": pipeline(lib=lib, source_store=store, job=task, note_key=note_key, file=None)})
        else:
            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)
                logger.info(f"Checking config for library: {lib.name}")
                tax_cfg = lib.plugin.get("taxonomy") or {}
                if (
                    (not graph or graph == lib.base_url) and
                    lib.sync and
                    lib.sync.get("api_key") and
                    lib.sync.get("library_id") and
                    lib.sync.get("library_type") and
                    tax_cfg
                ):
                    if mapping:
                        tax_cfg["mapping"] = mapping
                    res.append({"success": pipeline(lib=lib, source_store=store, job=task, note_key=note_key, file=None)})
    return res












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