from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter, Depends, status, Body
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets, shutil
from typing import Any, Literal as TypingLiteral
import logging
from pathlib import Path
# import asyncio
# from .global_store import *
from . import global_store
from .rdf import *
from .logging_config import logger, LogLevel
from .config import *
from .models import ZoteroLibrary
from .utils import *
import pkgutil
from importlib import import_module
from importlib.util import find_spec

security = HTTPBasic()

def verify(credentials: HTTPBasicCredentials = Depends(security)):
    if not API_USER or not API_PASSWORD:
        logger.warning("No password set!")
        return None

    correct_username = secrets.compare_digest(credentials.username, API_USER)
    correct_password = secrets.compare_digest(credentials.password, API_PASSWORD)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


protected_dependencies = (
    [Depends(verify)] if API_USER and API_PASSWORD else []
)

router = APIRouter(dependencies=protected_dependencies)
open_router = APIRouter()

def require_writable():
    if STORE_MODE == "directory_ro":
        raise HTTPException(
            status_code=503,
            detail="Store is read-only in this service"
        )
    
def include_plugins(app: FastAPI, plugins_pkg: str = "zotero_rdf_server.plugins", base_prefix: str = "/plugin") -> None:
    from .config import INCLUDE_CLOSED_ROUTER, INCLUDE_OPEN_ROUTER
    pkg = import_module(plugins_pkg)

    for m in pkgutil.iter_modules(pkg.__path__, prefix=pkg.__name__ + "."):
        if not m.ispkg:
            continue

        plugin_name = m.name.rsplit(".", 1)[-1]
        display_name = plugin_name.replace("_", " ").title()
        endpoints_mod = f"{m.name}.endpoints"

        if find_spec(endpoints_mod) is None:
            continue

        try:
            mod = import_module(endpoints_mod)
            prouter = getattr(mod, "router", None)
            open_router = getattr(mod, "open_router", None)

            if prouter is None and open_router is None:
                logger.warning("Plugin %s has endpoints.py but no routers", plugin_name)
                continue

            prefix = getattr(mod, "PLUGIN_PREFIX", f"{base_prefix}/{plugin_name}")
            if prouter is not None and INCLUDE_CLOSED_ROUTER:
                app.include_router(prouter, prefix=prefix,
                                    tags=["Plugin", display_name], dependencies=protected_dependencies)
                
            if open_router is not None and INCLUDE_OPEN_ROUTER:
                app.include_router(open_router, prefix=prefix,
                                    tags=["Plugin", display_name, "Open Endpoint"])

            logger.info("Loaded plugin %s at %s", plugin_name, prefix)
        except Exception:
            logger.exception("Failed to load plugin %s (%s)", plugin_name, endpoints_mod)

from io import BytesIO

def build_subset_store(source_store, graphs):
    temp_store = Store()
    for graph in graphs:
        temp_store.bulk_extend(source_store.quads_for_pattern(None, None, None, graph))
    return temp_store

def create_zip(rdf_data: bytes, rdf_filename: str) -> bytes:
    from zipfile import ZIP_DEFLATED, ZipFile
    buffer = BytesIO()

    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(rdf_filename, rdf_data)

    return buffer.getvalue()

def prepare_export(format: str, graph: list[str] | None, ts_filename: bool = False):
    # from .global_store import store
    store = global_store.get_store()
    rdf_format = ensure_rdf_format(format=format)
    if rdf_format is None:
        raise HTTPException(status_code=400, detail=f"Unsupported RDF format: {format}")

    checked_graphs = []
    all_graphs = None

    if graph:
        for g in graph:
            checked_graph, all_graphs = global_store.get_graph(g)
            if not checked_graph:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid graph IRI: {g}. Use one of these or None: {all_graphs}"
                )
            checked_graphs.append(checked_graph)

    extension = rdf_format.file_extension
    if len(checked_graphs) == 1:
        filename_base = iri_to_filename(graph[0])
    else:
        filename_base = "zotero_store" if not checked_graphs else "graph_subset"

    filename = default_filename(filename_base, extension) if ts_filename else f"{filename_base}.{extension}"
    path = EXPORT_DIRECTORY / filename
    return store, rdf_format, checked_graphs, path


