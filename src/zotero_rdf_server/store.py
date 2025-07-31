from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
import os, shutil, requests, tempfile, time

from .logging_config import logger
from .config import *
from .models import ZoteroLibrary
from .utils import *
from .rdf import *
from .schema import zotero_schema

store = Store()

def get_graph(graph: str | NamedNode):
    if graph and isinstance(graph, str):
        graph = safeNamedNode(graph.strip().strip('<>').strip())
    return (graph, [str(g) for g in store.named_graphs()]) if graph and isinstance(graph, NamedNode) and store.contains_named_graph(graph) else (None, [str(g) for g in store.named_graphs()])

def initialize_store():
    global store
    if STORE_MODE == "memory":
        store = Store()
    elif STORE_MODE == "directory":
        os.makedirs(STORE_DIRECTORY, exist_ok=True)
        try:
            store = Store(path=STORE_DIRECTORY)
        except Exception as e:
            logger.exception(f"Failed to load store: {e}")
    else:
        raise ValueError(f"Invalid store_mode: {STORE_MODE}")

def clear_directory(directory_path):
    for filename in os.listdir(directory_path):
        file_path = os.path.join(directory_path, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            logger.error(f"Failed to delete {file_path}. Reason: {e}")


def ensure_store(store):
    try:
        store.flush()
        logger.info("Store flushed to disk.")
    except Exception as e:
        logger.warning(f"Flush failed: {e}")
    try:
        store.optimize()
        logger.info("Store optimized after bulk load.")
    except Exception as e:
        logger.warning(f"Optimize failed: {e}")


def refresh_store(force_reload:bool = False):
    global store
    if REFRESH == False and not force_reload:
        try:
            del store
            store = Store(path=STORE_DIRECTORY)
            logger.info(f"Zotero data loaded (not refreshed) successfully. {len(store)} triples, graphs: {list(store.named_graphs())}")
        except Exception as e:
            logger.exception(f"Failed to load store: {e}")

    else:
        while True:
            try:
                logger.info("Refreshing Zotero data...")
                del store

                if STORE_MODE == "memory":
                    store = Store()                    
                else:
                    if os.path.exists(STORE_DIRECTORY):
                        clear_directory(STORE_DIRECTORY)
                    else:
                        os.makedirs(STORE_DIRECTORY, exist_ok=True)
                    try:
                        store = Store(path=STORE_DIRECTORY)
                    except Exception as e:
                        logger.exception(f"Failed to load store: {e}")

                if ZOT_SCHEMA: # TODO in Class?
                    try:
                        schema = requests.get(ZOT_SCHEMA).json()
                        zotero_schema(store,schema,ZOT_NS)
                        logger.info(f"Schema loaded from {ZOT_SCHEMA} for {ZOT_NS}")
                    except Exception as e:
                        logger.error(f"Schema could not be loaded: {e}")

                for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                    lib = ZoteroLibrary(lib_cfg)
                    ensure_store(store)
                    if lib.load_mode == "rdf":
                        try:
                            logger.info(f"Fetching RDF export for '{lib.name}'")
                            rdf_data = lib.fetch_rdf_export()
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".rdf") as tmp:
                                tmp.write(rdf_data)
                                tmp_path = tmp.name
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
                                os.unlink(tmp_path)
                        except Exception as e:
                            logger.error(f"Error loading RDF from API for {lib.library_id}: {e}")
                    elif lib.load_mode == "manual_import":
                        try:
                            import_rdf_from_disk(lib, store)
                        except Exception as e:
                            logger.error(f"Error loading from file import for {lib.name}: {e}")
                    elif lib.load_mode == "json":
                        if lib.library_type != "knowledge base":
                            try:
                                build_graph_for_library(lib, store)
                            except Exception as e:
                                logger.error(f"Error loading JSON from API for {lib.library_id}: {e}")
                        else:
                            logger.warning(f"{lib.name} excluded, because is a {lib.library_type}")
                    else:
                        logger.warning(f"Unknown load_mode '{lib.load_mode}' for '{lib.name}' — skipping.")

                    if lib.parser.get("auto")==True:
                        try:
                            ensure_store(store)
                            time.sleep(2)
                            logger.info("Start Parser Plugin")
                            parse_all_notes(lib, store, delete=True)
                        except Exception as e:
                            logger.error(f"Error parsing notes: {e}")
                    else:
                        logger.info(f"No notes parsing for {lib.name} in {lib.parser}")

                logger.info(f"Zotero data refreshed successfully. {len(store)} triples, graphs: {list(store.named_graphs())}")
                ensure_store(store)

            except Exception as e:
                logger.error(f"Error refreshing data: {e}")

            if REFRESH_INTERVAL >= 30:
                logger.info(f"Next refresh in {REFRESH_INTERVAL} seconds")
                time.sleep(REFRESH_INTERVAL)
            else:
                logger.info("Refresh interval less than 30 seconds — exiting after initial load.")
                break