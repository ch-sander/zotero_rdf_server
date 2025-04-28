import os
import requests
import yaml
import threading
import time
import tempfile
import logging
import shutil
from uuid import uuid4
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn
import subprocess

# --- Load configuration ---
config_path = os.getenv("CONFIG_FILE", "config.yaml")

with open(config_path, "r") as f:
    config = yaml.safe_load(f)

log_level = config["server"].get("log_level", "info").upper()
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

# --- Config ---
API_KEY = config["zotero"]["api_key"]
LIBRARY_TYPE = config["zotero"]["library_type"]
LIBRARY_ID = config["zotero"]["library_id"]
PORT = config["server"]["port"]
REFRESH_INTERVAL = config["server"]["refresh_interval"]
STORE_MODE = "directory"
STORE_DIRECTORY = os.getenv("STORE_DIRECTORY", "./data")
EXPORT_DIRECTORY = config["server"].get("export_directory", "./exports")
IMPORT_DIRECTORY = config["server"].get("import_directory", "./import")
LOAD_MODE = config["zotero"].get("load_mode", "json")
RDF_EXPORT_FORMAT = config["zotero"].get("rdf_export_format", "rdf_zotero")
API_QUERY_PARAMS = config["zotero"].get("api_query_params", {})
OXIGRAPH_CONTAINER = os.getenv("OXIGRAPH_CONTAINER", "oxigraph")
LIMIT = 100
BASE_URL = f"https://api.zotero.org/{LIBRARY_TYPE}/{LIBRARY_ID}"
HEADERS = {"Zotero-API-Key": API_KEY} if API_KEY else {}

# --- Constants ---
ZOT_NS = "http://www.zotero.org/namespaces/export#"

# --- App ---
router = APIRouter()
store = None

# --- Functions ---

def import_rdf_directory(target_store: Store, directory_path: str):
    for filename in os.listdir(directory_path):
        filepath = os.path.join(directory_path, filename)
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

        logger.info(f"Importing RDF file: {filepath}")
        before = len(target_store)
        target_store.bulk_load(path=filepath, format=fmt, base_iri=f"{BASE_URL}/items/", to_graph=NamedNode(BASE_URL))
        after = len(target_store)
        logger.info(f"Imported {after - before} triples from {filename}")

def fetch_rdf_export(target_store: Store):
    url = f"{BASE_URL}/items"
    params = {"format": RDF_EXPORT_FORMAT, "limit": LIMIT, **API_QUERY_PARAMS}
    response = requests.get(url, headers=HEADERS, params=params)
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".rdf") as tmp:
        tmp.write(response.content)
        tmp_path = tmp.name

    with open("./exports/test.rdf", "wb") as tmp:
        tmp.write(response.content)
        tmp_path = tmp.name


    logger.info(f"Loading API RDF export from temp file {tmp_path}")
    try:
        before = len(target_store)
        target_store.bulk_load(path=tmp_path, format=RdfFormat.RDF_XML, base_iri=url+"/", to_graph=NamedNode(BASE_URL))
        after = len(target_store)
        logger.info(f"Loaded {after - before} triples from API export")
    finally:
        print(tmp_path)
        # os.unlink(tmp_path)

def fetch_all(endpoint: str) -> list:
    start = 0
    all_data = []
    base_params = {"format": "json", "limit": LIMIT, **API_QUERY_PARAMS}

    while True:
        params = {**base_params, "start": start}
        response = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params)
        response.raise_for_status()
        data = response.json()
        if not data:
            break
        all_data.extend(data)
        start += LIMIT

    return all_data

def build_graph(target_store: Store):
    items = fetch_all("items")
    collections = fetch_all("collections")

    logger.info(f"Fetched {len(items)} items and {len(collections)} collections.")

    for col in collections:
        col_data = col["data"]
        col_uri = NamedNode(f"http://zotero.org/collection/{col_data['key']}")
        target_store.add(Quad(col_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Collection")))
        for field, value in col_data.items():
            if value:
                target_store.add(Quad(col_uri, NamedNode(f"{ZOT_NS}{field}"), Literal(str(value))))

    for item in items:
        data = item.get("data", {})
        key = data.get("key")
        node_uri = NamedNode(f"http://zotero.org/item/{key}")
        target_store.add(Quad(node_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Item")))

        for field, value in data.items():
            if value:
                target_store.add(Quad(node_uri, NamedNode(f"{ZOT_NS}{field}"), Literal(str(value))))

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

            if LOAD_MODE == "rdf":
                fetch_rdf_export(store)
            elif LOAD_MODE == "manual_import":
                import_rdf_directory(store, IMPORT_DIRECTORY)
            elif LOAD_MODE == "json":
                build_graph(store)
            else:
                raise ValueError(f"Unknown load_mode: {LOAD_MODE}")
            logger.info("Zotero data refreshed successfully.")

        except Exception as e:
            logger.error(f"Error refreshing data: {e}")
        time.sleep(REFRESH_INTERVAL)



# --- API Endpoints ---

@router.get("/export")
async def export_graph(format: str = Query("trig")):
    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)
    if format == "trig":
        path = os.path.join(EXPORT_DIRECTORY, "zotero_graph.trig")
        store.dump(path, RdfFormat.TRIG)
    elif format == "nquads":
        path = os.path.join(EXPORT_DIRECTORY, "zotero_graph.nq")
        store.dump(path, RdfFormat.N_QUADS)
    else:
        raise HTTPException(status_code=400, detail="Unsupported export format")

    return FileResponse(path, filename=os.path.basename(path))

# --- Start server ---

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    initialize_store()
    threading.Thread(target=refresh_store, daemon=True).start()
    yield

app = FastAPI(lifespan=app_lifespan)
app.include_router(router)