def dump_export(store, rdf_format, checked_graphs, output=None):
    data = None

    if not checked_graphs:
        len_store = len(store)
        if rdf_format.supports_datasets:
            data = store.dump(output=output, format=rdf_format, prefixes=PREFIXES)
        else:
            data = store.dump(
                output=output,
                format=rdf_format,
                prefixes=PREFIXES,
                from_graph=DefaultGraph(),
            )
    elif len(checked_graphs) == 1:
        len_store = len(store)
        g = checked_graphs[0]
        data = store.dump(
            output=output,
            format=rdf_format,
            prefixes=PREFIXES,
            from_graph=g,
            base_iri=str(g.value).rstrip("/") + "/" if getattr(g, "value", None) else None,
        )
    else:
        subset_store = build_subset_store(store, checked_graphs)
        data = subset_store.dump(output=output, format=rdf_format, prefixes=PREFIXES)
        len_store = len(subset_store)

    return len_store, data


@open_router.get("/export", summary="Create export", tags=["Data"])
async def export_graph(
    format: str = Query("trig"),
    graph: list[str] | None = Query(default=None, description="Named graph IRIs (repeat parameter)"),
    as_zip: bool = Query(
        default=False,
        alias="zip",
        description="Package RDF export as ZIP",
    ),
):
    store, rdf_format, checked_graphs, path = prepare_export(format, graph, True)

    len_store, data = dump_export(store, rdf_format, checked_graphs)

    if as_zip:
        data = create_zip(data, path.name)
        path = path.with_suffix(".zip")
        media_type = "application/zip"
    else:
        media_type = getattr(
            rdf_format,
            "media_type",
            "application/octet-stream",
        )

    return StreamingResponse(
        BytesIO(data),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "X-Export-Length": str(len_store),
        },
    )


@router.get("/export/file", summary="Create export file", tags=["Data"])
async def export_graph_file(
    format: str = Query("trig"),
    graph: list[str] | None = Query(default=None, description="Named graph IRIs (repeat parameter)"),
    timestamp: bool | None = Query(default=False, description="Add Timestamp to filename"),
    as_gzip: bool = Query(
        default=False,
        alias="gzip",
        description="Compress RDF export using Gzip",
    )
):
    store, rdf_format, checked_graphs, path = prepare_export(format, graph, timestamp)

    # EXPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)

    if as_gzip:
        import gzip
        path = path.with_suffix(path.suffix + ".gz")

        with gzip.open(path, mode="wb") as output:
            len_store, _ = dump_export(
                store,
                rdf_format,
                checked_graphs,
                output=output,
            )
    else:
        logger.info(f"Create {path}")
        logger.info(f"Checked graphs: {checked_graphs!r}")

        len_store, _ = dump_export(
            store,
            rdf_format,
            checked_graphs,
            path,
        )

    return {
        "success": (
            f"Exported "
            f"{[str(g) for g in checked_graphs] or [str(g) for g in store.named_graphs()]}"
        ),
        "len": len_store,
        "path": path,
    }

@router.get(
    "/backup",
    summary="Create backup",
    description=f"Creates a complete backup of the store to {BACKUP_DIRECTORY}",
    tags=["Data"],
)
async def backup_store(
    timestamp: bool = Query(
        default=False,
        description="Add timestamp to backup directory name",
    )
):
    store = global_store.get_store()
    backup_root = Path(BACKUP_DIRECTORY).resolve()

    backup_name = (
        f"Store_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if timestamp
        else "Store"
    )
    backup_path = backup_root / backup_name

    log_file = backup_root / "backup.log"

    try:
        store_path = Path(STORE_DIRECTORY).resolve()
    except AttributeError:
        return {"error": f"The current store was not found in {STORE_DIRECTORY} (maybe in-memory DB?)"}

    if backup_path == store_path or backup_path in store_path.parents:
        raise RuntimeError("Cannot backup into the current store's own directory")

    if backup_path.exists():
        shutil.rmtree(backup_path, ignore_errors=True)
        log_file.write_text(
            f"[{datetime.now().isoformat()}] Deleted old Store backup\n",
            encoding="utf-8",
        )

    store.backup(str(backup_path))

    backup_store = Store(str(backup_path))
    graphs = [str(g) for g in backup_store.named_graphs()]

    with log_file.open("a", encoding="utf-8") as f:
        f.write(
            f"[{datetime.now().isoformat()}] Created new backup in {backup_path}\n"
        )

    return {
        "status": "success",
        "backup store": {
            "path": backup_path,
            "named_graphs": graphs,
            "len": len(backup_store),
        },
    }

