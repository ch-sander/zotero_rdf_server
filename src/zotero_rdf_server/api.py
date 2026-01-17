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
import pkgutil
from importlib import import_module
from importlib.util import find_spec


router = APIRouter()

def include_plugins(app: FastAPI, plugins_pkg: str = "zotero_rdf_server.plugins", base_prefix: str = "/plugin") -> None:
    pkg = import_module(plugins_pkg)

    for m in pkgutil.iter_modules(pkg.__path__, prefix=pkg.__name__ + "."):
        if not m.ispkg:
            continue

        plugin_name = m.name.rsplit(".", 1)[-1]
        endpoints_mod = f"{m.name}.endpoints"

        if find_spec(endpoints_mod) is None:
            continue

        try:
            mod = import_module(endpoints_mod)
            prouter = getattr(mod, "router", None)
            if prouter is None:
                logger.warning("Plugin %s has endpoints.py but no `router`", plugin_name)
                continue

            prefix = getattr(mod, "PLUGIN_PREFIX", f"{base_prefix}/{plugin_name}")
            app.include_router(prouter, prefix=prefix)

            logger.info("Loaded plugin %s at %s", plugin_name, prefix)
        except Exception:
            logger.exception("Failed to load plugin %s (%s)", plugin_name, endpoints_mod)



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