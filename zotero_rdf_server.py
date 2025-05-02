import os
import requests
import yaml
import threading
import time
import tempfile
import logging
import shutil
from uuid import uuid5, NAMESPACE_URL, uuid4
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn
from collections import defaultdict
import json

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
ZOT_NS = ZOTERO_CONFIGS.get("vocab", "http://www.zotero.org/namespaces/export#")
ZOT_API_URL = ZOTERO_CONFIGS.get("api_url", "https://api.zotero.org/")
ZOT_BASE_URL = ZOTERO_CONFIGS.get("base_url", "https://www.zotero.org/")
ZOT_SCHEMA = ZOTERO_CONFIGS.get("schema", "https://api.zotero.org/schema")
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

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
        self.base_api_url = f"{ZOT_API_URL}{self.library_type}/{self.library_id}"
        self.base_url = f"{ZOT_BASE_URL}{self.library_type}/{self.library_id}"
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
    rdf_mapping = map.get("rdf_mapping") or []
    def zotero_property_map(predicate_str: str, object: str | dict | list, map: dict):
        XSD_NS = "http://www.w3.org/2001/XMLSchema#"


        try:
            if object == "" or object == {} or object == [] or object == None or object == [{}]:
                return None
            
            if rdf_mapping and predicate_str not in rdf_mapping: # no mapping if none specified or not specified for mapping
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
            if field not in white and field not in rdf_mapping:
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

def apply_rdf_types(store: Store, node: NamedNode, data: dict, type_fields: list[str], default_type: str, base_ns: str, prefix_ns: str):
    if not type_fields:
        store.add(Quad(node, NamedNode(RDF_TYPE), NamedNode(f"{prefix_ns}{default_type}")))
    else:
        for field in type_fields:
            if field.startswith("_"):
                val = field.lstrip("_")
            else:
                val = data.get(field)
                if not val:
                    continue
            val_str = str(val)
            type_node = NamedNode(val_str) if val_str.startswith("http") else NamedNode(f"{prefix_ns}{val_str}")
            store.add(Quad(node, NamedNode(RDF_TYPE), type_node))

def apply_additional_properties(store: Store, node: NamedNode, data: dict, specs: list[dict], prefix_ns: str):
    for spec in specs:
        property_str = spec.get("property")
        value_spec = spec.get("value")
        named_node = spec.get("named_node", False)

        if not property_str or not value_spec:
            continue

        predicate = NamedNode(property_str) if property_str.startswith("http") else NamedNode(f"{prefix_ns}{property_str}")

        if value_spec.startswith("_"):
            raw_value = value_spec.lstrip("_")
        else:
            raw_value = data.get(value_spec)
            if not raw_value:
                continue

        if named_node:
            obj = NamedNode(str(raw_value)) if str(raw_value).startswith("http") else NamedNode(f"{prefix_ns}{raw_value}")
        else:
            obj = Literal(str(raw_value))

        store.add(Quad(node, predicate, obj))


def build_graph_for_library(lib: ZoteroLibrary, store: Store):
    items = lib.fetch_items()
    collections = lib.fetch_collections()
    logger.info(f"[{lib.name}] Fetched {len(items)} items and {len(collections)} collections.")
    map = lib.map

    for col in collections:
        col_data = col["data"]
        key = col_data.get("key", uuid4())
        col_uri = NamedNode(f"{lib.base_url}/collections/{key}")

        collection_type_fields = map.get("collection_type") or []
        apply_rdf_types(store, col_uri, col_data, collection_type_fields, "Collection", lib.base_url, ZOT_NS)

        collection_additional = map.get("collection_additional") or []
        apply_additional_properties(store, col_uri, col_data, collection_additional, ZOT_NS)

        add_rdf_from_dict(store, col_uri, col_data, ZOT_NS, lib.base_url, map)

    for item in items:
        item_data = item.get("data", {})
        key = item_data.get("key",uuid4())
        item_type_fields = lib.map.get("item_type") or []
        node_uri = NamedNode(f"{lib.base_url}/items/{key}")
        apply_rdf_types(store, node_uri, item_data, item_type_fields, "Item", lib.base_url, ZOT_NS)

        item_additional = map.get("item_additional") or []
        apply_additional_properties(store, node_uri, item_data, item_additional, ZOT_NS)

        add_rdf_from_dict(store, node_uri, item_data, ZOT_NS, lib.base_url, map)

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