@router.get("/reload", summary="Reload app", description="Will trigger a reload, even if not set in config.", tags=["Data"])
async def reload(
    reload_libraries: bool = Query(
        default=True,
        description="Reload libraries/graphs into the store from (re)loaded configuration",
    ),
    logging_level: LogLevel = Query(
        default=log_level,
        description="Temporarily sets log level during reload",
    ),
    remove_store: bool = Query(
        default=True,
        description="Clears data directory / store before loading",
    ),
    reload_config: bool = Query(
        default=True,
        description="Reload the entire configuration (re-read env/config and reinit globals)",
    ),
):
    current_level = logger.level
    if logging_level:
        new_level = getattr(logging, logging_level.upper(), None)
        if not isinstance(new_level, int):
            return {"error": f"Invalid log level: {logging_level}"}
        logger.setLevel(new_level)

    try:
        # if reload_config: # TODO
        #     from . import config as config_module
        #     importlib.reload(config_module)
        #     logger.warning("CONFIG reloaded!")

        global_store.refresh_store(reload_libraries, remove_store=remove_store)

        # from .global_store import store
        store = global_store.get_store()
        graphs = [str(g) for g in store.named_graphs()]
        return {
            "status": "success",
            "reloaded": {
                "config": bool(reload_config),
                "libraries": bool(reload_libraries),
                "store_cleared": bool(remove_store),
            },
            "store": {"named_graphs": graphs, "len": len(store)},
        }

    finally:
        logger.setLevel(current_level)

@router.get("/optimize", summary="Optimize Store", description="Will optimize the oxigraph store", tags=["Data"])
async def optimize_store():
    with global_store._store_lock:
        store = global_store.get_store()
        store.optimize()

    return {"success": "Store optimized"}

@open_router.get("/favicon.ico", include_in_schema=False)
def favicon():
    try:
        FAVICON = STATIC_UI_DIRECTORY / "favicon.ico"

        if not FAVICON.exists():
            ensure_import("pillow==12.1.1")
            from PIL import Image, ImageDraw
            import random

            orange = (0xFF, 0x99, 0x00)  # #ff9900
            blue = (0x4A, 0x86, 0xE8)    # #4a86e8

            size = 64
            block = 8
            cells = size // block

            base, accent = random.choice([(orange, blue), (blue, orange)])

            img = Image.new("RGB", (size, size), base)
            draw = ImageDraw.Draw(img)

            def fill_cell(cx, cy, color):
                x0 = cx * block
                y0 = cy * block
                x1 = x0 + block - 1
                y1 = y0 + block - 1
                draw.rectangle([x0, y0, x1, y1], fill=color)

            motif = random.choice([
                "checker",
                "diagonal",
                "cross",
                "frame",
                "quadrants",
                "rings",
            ])

            if motif == "checker":
                offset = random.randint(0, 1)
                for y in range(cells):
                    for x in range(cells):
                        if (x + y + offset) % 2 == 0:
                            fill_cell(x, y, accent)

            elif motif == "diagonal":
                width = random.choice([1, 2])
                direction = random.choice([1, -1])
                for y in range(cells):
                    for x in range(cells):
                        d = (x - y) if direction == 1 else (x + y - (cells - 1))
                        if abs(d) <= width or abs(d) % 4 == 0:
                            fill_cell(x, y, accent)

            elif motif == "cross":
                thickness = random.choice([1, 2])
                mid = cells // 2
                for y in range(cells):
                    for x in range(cells):
                        if abs(x - mid) < thickness or abs(y - mid) < thickness:
                            fill_cell(x, y, accent)

            elif motif == "frame":
                thickness = random.choice([1, 2])
                for y in range(cells):
                    for x in range(cells):
                        if (
                            x < thickness
                            or y < thickness
                            or x >= cells - thickness
                            or y >= cells - thickness
                        ):
                            fill_cell(x, y, accent)

                inner = random.choice([True, False])
                if inner:
                    c0 = cells // 2 - 1
                    for y in range(c0, c0 + 2):
                        for x in range(c0, c0 + 2):
                            fill_cell(x, y, accent)

            elif motif == "quadrants":
                chosen = random.choice([
                    {(0, 0), (1, 1)},
                    {(1, 0), (0, 1)},
                    {(0, 0), (1, 0)},
                    {(0, 1), (1, 1)},
                ])
                half = cells // 2
                for qx, qy in chosen:
                    for y in range(qy * half, (qy + 1) * half):
                        for x in range(qx * half, (qx + 1) * half):
                            fill_cell(x, y, accent)

            elif motif == "rings":
                rings = random.choice([1, 2, 3])
                for r in range(rings):
                    for i in range(r, cells - r):
                        fill_cell(i, r, accent)
                        fill_cell(i, cells - 1 - r, accent)
                        fill_cell(r, i, accent)
                        fill_cell(cells - 1 - r, i, accent)
                    accent, base = base, accent

            buffer = BytesIO()
            img.save(buffer, format="ICO")

            return Response(
                content=buffer.getvalue(),
                media_type="image/x-icon",
                headers={"Cache-Control": "no-store"}
            )

        return FileResponse(FAVICON)

    except Exception as e:
        logger.error(e)
        return Response(status_code=204)

