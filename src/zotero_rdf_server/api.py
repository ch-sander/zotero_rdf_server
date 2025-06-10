from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
import logging
from pathlib import Path
import asyncio
from .store import *
from .rdf import *
from .logging_config import logger, LogLevel
from .config import *
from .models import ZoteroLibrary
from .utils import *

router = APIRouter()

@router.get("/export", summary="Create export", description=f"Exports the store or a named graph to {EXPORT_DIRECTORY}", tags=["data"])
async def export_graph(
    format: str = Query("trig"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional)")
):
    from .store import store
    checked_graph, all_graphs = get_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")

    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)

    rdf_format = RdfFormat.from_extension(format.lower())
    if rdf_format is None:
        raise ValueError(f"Unsupported RDF format: {format}")

    extension = rdf_format.file_extension
    filename_base = iri_to_filename(graph) if graph else "zotero_store"
    logger.info(filename_base)
    path = os.path.join(EXPORT_DIRECTORY, f"{filename_base}.{extension}")

    kwargs = {}
    if graph:
        kwargs["from_graph"] = checked_graph
        logger.info(f"Export from graph: {checked_graph}")
    elif not rdf_format.supports_datasets:        
        kwargs["from_graph"] = DefaultGraph()
        logger.info("Export from DefaultGraph")
    else:
        logger.info(f"Export from graphs: {list(store.named_graphs())}")
        
    print(f"Checked graph: {checked_graph!r}")
    print(f"Kwargs: {kwargs}")
    print(f"Graph triples: {len(list(store.quads_for_pattern(None,None,None,checked_graph)))}")

    store.dump(output=path, format=rdf_format, prefixes=PREFIXES, from_graph=checked_graph)
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
async def get_graphs():
    from .store import store
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}



@router.get("/csv", summary="Export CSV", description="Exports a named graph or the entire store as CSV or loads a CSV as RDF into the store", tags=["RDF"])
async def get_csv(
    graph: str | None = Query(default=None, description="Named graph IRI (optional)"),
    load_csv: str | None = Query(default=None, description="Load a CSV file into the store"),
    delete: bool | None = Query(default=False, description="Removes triples from graph if true, done before loading triples (you may only use subject IRIs to just delete)")
    ):
    from collections import defaultdict
    import csv

    graph_uri = safeNamedNode(graph) if graph else None
    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)
    output_file = os.path.join(EXPORT_DIRECTORY, f"export.csv")
    delimiter = " | "

    from .store import store

    graphs = [str(g) for g in store.named_graphs()]
    graph = f"<{graph.strip().strip('<>').strip()}>" if graph else None
    if graph and graph not in graphs:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {graphs}")
    # subject → { predicate → [objects...] }
    # NamedNodes as objects are wrapped in <> for both export and import
    records = defaultdict(lambda: defaultdict(list))
    all_predicates = set()
    for quad in store.quads_for_pattern(None, None, None, graph_uri):
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

    if load_csv and os.path.exists(load_csv) and load_csv is not output_file:
        if delete:
            subjects = set()
            with open(load_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    subj_iri = row["IRI"].strip()
                    if subj_iri:
                        subjects.add(safeNamedNode(subj_iri))
            for subj in subjects:
                for quad in store.quads_for_pattern(subj, None, None, graph_uri):
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
                            quad = Quad(subj, predicate, obj, graph_uri)
                            store.add(quad)
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}

### PLUGINS ###

@router.get(
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
    from zotero_rdf_server.plugins.rdf_znotes import znotes_to_rdf
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
                    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)
                    rdf_format = RdfFormat.from_extension(output_format.lower())
                    if rdf_format is None:
                        raise ValueError(f"Unsupported RDF format: {output_format}")

                    extension = rdf_format.file_extension
                    filename_base = iri_to_filename(target_graph) if target_graph else "zotero_notes"
                    path = os.path.join(EXPORT_DIRECTORY, f"{filename_base}.{extension}")
                    input.dump(output=path, format=rdf_format, prefixes=PREFIXES, from_graph = target_graph)

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

@router.get("/rdf2znotes", summary="RDF to Zotero Notes", description="Creates Zotero Notes with RDF dump block from Store or RDF dataset", tags=["RDF", "Plugins"])
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
    
    from zotero_rdf_server.plugins.rdf_znotes import describe_resources, rdf_to_znotes
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
        )[:22]
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

@router.get("/parse_notes", summary="Parse notes", description="Triggers the parsing of all Zotero notes with semantic-html plugin", tags=["RDF", "Plugins"])
async def parse_notes(
    delete: bool = Query(default=False, description="Delete all existing triples related to parsed note"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional)"),
    note_predicate: str | None  = Query(default=f"{ZOT_NS}note", description="predicate for note HTML"),
    query: str | None = Query(default=None, description="Query to retrieve notes, requires ?s ?p ?o as bindings (optional)"),
    push: bool | None = Query(default=True, description="Push triples to store (optional)")
    ):

    from .store import store

    checked_graph, all_graphs = checked_graph(graph)
    if graph and not checked_graph:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")
    
    if not note_predicate:
        predicate = safeNamedNode(f"{ZOT_NS}note")
    else:
        predicate = safeNamedNode(f"{note_predicate}")

    for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
        lib = ZoteroLibrary(lib_cfg)
        if not graph or graph.value == lib.base_url:
            result=parse_all_notes(lib, store, note_predicate=predicate, query_str=query, delete=delete,push=push)
    return {"success":f"{result} notes parsed"}

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

    # Nach dem Löschen zurück zur Viewer-Seite
    return RedirectResponse(url="/logs", status_code=303)