@router.get("/schema")
async def schema():

    def extract_labels_from_locales(locales):
        class_labels = defaultdict(list)
        property_labels = defaultdict(list)

        for lang, content in locales.items():
            # Klassen: itemTypes + creatorTypes
            for class_id, label in content.get("itemTypes", {}).items():
                class_labels[class_id].append({"@value": label, "@language": lang})
            for class_id, label in content.get("creatorTypes", {}).items():
                class_labels[class_id].append({"@value": label, "@language": lang})

            # Properties: fields
            for prop_id, label in content.get("fields", {}).items():
                property_labels[prop_id].append({"@value": label, "@language": lang})

        return class_labels, property_labels
    def generate_schema_jsonld(ontology, class_labels, property_labels):
        context = {
            "owl": "http://www.w3.org/2002/07/owl#",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "zot": "http://example.org/zotero#"
        }

        def format_union(entities):
            if len(entities) == 1:
                return {"@id": f"zot:{entities[0]}"}
            return {
                "@type": "owl:Class",
                "owl:unionOf": [{"@id": f"zot:{e}"} for e in sorted(entities)]
            }

        jsonld = {
            "@context": context,
            "@graph": []
        }

        for cls in ontology["classes"]:
            entry = {
                "@id": f"zot:{cls['class']}",
                "@type": "owl:Class"
            }
            if cls["class"] in class_labels:
                entry["rdfs:label"] = class_labels[cls["class"]]
            jsonld["@graph"].append(entry)

        for prop in ontology["datatypeProperties"]:
            entry = {
                "@id": f"zot:{prop['property']}",
                "@type": "owl:DatatypeProperty",
                "rdfs:domain": format_union(prop["domains"])
            }
            if "equivalentProperty" in prop:
                entry["owl:equivalentProperty"] = {"@id": f"zot:{prop['equivalentProperty']}"}
            if prop["property"] in property_labels:
                entry["rdfs:label"] = property_labels[prop["property"]]
            jsonld["@graph"].append(entry)

        for obj_prop in ontology["objectProperties"]:
            entry = {
                "@id": f"zot:{obj_prop['property']}",
                "@type": "owl:ObjectProperty",
                "rdfs:domain": format_union([obj_prop["domain"]]),
                "rdfs:range": format_union(obj_prop["range"])
            }
            if obj_prop["property"] in property_labels:
                entry["rdfs:label"] = property_labels[obj_prop["property"]]
            jsonld["@graph"].append(entry)

        return jsonld
    def zotero_schema_to_ontology_json(zotero_json):
        classes = set()
        datatype_properties = defaultdict(lambda: {"domains": set(), "equivalentProperty": None})
        creator_classes = set()
        creator_property_relations = defaultdict(set)

        for item_type in zotero_json.get("itemTypes", []):
            item_type_name = item_type["itemType"]
            classes.add(item_type_name)

            for field in item_type.get("fields", []):
                field_name = field["field"]
                datatype_properties[field_name]["domains"].add(item_type_name)
                if "baseField" in field:
                    datatype_properties[field_name]["equivalentProperty"] = field["baseField"]

            for creator in item_type.get("creatorTypes", []):
                creator_type = creator["creatorType"]
                creator_classes.add(creator_type)
                creator_property_relations[item_type_name].add(creator_type)

        ontology = {
            "classes": sorted([{"class": cls} for cls in classes.union(creator_classes)], key=lambda x: x["class"]),
            "datatypeProperties": [],
            "objectProperties": []
        }

        for prop, details in sorted(datatype_properties.items()):
            entry = {
                "property": prop,
                "domains": sorted(details["domains"])
            }
            if details["equivalentProperty"]:
                entry["equivalentProperty"] = details["equivalentProperty"]
            ontology["datatypeProperties"].append(entry)

        for item_type, creator_types in sorted(creator_property_relations.items()):
            ontology["objectProperties"].append({
                "property": "creators",
                "domain": item_type,
                "range": sorted(creator_types)
            })

        return ontology
    
    def load_zotero_schema_from_api():
        response = requests.get(ZOT_SCHEMA)
        response.raise_for_status()
        return response.json()

    zotero_schema = load_zotero_schema_from_api()
    ontology = zotero_schema_to_ontology_json(zotero_schema)
    class_labels, property_labels = extract_labels_from_locales(zotero_schema["locales"])
    jsonld = generate_schema_jsonld(ontology, class_labels, property_labels)
    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)
    path = os.path.join(EXPORT_DIRECTORY, "zotero_schema_ontology.jsonld")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(jsonld, f, ensure_ascii=False, indent=2)
    
    return FileResponse(path, media_type="application/ld+json", filename=os.path.basename(path))
# --- Start server ---

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    initialize_store()
    threading.Thread(target=refresh_store, daemon=True).start()
    yield

app = FastAPI(lifespan=app_lifespan)
app.include_router(router)