@router.get("/libs", summary="List of all libraries", description="Returns all available libraries with configuration.", tags=["Admin"])
async def get_libs():
    import importlib
    from . import config
    importlib.reload(config)
    result = [ZoteroLibrary(cfg) for cfg in config.ZOTERO_LIBRARIES_CONFIGS]
    return {"success": result}

@open_router.get("/graphs", summary="List of all named graphs", description="Returns all available named graphs.", tags=["RDF"])
async def list_graphs():
    # from .global_store import store
    store = global_store.get_store()
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}

@router.post(
    "/sparql_update",
    summary="Run a SPARQL UPDATE against the RDF store",
    description=(
        "Executes a SPARQL UPDATE against the global RDF store.\n\n"
        "The request body must be plain text. It may contain either a SPARQL UPDATE query "
        "directly or a path that `load_text_like()` can resolve."
    ),
    tags=["RDF", "Proxy"],
    dependencies=[Depends(require_writable)],
)
async def sparql_update(
    update_query: str = Body(
        ...,
        min_length=1,
        max_length=20_000,
        examples=None,
        media_type="text/plain",
        description="SPARQL UPDATE query or path to a SPARQL UPDATE file.",
    ),
) -> dict:
    store = global_store.get_store()

    try:
        update = load_text_like(update_query, label="SPARQL UPDATE query")
    except Exception as e:
        logger.error("Could not load SPARQL UPDATE query: %s", e, exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=f"Could not load SPARQL UPDATE query: {e}",
        )

    try:
        with global_store._store_lock:
            before = len(store)

            logger.warning(
                "Running SPARQL UPDATE; store size before=%s",
                before,
            )
            logger.info(update)
            store.update(update=update)

            after = len(store)

            logger.warning(
                "Finished SPARQL UPDATE; store size before=%s; after=%s; delta=%+d",
                before,
                after,
                after - before,
            )

    except Exception as e:
        logger.error(
            "SPARQL UPDATE failed: %s\n\n%s",
            e,
            update,
            exc_info=True,
        )
        raise HTTPException(
            status_code=400,
            detail=f"SPARQL UPDATE failed: {e}",
        )

    return {
        "success": True,
        "store_size_before": before,
        "store_size_after": after,
        "delta": after - before,
    }

