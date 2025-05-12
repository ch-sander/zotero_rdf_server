from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
import os, shutil, requests, tempfile, time
from enum import Enum

from .logging_config import logger
from .config import *
from .models import ZoteroLibrary
from .utils import *
from .rdf import *
from .schema import zotero_schema

store = Store()

def initialize_store():
    global store
    if STORE_MODE == "memory":
        store = Store()
    elif STORE_MODE == "directory":
        os.makedirs(STORE_DIRECTORY, exist_ok=True)
        store = Store(path=STORE_DIRECTORY)
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


def refresh_store(force_reload:bool = False):
    global store
    if REFRESH == False and not force_reload:
        del store
        store = Store(path=STORE_DIRECTORY)
        logger.info(f"Zotero data loaded (not refresehd) successfully. {len(store)} triples, graphs: {list(store.named_graphs())}")
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
                    store = Store(path=STORE_DIRECTORY)

                if ZOT_SCHEMA: # TODO in Class?
                    try:
                        schema = requests.get(ZOT_SCHEMA).json()
                        zotero_schema(store,schema,ZOT_NS)
                        logger.info(f"Schema loaded from {ZOT_SCHEMA} for {ZOT_NS}")
                    except Exception as e:
                        logger.error(f"Schema could not be loaded: {e}")

                for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                    lib = ZoteroLibrary(lib_cfg)

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
                        try:
                            build_graph_for_library(lib, store)
                        except Exception as e:
                            logger.error(f"Error loading JSON from API for {lib.library_id}: {e}")
                    else:
                        logger.warning(f"Unknown load_mode '{lib.load_mode}' for '{lib.name}' — skipping.")

                    if lib.parser.get("auto")==True:
                        try:
                            logger.info("Start Parser Plugin")
                            parse_all_notes(lib, store)
                        except Exception as e:
                            logger.error(f"Error parsing notes: {e}")
                    else:
                        logger.info(f"No notes parsing for {lib.name} in {lib.parser}")


                logger.info(f"Zotero data refreshed successfully. {len(store)} triples, graphs: {list(store.named_graphs())}")


            except Exception as e:
                logger.error(f"Error refreshing data: {e}")

            if REFRESH_INTERVAL >= 30:
                logger.info(f"Next refresh in {REFRESH_INTERVAL} seconds")
                time.sleep(REFRESH_INTERVAL)
            else:
                logger.info("Refresh interval less than 30 seconds — exiting after initial load.")
                break


class LogLevel(str, Enum):
    debug = "DEBUG"
    info = "INFO"
    warning = "WARNING"
    error = "ERROR"

def iri_to_filename(iri: str) -> str:
    parsed = urlparse(iri)
    parts = [parsed.netloc] + parsed.path.strip("/").split("/")
    safe = "_".join(parts)
    return re.sub(r"[^\w\-\.]", "_", safe)