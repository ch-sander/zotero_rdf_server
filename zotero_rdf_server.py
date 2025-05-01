import os
import requests
import yaml
import threading
import time
import tempfile
import logging
import shutil
from uuid import uuid4
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn

# --- Load configuration ---
config_path = os.getenv("CONFIG_FILE", "config.yaml")

with open(config_path, "r") as f:
    config = yaml.safe_load(f)

log_level = config["server"].get("log_level", "info").upper()
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

# --- Config ---
ZOTERO_CONFIGS = config["zotero"]
PORT = config["server"]["port"]
REFRESH_INTERVAL = config["server"]["refresh_interval"]
STORE_MODE = "directory"
STORE_DIRECTORY = os.getenv("STORE_DIRECTORY", "./data")
EXPORT_DIRECTORY = config["server"].get("export_directory", "./exports")
IMPORT_DIRECTORY = config["server"].get("import_directory", "./import")
OXIGRAPH_CONTAINER = os.getenv("OXIGRAPH_CONTAINER", "oxigraph")
LIMIT = 100

# --- Constants ---
ZOT_NS = "http://www.zotero.org/namespaces/export#"

# --- App ---
router = APIRouter()
store = None

# --- Class ---

class ZoteroLibrary:
    def __init__(self, config: dict):
        self.name = config["name"]
        self.load_mode = config.get("load_mode", "json")
        self.library_type = config["library_type"]
        self.library_id = config["library_id"]
        self.api_key = config.get("api_key", None)
        self.rdf_export_format = config.get("rdf_export_format", "rdf_zotero")
        self.api_query_params = config.get("api_query_params") or {}
        self.base_api_url = f"https://api.zotero.org/{self.library_type}/{self.library_id}"
        self.base_url = f"https://www.zotero.org/{self.library_type}/{self.library_id}"
        self.headers = {"Zotero-API-Key": self.api_key} if self.api_key else {}

    def fetch_paginated(self, endpoint: str) -> list:
        results = []
        start = 0
        while True:
            params = {"format": "json", "limit": LIMIT, "start": start, **self.api_query_params}
            response = requests.get(f"{self.base_api_url}/{endpoint}", headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            if not data:
                break
            results.extend(data)
            start += LIMIT
        return results

    def fetch_items(self) -> list:
        return self.fetch_paginated("items")

    def fetch_collections(self) -> list:
        return self.fetch_paginated("collections")

    def fetch_rdf_export(self) -> bytes:
        params = {"format": self.rdf_export_format, "limit": LIMIT, **self.api_query_params}
        response = requests.get(f"{self.base_api_url}/items", headers=self.headers, params=params)
        response.raise_for_status()
        return response.content  # RDF XML as Bytes


# --- Functions ---

def import_rdf_from_disk(lib: ZoteroLibrary, store: Store, base_dir: str):
    subdir = os.path.join(base_dir, lib.name)
    if not os.path.isdir(subdir):
        logger.warning(f"Directory not found for manual import: {subdir}")
        return

    logger.info(f"Importing RDF files for '{lib.name}' from {subdir}")
    for filename in os.listdir(subdir):
        filepath = os.path.join(subdir, filename)
        if filename.endswith(".rdf"):
            fmt = RdfFormat.RDF_XML
        elif filename.endswith(".trig"):
            fmt = RdfFormat.TRIG
        elif filename.endswith(".ttl"):
            fmt = RdfFormat.TURTLE
        elif filename.endswith(".nt"):
            fmt = RdfFormat.N_TRIPLES
        elif filename.endswith(".nq"):
            fmt = RdfFormat.N_QUADS
        else:
            logger.info(f"Skipping unsupported file: {filename}")
            continue

        before = len(store)
        store.bulk_load(path=filepath, format=fmt, base_iri=f"{lib.base_url}/items/", to_graph=NamedNode(lib.base_url))
        after = len(store)
        logger.info(f"Imported {after - before} triples from {filename}")


def add_rdf_from_dict_pyox(store: Store, subject: NamedNode | BlankNode, data: dict, ns_prefix: str):
    for field, value in data.items():
        predicate = NamedNode(f"{ns_prefix}{field}")

        if isinstance(value, dict):
            bnode = BlankNode()
            store.add(Quad(subject, predicate, bnode))
            add_rdf_from_dict_pyox(store, bnode, value, ns_prefix)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    bnode = BlankNode()
                    store.add(Quad(subject, predicate, bnode))
                    add_rdf_from_dict_pyox(store, bnode, item, ns_prefix)
                else:
                    lit = Literal(str(item))
                    store.add(Quad(subject, predicate, lit))

        elif value is not None:
            lit = Literal(str(value))
            store.add(Quad(subject, predicate, lit))


def build_graph_for_library(lib: ZoteroLibrary, store: Store):
    items = lib.fetch_items()
    collections = lib.fetch_collections()

    logger.info(f"[{lib.name}] Fetched {len(items)} items and {len(collections)} collections.")

    for col in collections:
        col_data = col["data"]
        col_uri = NamedNode(f"{lib.base_url}/collections/{col_data['key']}")
        store.add(Quad(col_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Collection")))
        add_rdf_from_dict_pyox(store, col_uri, col_data, ZOT_NS)

    for item in items:
        data = item.get("data", {})
        key = data.get("key")
        node_uri = NamedNode(f"{lib.base_url}/items/{key}")
        store.add(Quad(node_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Item")))
        add_rdf_from_dict_pyox(store, node_uri, data, ZOT_NS)



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


def refresh_store():
    global store
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

            for lib_cfg in ZOTERO_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)

                if lib.load_mode == "rdf":
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
                            to_graph=NamedNode(lib.base_url)
                        )
                        after = len(store)
                        logger.info(f"Loaded {after - before} triples from RDF export for '{lib.name}'")
                    finally:
                        os.unlink(tmp_path)

                elif lib.load_mode == "manual_import":
                    import_rdf_from_disk(lib, store, IMPORT_DIRECTORY)

                elif lib.load_mode == "json":
                    build_graph_for_library(lib, store)

                else:
                    logger.warning(f"Unknown load_mode '{lib.load_mode}' for '{lib.name}' — skipping.")

            logger.info("Zotero data refreshed successfully.")

        except Exception as e:
            logger.error(f"Error refreshing data: {e}")
        time.sleep(REFRESH_INTERVAL)

# --- API Endpoints ---

@router.get("/export")
async def export_graph(
    format: str = Query("trig"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional)")
):
    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)

    format_map = {
        "trig": (RdfFormat.TRIG, "trig"),
        "nquads": (RdfFormat.N_QUADS, "nq"),
        "ttl": (RdfFormat.TURTLE, "ttl"),
        "nt": (RdfFormat.N_TRIPLES, "nt"),
        "n3": (RdfFormat.N3, "n3"),
        "xml": (RdfFormat.RDF_XML, "rdf")
    }

    if format not in format_map:
        raise HTTPException(status_code=400, detail="Unsupported export format")

    rdf_format, extension = format_map[format]
    path = os.path.join(EXPORT_DIRECTORY, f"zotero_graph.{extension}")

    no_named_graph_support = rdf_format in {
        RdfFormat.TURTLE, RdfFormat.N_TRIPLES, RdfFormat.N3, RdfFormat.RDF_XML
    }

    kwargs = {}
    if graph:
        kwargs["from_graph"] = NamedNode(graph)
    elif no_named_graph_support:
        kwargs["from_graph"] = DefaultGraph()

    store.dump(output=path, format=rdf_format, **kwargs)
    return FileResponse(path, filename=os.path.basename(path))

# --- Start server ---

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    initialize_store()
    threading.Thread(target=refresh_store, daemon=True).start()
    yield

app = FastAPI(lifespan=app_lifespan)
app.include_router(router)