@router.delete(
    "/mapping-targets",
    summary="Delete all target statements from a mapping graph",
    description=(
        "Finds all statements of the form `?entry zmap:target ?entity` in the selected mapping graph.\n\n"
        "Only the target statements are removed. The mapping entries and their remaining statements "
        "are preserved.\n\n"
        "By default, the endpoint performs a dry run. Set `execute=true` to remove the statements."
    ),
    tags=["RDF", "Maintenance"],
    dependencies=[Depends(require_writable)],
)
async def delete_mapping_targets(
    mapping_graph_iri: str = Query(
        ...,
        description="Named graph IRI from which mapping target statements are to be removed.",
    ),
    execute: bool = Query(
        default=False,
        description=(
            "If false, return the number of matching statements without modifying the store. "
            "If true, remove all matching target statements."
        ),
    ),
) -> dict[str, Any]:  
    store = global_store.get_store()

    map_graph, all_graphs = global_store.get_graph(mapping_graph_iri)
    if mapping_graph_iri is not None and not map_graph:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}",
        )
    target_quads = list(store.quads_for_pattern(None, MAP_TARGET_NODE, None, graph_name=map_graph))
    count = len(target_quads)

    if not execute:
        return {
            "execute": False,
            "map_graph": str(map_graph),
            "would_remove": count,
            "predicate": str(MAP_TARGET_NODE),
        }

    for q in target_quads:
        with global_store._store_lock:
            store.remove(q)

    return {
        "execute": True,
        "map_graph": str(map_graph),
        "removed": count,
        "predicate": str(MAP_TARGET_NODE),
    }

@router.delete(
    "/purge",
    summary="Find and optionally delete orphan entities or dangling mappings",
    description=(
        "Finds inconsistent knowledge-base entities or mapping entries for one or more configured "
        "libraries.\n\n"
        "Entity mode (`resource_type=entities`):\n"
        "- An entity is a candidate when it is not referenced as a mapping target.\n"
        "- When `require_unreferenced=true`, the entity must also not occur as an object in the "
        "configured graphs.\n"
        "- Entities with an outgoing `owl:sameAs` statement can optionally be preserved.\n\n"
        "Mapping mode (`resource_type=mappings`):\n"
        "- A mapping entry can be selected when it has no target.\n"
        "- A mapping entry can also be selected when its target has no subject facts in the "
        "knowledge-base graph.\n\n"
        "By default, the endpoint performs a dry run. Set `execute=true` to delete the selected resources."
    ),
    tags=["RDF", "Maintenance"],
    dependencies=[Depends(require_writable)],
)
async def purge(
    library_iri: str | None = Query(
        default=None,
        description=(
            "Optional configured library base IRI. If provided, process only that library. "
            "If omitted, process all configured libraries."
        ),
    ),
    resource_type: TypingLiteral["entities", "mappings"] = Query(
        default="entities",
        description="Type of resource to inspect and optionally delete.",
    ),
    execute: bool = Query(
        default=False,
        description=(
            "If false, return the resources that would be deleted. "
            "If true, delete those resources from their respective graphs."
        ),
    ),
    require_unreferenced: bool = Query(
        default=False,
        description=(
            "Entity mode only. If true, select an unmapped entity only when it is also not "
            "referenced as an object in the configured graphs."
        ),
    ),
    preserve_sameas_subjects: bool = Query(
        default=False,
        description=(
            "Entity mode only. If true, preserve entities that have an outgoing `owl:sameAs` "
            "statement in the knowledge-base graph."
        ),
    ),
    include_mappings_without_target: bool = Query(
        default=True,
        description=(
            "Mapping mode only. Select mapping entries that do not contain a target statement."
        ),
    ),
    include_mappings_with_missing_target_entity: bool = Query(
        default=False,
        description=(
            "Mapping mode only. Select mapping entries whose target has no subject facts in the "
            "knowledge-base graph."
        ),
    ),
) -> list[dict[str, Any]]:
    from .global_store import get_graph
    store = global_store.get_store()
    checked_graph, all_graphs = get_graph(library_iri)
    if library_iri is not None and not checked_graph:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}",
        )

    results: list = []

    for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
        lib = ZoteroLibrary(lib_cfg)

        if library_iri is not None and library_iri != lib.base_uri:
            logger.debug("Skipping %s (filtered by graph_iri=%s)", lib.base_uri, library_iri)
            continue

        logger.info("Purging for %s (mode=%s, delete=%s)...", lib.base_uri, resource_type, execute)

        entity_graph = safeNamedNode(lib.knowledge_base_graph)
        map_graph = safeNamedNode(lib.mapping_base_graph)

        if resource_type == "entities":
            graphs_to_check = [
                safeNamedNode(lib.knowledge_base_graph),
                safeNamedNode(lib.base_uri),
            ]

            results.append(
                purge_orphan_entities(
                    store,
                    entity_graph=entity_graph,
                    map_graph=map_graph,
                    graphs_to_check_for_objects=graphs_to_check,
                    delete=execute,
                    not_mapped_only=not require_unreferenced,
                    keep_if_sameas_subject=preserve_sameas_subjects,
                )
            )

        elif resource_type == "mappings":
            results.append(
                purge_dangling_mappings(
                    store,
                    entity_graph=entity_graph,
                    map_graph=map_graph,
                    delete=execute,
                    delete_if_missing_target=include_mappings_without_target,
                    delete_if_target_not_in_kb=include_mappings_with_missing_target_entity,
                )
            )

    return results

