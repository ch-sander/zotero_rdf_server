import os
import requests
import yaml
import threading
import time
import tempfile
import logging
import shutil
from uuid import uuid5, NAMESPACE_URL
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn

# --- Load configuration ---
config_path = os.getenv("CONFIG_FILE", "config.yaml")
zotero_config_path = os.getenv("ZOTERO_CONFIG_FILE", "zotero.yaml")

with open(config_path, "r") as f:
    config = yaml.safe_load(f)

with open(zotero_config_path, "r") as f:
    zotero_config = yaml.safe_load(f)

log_level = config["server"].get("log_level", "info").upper()
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

# --- Config ---
ZOTERO_LIBRARIES_CONFIGS = zotero_config["libraries"]
ZOTERO_CONFIGS = zotero_config["context"]
PORT = config["server"]["port"]
REFRESH_INTERVAL = config["server"]["refresh_interval"]
STORE_MODE = "directory"
STORE_DIRECTORY = os.getenv("STORE_DIRECTORY", "./data")
EXPORT_DIRECTORY = config["server"].get("export_directory", "./exports")
IMPORT_DIRECTORY = config["server"].get("import_directory", "./import")
OXIGRAPH_CONTAINER = os.getenv("OXIGRAPH_CONTAINER", "oxigraph")
LIMIT = 100

# --- Constants ---
ZOT_NS = ZOTERO_CONFIGS.get("zotero_ns", "http://www.zotero.org/namespaces/export#")


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
        self.map = config.get("map") or {}

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


def add_rdf_from_dict(store: Store, subject: NamedNode | BlankNode, data: dict, ns_prefix: str, base_uri: str, map: dict):
    BASE_NS = uuid5(NAMESPACE_URL, base_uri)
    white = map.get("white") or []
    black = map.get("black") or []
    named = map.get("named") or []
    def zotero_property_map(predicate_str: str, object: str | dict | list, map: dict):
        XSD_NS = "http://www.w3.org/2001/XMLSchema#"


        try:
            if object == "" or object == {} or object == [] or object == None or object == [{}]:
                return None
            
            if named and predicate_str not in named: # no mapping if none specified or not specified for mapping
                return None if isinstance(object, dict) else Literal(str(object))
            
            if isinstance(object, dict): # dicts as named nodes

                if predicate_str == "tags" and "tag" in object: # tags
                    tag_value = object["tag"]
                    tag_iri = uuid5(BASE_NS, tag_value)
                    tag_node = NamedNode(f"{base_uri}/tags/{tag_iri}")
                    store.add(Quad(subject, NamedNode(f"{ns_prefix}tags"), tag_node))
                    logger.info(f"Tag added: {tag_value}")
                    for key, val in object.items():
                        if val:
                            pred = NamedNode(f"{ns_prefix}{key}")
                            store.add(Quad(tag_node, pred, Literal(str(val))))
                    return None
                
                if predicate_str == "creators": #creators
                    if "name" in object:
                        label = object["name"]
                    else:
                        label = f"{object.get('lastName', '')}-{object.get('firstName', '')}"
                    creator_uuid = uuid5(BASE_NS, label)
                    creator_node = NamedNode(f"{base_uri}/creators/{creator_uuid}")
                    for key, val in object.items():
                        if key != "creatorType" and val:
                            pred = NamedNode(f"{ns_prefix}{key}")
                            store.add(Quad(creator_node, pred, Literal(str(val))))
                    bnode = BlankNode()
                    logger.info(f"Creator added: {label}")
                    store.add(Quad(subject, NamedNode(f"{ns_prefix}creators"), bnode))
                    store.add(Quad(bnode, NamedNode(f"{ns_prefix}creator"), creator_node))
                    if "creatorType" in object and object["creatorType"]:
                        type_node = NamedNode(f"{ns_prefix}{object['creatorType']}")
                        store.add(Quad(bnode, NamedNode(f"{ns_prefix}creatorType"), type_node))
                    return None

            elif isinstance(object, str):    
                if predicate_str == "collections": #collections
                    return NamedNode(f"{base_uri}/collections/{object}")
                elif predicate_str in ["url","dc:relation"] and object.startswith("http"): # url
                    return NamedNode(object)
                elif predicate_str in ["numPages","numberOfVolumes","volume","series number"] and object.isdigit(): # int
                    return Literal(str(object),datatype=NamedNode(f"{XSD_NS}int"))
                elif predicate_str == "date": # date
                    year = int(object)
                    if year > 0:
                        return Literal(str(year), datatype=NamedNode(f"{XSD_NS}gYear"))
                elif predicate_str in ["dateModified","accessDate","zot:dateAdded"]: # dateTime
                    return Literal(str(object),datatype=NamedNode(f"{XSD_NS}dateTime"))
                else:
                    return Literal(str(object))
            else:
                logger.error(f"Error: pass dict or str")

        except Exception as e:
            logger.error(f"Error: {e}")
            return None
        
    # main function start here!

    for field, value in data.items():
        predicate = NamedNode(f"{ns_prefix}{field}")

        if white:
            if field not in white and field not in named:
                logger.info(f"Skipping {field} (not in whitelist)")
                continue
        elif black and field in black:
            logger.info(f"Skipping {field} (in blacklist)")
            continue
        
        if isinstance(value, dict):
            obj = zotero_property_map(field, value, map)
            if obj is None:
                continue
            bnode = BlankNode()
            store.add(Quad(subject, predicate, bnode))
            add_rdf_from_dict(store, bnode, value, ns_prefix, base_uri, map)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    if zotero_property_map(field, item, map) is None:
                        continue
                    bnode = BlankNode()
                    store.add(Quad(subject, predicate, bnode))
                    add_rdf_from_dict(store, bnode, item, ns_prefix, base_uri, map)
                else:
                    obj = zotero_property_map(field, item, map)
                    if obj is not None:
                        store.add(Quad(subject, predicate, obj))

        elif value is not None:
            obj = zotero_property_map(field, value, map)
            if obj is not None:
                store.add(Quad(subject, predicate, obj))


def build_graph_for_library(lib: ZoteroLibrary, store: Store):
    items = lib.fetch_items()
    collections = lib.fetch_collections()
    logger.info(f"[{lib.name}] Fetched {len(items)} items and {len(collections)} collections.")
    map = lib.map

    for col in collections:
        col_data = col["data"]
        col_uri = NamedNode(f"{lib.base_url}/collections/{col_data['key']}")
        store.add(Quad(col_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Collection")))
        add_rdf_from_dict(store, col_uri, col_data, ZOT_NS, lib.base_url, map)

    for item in items:
        data = item.get("data", {})
        key = data.get("key")        
        node_uri = NamedNode(f"{lib.base_url}/items/{key}")
        store.add(Quad(node_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Item")))
        add_rdf_from_dict(store, node_uri, data, ZOT_NS, lib.base_url, map)

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

            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
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

            logger.info(f"Zotero data refreshed successfully. {len(store)} triples")

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