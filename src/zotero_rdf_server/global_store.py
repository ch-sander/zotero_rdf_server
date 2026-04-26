from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
import os, shutil, requests, tempfile, time, gc, threading
from pathlib import Path

from .logging_config import logger
from .config import *
from .models import ZoteroLibrary
from .utils import *
from .rdf import *
from .schema import zotero_schema
from importlib.util import find_spec

store = None
_store_lock = threading.RLock()

def close_store():
    global store
    store = None
    gc.collect()


def open_store_rw():
    global store
    STORE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    store = Store(path=str(STORE_DIRECTORY))
    return store


def get_store():
    global store

    if store is None:
        initialize_store()

    return store

def get_graph(graph: str | NamedNode):
    s = get_store()

    if graph and isinstance(graph, str):
        graph = safeNamedNode(graph.strip().strip('<>').strip())

    graphs = [str(g) for g in s.named_graphs()]

    if graph and isinstance(graph, NamedNode) and s.contains_named_graph(graph):
        return graph, graphs

    return None, graphs

def initialize_store():
    global store

    with _store_lock:
        close_store()

        if STORE_MODE == "directory_ro":
            store = Store.read_only(path=str(STORE_DIRECTORY))
            logger.warning(f"Store re-opened read-only. {len(store)} triples.")

        elif STORE_MODE == "memory":
            store = Store()
            logger.warning(f"Store re-opened in memory. {len(store)} triples.")

        elif STORE_MODE == "directory_rw":
            try:
                STORE_DIRECTORY.mkdir(parents=True, exist_ok=True)
                store = Store(path=str(STORE_DIRECTORY))
                logger.warning(f"Store re-opened read-write. {len(store)} triples.")
            except Exception as e:
                logger.exception(f"Failed to load store: {e}")
                raise

        else:
            raise ValueError(f"Invalid store_mode: {STORE_MODE}")

def clear_directory(directory_path):
    directory = safe_path(directory_path)
    if directory.exists():
        for item in directory.iterdir():
            try:
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
            except Exception as e:
                logger.error(f"Failed to delete {item}. Reason: {e}")
    else:
        logger.error(f"{directory} does not exist!")

def ensure_store(store):
    if STORE_MODE != "directory_rw":
        return

    try:
        with _store_lock:
            store.flush()
        logger.info("Store flushed to disk.")
    except Exception as e:
        logger.warning(f"Flush failed: {e}")

    try:
        with _store_lock:
            store.optimize()
        logger.info("Store optimized after bulk load.")
    except Exception as e:
        logger.warning(f"Optimize failed: {e}")

def refresh_store(force_reload:bool = False, remove_store:bool=True):
    global store

    with _store_lock:
        if STORE_MODE == "directory_ro":
            while True:
                try:
                    close_store()
                except Exception:
                    pass
                try:
                    store = Store.read_only(path=str(STORE_DIRECTORY))
                    logger.info(f"RO store reopened. {len(store)} triples.")
                except Exception as e:
                    logger.warning(f"RO reopen failed: {e}")

                if REFRESH_INTERVAL >= 30:
                    time.sleep(REFRESH_INTERVAL)
                else:
                    break
            return
            
        if REFRESH == False and not force_reload:
            try:
                close_store()
                store = Store(path=STORE_DIRECTORY)
                logger.info(f"Zotero data loaded (not refreshed) successfully. {len(store)} triples, graphs: {list(store.named_graphs())}")
            except Exception as e:
                logger.exception(f"Failed to load store: {e}")
                raise

        else:
            import importlib
            from . import config
            importlib.reload(config)
            logger.warning("CONFIG reloaded!")
            while True:
                try:
                    logger.info("Refreshing Zotero data...")
                    close_store()

                    if config.STORE_MODE == "memory":
                        store = Store()                    
                    else:
                        if config.STORE_DIRECTORY.exists() and remove_store:
                            clear_directory(config.STORE_DIRECTORY)
                        # else:
                        #     STORE_DIRECTORY.mkdir(parents=True,exist_ok=True)
                        try:
                            store = Store(path=config.STORE_DIRECTORY)
                        except Exception as e:
                            logger.exception(f"Failed to load store: {e}")
                            raise

                    if ZOT_SCHEMA:
                        try:
                            schema = requests.get(config.ZOT_SCHEMA).json()
                            zotero_schema(store,schema,ZOT_NS)
                            logger.info(f"Schema loaded from {config.ZOT_SCHEMA} for {ZOT_NS}")
                        except Exception as e:
                            logger.error(f"Schema could not be loaded: {e}")

                    for lib_cfg in config.ZOTERO_LIBRARIES_CONFIGS:
                        lib = ZoteroLibrary(lib_cfg)
                        ensure_store(store)
                        if lib.load_mode == "rdf":
                            try:
                                logger.info(f"Fetching RDF export for '{lib.name}'")
                                rdf_data = lib.fetch_rdf_export()
                                with tempfile.NamedTemporaryFile(delete=False, suffix=".rdf") as tmp:
                                    tmp.write(rdf_data)
                                    tmp_path = Path(tmp.name)
                                try:
                                    before = len(store)
                                    store.bulk_load(
                                        path=tmp_path,
                                        format=RdfFormat.RDF_XML,
                                        base_iri=f"{lib.base_url}/items/",
                                        to_graph=safeNamedNode(lib.base_url)
                                    )
                                    after = len(store)
                                    logger.info(f"Loaded {after - before} triples from RDF export for '{lib.name}'")
                                finally:
                                    tmp_path.unlink(missing_ok=True)
                            except Exception as e:
                                logger.error(f"Error loading RDF from API for {lib.library_id}: {e}")
                        elif lib.load_mode == "manual_import":
                            try:
                                import_rdf(lib, store)
                            except Exception as e:
                                logger.error(f"Error loading from file import for {lib.name}: {e}")
                        elif lib.load_mode == "json":
                            if lib.library_type not in ["knowledge base","mapping", "dataset"]:
                                try:
                                    build_graph_for_library(lib, store)
                                except Exception as e:
                                    logger.error(f"Error loading JSON from API for {lib.library_id}: {e}")
                            else:
                                logger.warning(f"{lib.name} excluded, because is a {lib.library_type}")
                        else:
                            logger.warning(f"Unknown load_mode '{lib.load_mode}' for '{lib.name}' — skipping.")

                        if (lib.plugin or {}).get("notes_parser", {}).get("auto") is True:
                            try:
                                ensure_store(store)
                                time.sleep(2)
                                logger.info("Start Parser Plugin")
                                # TODO read predicate/query, and tag filter from YAML?
                                
                                try:
                                    from .plugins.parser.parse_note import parse_all_notes
                                    parse_all_notes(lib, store, delete=True)
                                except ImportError:
                                    logger.exception("parse_all_notes import failed!")                                

                                
                            except Exception as e:
                                logger.error(f"Error parsing notes: {e}")
                        else:
                            logger.info(f"No notes parsing for {lib.name}")

                    logger.info(f"Zotero data refreshed successfully. {len(store)} triples, graphs: {list(store.named_graphs())}")
                    ensure_store(store)

                except Exception as e:
                    logger.error(f"Error refreshing data: {e}")

                if config.REFRESH_INTERVAL >= 30:
                    logger.info(f"Next refresh in {config.REFRESH_INTERVAL} seconds")
                    time.sleep(config.REFRESH_INTERVAL)
                else:
                    logger.info("Refresh interval less than 30 seconds — exiting after initial load.")
                    break