@router.post(
    "/merge",
    summary="Merge or replace an entity and update all references",
    description=(
        "Updates references from `old_iri` to `new_iri` in the knowledge base and mapping graphs.\n\n"
        "By default, outgoing facts of the old entity are copied to the new entity, all incoming "
        "references are rewritten, mapping entries are retargeted, and the old entity is removed.\n\n"
        "Set `delete_old_only=true` to discard the outgoing facts of the old entity instead of copying "
        "them. Incoming references and mapping entries are still rewritten to the new entity.\n\n"
        "Set `only_redirect=true` to keep the old entity and its outgoing facts. In this mode, references "
        "and mappings are still rewritten and a `new owl:sameAs old` triple is added.\n\n"
        "`only_redirect` and `delete_old_only` are mutually exclusive."
    ),
    tags=["RDF", "Semantics"],
    dependencies=[Depends(require_writable)],
)
async def merge(
    old_iri: str = Query(
        ...,
        description=(
            "IRI of the old entity. References to this entity will be rewritten to `new_iri`."
        ),
    ),
    new_iri: str = Query(
        ...,
        description=(
            "IRI of the target entity that will replace or absorb the old entity."
        ),
    ),
    kb_graph_iri: str = Query(
        ...,
        description=(
            "Named graph IRI containing the knowledge-base facts of both entities."
        ),
    ),
    map_graph_iri: str = Query(
        ...,
        description=(
            "Named graph IRI containing mapping entries that may reference the old entity."
        ),
    ),
    only_redirect: bool = Query(
        default=False,
        description=(
            "Keep the old entity and its outgoing knowledge-base facts. Rewrite incoming references "
            "and mappings to the new entity and add `new owl:sameAs old`. Cannot be combined with "
            "`delete_old_only`."
        ),
    ),
    delete_old_only: bool = Query(
        default=False,
        description=(
            "Delete the outgoing knowledge-base facts of the old entity without copying them to the "
            "new entity. Incoming references and mapping entries are still rewritten to the new entity. "
            "Cannot be combined with `only_redirect`."
        ),
    ),
    dedup_mapping: bool = Query(
        default=False,
        description=(
            "After retargeting mapping entries, merge or remove duplicate mappings that target the "
            "new entity."
        ),
    ),
) -> dict:
    from .global_store import get_graph

    store = global_store.get_store()

    old = safeNamedNode(old_iri)
    new = safeNamedNode(new_iri)

    if old == new:
        raise HTTPException(
            status_code=400,
            detail="old_iri and new_iri must be different.",
        )

    if only_redirect and delete_old_only:
        raise HTTPException(
            status_code=400,
            detail="only_redirect and delete_old_only cannot both be true.",
        )

    checked_graph_map, _ = get_graph(map_graph_iri)
    checked_graph_kb, _ = get_graph(kb_graph_iri)

    if not checked_graph_map or not checked_graph_kb:
        raise HTTPException(
            status_code=400,
            detail="Knowledge base or mapping graph not found or empty.",
        )

    def has_subject_in_graph(
        store: Store,
        subject: NamedNode,
        graph: NamedNode,
    ) -> bool:
        return any(
            True
            for _ in store.quads_for_pattern(
                subject,
                None,
                None,
                graph_name=graph,
            )
        )

    if (
        not has_subject_in_graph(store, old, checked_graph_kb)
        or not has_subject_in_graph(store, new, checked_graph_kb)
    ):
        raise HTTPException(
            status_code=400,
            detail="Knowledge base does not contain facts about old/new IRI.",
        )

    merge_entities(
        store,
        old,
        new,
        only_redirect=only_redirect,
        delete_old_only=delete_old_only,
        map_graph=checked_graph_map,
        KB_graph=checked_graph_kb,
        dedup_mapping=dedup_mapping,
    )

    return {
        "only_redirect": only_redirect,
        "delete_old_only": delete_old_only,
        "dedup_mapping": dedup_mapping,
        "old": str(old),
        "new": str(new),
        "kb_graph": str(checked_graph_kb),
        "map_graph": str(checked_graph_map),
    }

