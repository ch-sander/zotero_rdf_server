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
# API_KEY = config["zotero"]["api_key"]
# LIBRARY_TYPE = config["zotero"]["library_type"]
# LIBRARY_ID = config["zotero"]["library_id"]
ZOTERO_CONFIGS = config["zotero"]
PORT = config["server"]["port"]
REFRESH_INTERVAL = config["server"]["refresh_interval"]
STORE_MODE = "directory"
STORE_DIRECTORY = os.getenv("STORE_DIRECTORY", "./data")
EXPORT_DIRECTORY = config["server"].get("export_directory", "./exports")
IMPORT_DIRECTORY = config["server"].get("import_directory", "./import")
# LOAD_MODE = config["zotero"].get("load_mode", "json")
# RDF_EXPORT_FORMAT = config["zotero"].get("rdf_export_format", "rdf_zotero")
# API_QUERY_PARAMS = config["zotero"].get("api_query_params", {})
OXIGRAPH_CONTAINER = os.getenv("OXIGRAPH_CONTAINER", "oxigraph")
LIMIT = 100
# BASE_URL = f"https://api.zotero.org/{LIBRARY_TYPE}/{LIBRARY_ID}"
HEADERS = {"Zotero-API-Key": API_KEY} if API_KEY else {}

# --- Constants ---
ZOT_NS = "http://www.zotero.org/namespaces/export#"

# --- App ---
router = APIRouter()
store = None

# --- Functions ---

def import_rdf_directory(target_store: Store, directory_path: str):
    for lib_cfg in ZOTERO_CONFIGS:
        name = lib_cfg["name"]
        load_mode = lib_cfg.get("load_mode", "manual_import")

        if load_mode != "manual_import":
            continue

        subdir = os.path.join(directory_path, name)
        if not os.path.isdir(subdir):
            logger.warning(f"Skipping {name}: directory {subdir} not found")
            continue

        logger.info(f"Importing RDF files for library '{name}' from {subdir}")
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

            logger.info(f"Importing RDF file: {filepath}")
            before = len(target_store)
            base_iri = f"{lib_cfg['library_type']}/{lib_cfg['library_id']}/items/"
            target_store.bulk_load(path=filepath, format=fmt, base_iri=base_iri, to_graph=NamedNode(base_iri))
            after = len(target_store)
            logger.info(f"Imported {after - before} triples from {filename}")


def fetch_rdf_export(target_store: Store):
    for lib_cfg in ZOTERO_CONFIGS:
        name = lib_cfg["name"]
        load_mode = lib_cfg.get("load_mode", "json")
        if load_mode != "rdf":
            continue

        base_url = f"https://api.zotero.org/{lib_cfg['library_type']}/{lib_cfg['library_id']}"
        export_format = lib_cfg.get("rdf_export_format", "rdf_zotero")
        api_key = lib_cfg["api_key"]
        api_query_params = lib_cfg.get("api_query_params", {})
        headers = {"Zotero-API-Key": api_key}

        url = f"{base_url}/items"
        params = {"format": export_format, "limit": LIMIT, **api_query_params}

        logger.info(f"Fetching RDF export for library '{name}' in format '{export_format}'")
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".rdf") as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        logger.info(f"Loading API RDF export for '{name}' from temp file {tmp_path}")
        try:
            before = len(target_store)
            target_store.bulk_load(path=tmp_path, format=RdfFormat.RDF_XML, base_iri=url + "/", to_graph=NamedNode(base_url))
            after = len(target_store)
            logger.info(f"Loaded {after - before} triples from API export for '{name}'")
        finally:
            os.unlink(tmp_path)


def fetch_all(endpoint: str) -> list:
    results = {}

    for lib_cfg in ZOTERO_CONFIGS:
        name = lib_cfg["name"]
        load_mode = lib_cfg.get("load_mode", "json")

        if load_mode not in ("json", "rdf"):
            print(f"Skipping {name}: load_mode is '{load_mode}'")
            continue

        api_key = lib_cfg["api_key"]
        lib_type = lib_cfg["library_type"]
        lib_id = lib_cfg["library_id"]
        api_query_params = lib_cfg.get("api_query_params", {})
        headers = {"Zotero-API-Key": api_key}
        base_url = f"https://api.zotero.org/{lib_type}/{lib_id}"

        start = 0
        all_data = []
        base_params = {"format": "json", "limit": LIMIT, **api_query_params}

        while True:
            params = {**base_params, "start": start}
            response = requests.get(f"{base_url}/{endpoint}", headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            if not data:
                break
            all_data.extend(data)
            start += LIMIT

        results.append({
            "base_url":base_url,
            "name": name,
            "load_mode": load_mode,
            "data": all_data,
            "rdf_export_format": lib_cfg.get("rdf_export_format") if load_mode == "rdf" else None
        })

    return results


def build_graph(target_store: Store):
    items_list = fetch_all("items")
    collections_list = fetch_all("collections")

    for lib_items, lib_colls in zip(items_list, collections_list):
        base_url = lib_items["base_url"]
        items = lib_items["data"]
        collections = lib_colls["data"]

        logger.info(f"[{lib_items['name']}] Fetched {len(items)} items and {len(collections)} collections.")

        for col in collections:
            col_data = col["data"]
            col_uri = NamedNode(f"{base_url}/collections/{col_data['key']}")
            target_store.add(Quad(col_uri, NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), NamedNode(f"{ZOT_NS}Collection")))
            for field, value in col_data.items():
                if value:
                    target_store.add(Quad(col_uri, NamedNode(f"{ZOT_NS}{field}"), Literal(str(value))))

        for item in items:
            data = item.get("data", {})
            key = data.get("key")
            node_uri = NamedNode(f"{base_url}/items/{key}")
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

            if any(lib["load_mode"] == "rdf" for lib in ZOTERO_CONFIGS):
                fetch_rdf_export(store)

            if any(lib["load_mode"] == "manual_import" for lib in ZOTERO_CONFIGS):
                import_rdf_directory(store, IMPORT_DIRECTORY)

            if any(lib["load_mode"] == "json" for lib in ZOTERO_CONFIGS):
                build_graph(store)

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