@router.post(
    "/kb-map-sync",
    summary="Synchronize knowledge-base entities and mapping entries",
    description=(
        "Synchronizes entities in a knowledge-base graph with entries in a mapping graph.\n\n"
        "Supported directions:\n"
        "- `mapping_to_kb`: create missing knowledge-base entities for mapping targets.\n"
        "- `kb_to_mapping`: create missing mapping entries for knowledge-base entities.\n"
        "- `both`: perform both operations.\n"
        "- `auto`: select `mapping_to_kb` if only mappings exist, `kb_to_mapping` if only "
        "entities exist, and `both` otherwise.\n\n"
        "By default, the endpoint performs a dry run. Set `execute=true` to modify the store."
    ),
    tags=["RDF", "Synchronization"],
    dependencies=[Depends(require_writable)],
)
async def kb_map_sync(
    knowledge_base_graph_iri: str = Query(
        ...,
        description=(
            "Named graph IRI containing the knowledge-base entities. "
            "The graph may be created implicitly when it does not yet exist."
        ),
    ),
    mapping_graph_iri: str = Query(
        ...,
        description=(
            "Named graph IRI containing the mapping entries. "
            "The graph may be created implicitly when it does not yet exist."
        ),
    ),
    direction: TypingLiteral[
        "auto",
        "mapping_to_kb",
        "kb_to_mapping",
        "both",
    ] = Query(
        default="auto",
        description=(
            "Synchronization direction. `auto` resolves the direction from the current graph contents."
        ),
    ),
    create_missing_entities: bool = Query(
        default=True,
        description=(
            "Applicable to `mapping_to_kb` and `both`. Create an entity when a mapping target "
            "has no subject facts in the knowledge-base graph."
        ),
    ),
    create_missing_mappings: bool = Query(
        default=True,
        description=(
            "Applicable to `kb_to_mapping` and `both`. Create a mapping entry for each entity "
            "that is not currently referenced as a mapping target."
        ),
    ),
    seed_mapping_labels: bool = Query(
        default=True,
        description=(
            "Applicable when mapping entries are created or updated. Copy the entity's first "
            "`rdfs:label` value to the mapping entry when no equivalent mapping label exists."
        ),
    ),
    default_entity_types: list[str] | None = Query(
        default=None,
        description=(
            "Fallback RDF type IRIs assigned to entities created from mapping entries when the "
            "mapping entry provides no type hints. May be specified multiple times."
        ),
    ),
    execute: bool = Query(
        default=False,
        description=(
            "If false, validate the request and resolve the synchronization direction without "
            "modifying the store. If true, perform the synchronization."
        ),
    ),
) -> dict[str, Any]:
    from .global_store import get_graph
    store = global_store.get_store()

    entity_graph, _ = get_graph(knowledge_base_graph_iri)
    map_graph, _ = get_graph(mapping_graph_iri)



    if not entity_graph and not map_graph:
        raise HTTPException(
            status_code=400,
            detail="Both entity_graph and map_graph are missing in the store.",
        )
    
    if not entity_graph:
        entity_graph=safeNamedNode(knowledge_base_graph_iri)
    if not map_graph:
        map_graph=safeNamedNode(mapping_graph_iri)

    # Resolve direction in the same way as sync_kb_mapping does (dry-run)
    has_mapping = any(True for _ in iter_mapping_entries(store, map_graph)) if map_graph else False
    has_entities = any(True for _ in iter_entities(store, entity_graph)) if map_graph else False

    resolved_direction = direction
    if direction == "auto":
        if has_mapping and not has_entities:
            resolved_direction = "mapping_to_kb"
        elif has_entities and not has_mapping:
            resolved_direction = "kb_to_mapping"
        else:
            resolved_direction = "both"

    if not execute:
        return {
            "execute": False,
            "entity_graph": str(entity_graph),
            "map_graph": str(map_graph),
            "checks": {
                "has_entities": has_entities,
                "has_mapping": has_mapping,
            },
            "direction": resolved_direction,
        }

    # Execute synchronization (mutating)

    result = sync_kb_mapping(
        store,
        entity_graph=entity_graph,
        map_graph=map_graph,
        direction=resolved_direction,
        seed_mapping_labels=seed_mapping_labels,
        create_missing_entities=create_missing_entities,
        create_missing_mappings=create_missing_mappings,
        default_entity_types=default_entity_types,
    )

    # JSON safe return
    return {
        "execute": False,
        "knowledge_base_graph": str(entity_graph),
        "mapping_graph": str(map_graph),
        "requested_direction": direction,
        "resolved_direction": resolved_direction,
        "checks": {
            "has_entities": has_entities,
            "has_mapping_entries": has_mapping,
        },
        "options": {
            "create_missing_entities": create_missing_entities,
            "create_missing_mappings": create_missing_mappings,
            "seed_mapping_labels": seed_mapping_labels,
            "default_entity_types": default_entity_types or [],
        },
    }

@router.delete(
    "/remove_graph",
    summary="Remove or clear a graph from the RDF store",
    description=(
        "Removes a named graph from the store. "
        "If the default graph is specified, it will be cleared but not removed.\n\n"
        "By default this endpoint performs a dry run (`execute=false`)."
    ),
    tags=["RDF"], dependencies=[Depends(require_writable)]
)
async def delete_graph(
    graph_iri: str | None = Query(
        default=None,
        description=(
            "Named graph IRI to remove. "
            "If omitted or set to 'default', the default graph will be cleared."
        ),
    ),
    execute: bool = Query(
        default=False,
        description="If true, performs the deletion. If false, only checks existence.",
    ),
) -> dict[str, Any]:
    from .global_store import get_graph, _store_lock
    store = global_store.get_store()

    # Resolve graph object
    if graph_iri is None or graph_iri.lower() == "default":
        checked_graph = DefaultGraph()
        graph_label = "default"
    else:
        checked_graph, all_graphs = get_graph(graph_iri)
        graph_label = graph_iri

    checked_graph, all_graphs = get_graph(graph_iri)
    if graph_iri is not None and not checked_graph:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}",
        )
    
    is_named = not isinstance(checked_graph, DefaultGraph)


    if not execute:
        return {
            "execute": False,
            "graph": graph_label,
            "is_named_graph": is_named,
            "action": "remove" if is_named else "clear",
        }

    try:
        store.remove_graph(checked_graph)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error removing graph: {str(e)}",
        )

    return {
        "execute": True,
        "graph": graph_label,
        "action": "removed" if is_named else "cleared",
    }


@router.get("/csv", summary="Export CSV", description="Exports a named graph or the entire store as CSV or loads a CSV as RDF into the store", tags=["RDF","Data"], dependencies=[Depends(require_writable)])
async def get_csv(
    graph: str | None = Query(default=None, description="Named graph IRI (optional)"),
    load_csv: str | Path | None = Query(default=None, description="Load a CSV file into the store"),
    delete: bool | None = Query(default=False, description="Removes triples from graph if true, done before loading triples (you may only use subject IRIs to just delete)")
    ):
    from collections import defaultdict
    import csv

    # EXPORT_DIRECTORY.mkdir(parents=True,exist_ok=True)
    output_file = EXPORT_DIRECTORY / "export.csv"
    delimiter = " | "

    from .global_store import get_graph
    store = global_store.get_store()

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

###LOGS###

@router.get("/logs", response_class=HTMLResponse, tags=["Admin"])
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


@router.post("/logs/clear", tags=["Admin"])
def clear_log_file():
    try:
        with open("app.log", "w") as f:
            f.write("")  # Logdatei leeren
    except Exception as e:
        return HTMLResponse(content=f"Error clearing log file: {e}", status_code=500)

    return RedirectResponse(url="/logs", status_code=303)

@router.post(
    "/logs/level",
    summary="Set log level",
    description="Sets the application log level at runtime.",
    tags=["Admin"]
)
async def set_log_level(
    logging_level: LogLevel = Query(..., description="New log level")
):
    old_level = logging.getLevelName(logger.level)

    new_level = getattr(logging, logging_level.value)
    logger.setLevel(new_level)

    return {
        "status": "ok",
        "old_level": old_level,
        "new_level": logging_level.value,
    }