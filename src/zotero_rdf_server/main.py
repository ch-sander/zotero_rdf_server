import os
import requests
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import ReadTimeout, RequestException
import yaml
import threading
import time
import tempfile
import logging
import asyncio
import shutil
from uuid import uuid5, NAMESPACE_URL, uuid4
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode, DefaultGraph
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter
# from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
import uvicorn
from collections import defaultdict
import json, re
from datetime import datetime, timezone
from dateutil import parser
from pathlib import Path
from rapidfuzz import fuzz, process
from urllib.parse import quote, urlparse
from enum import Enum

# --- Load configuration ---
config_path = os.getenv("CONFIG_FILE", "config.yaml")
zotero_config_path = os.getenv("ZOTERO_CONFIG_FILE", "zotero.yaml")

with open(config_path, "r") as f:
    config = yaml.safe_load(f)

with open(zotero_config_path, "r") as f:
    zotero_config = yaml.safe_load(f)

config = config or {}
zotero_config = zotero_config or {}

log_level = config["server"].get("log_level", "info").upper()
from zotero_rdf_server.logging_config import logger, setup_logging
setup_logging(log_level)

# --- Config ---
REFRESH_INTERVAL = config["server"].get("refresh_interval", 0)
DELAY = config["server"].get("delay", 60)
STORE_MODE = "directory"
STORE_DIRECTORY = config["server"].get("store_directory", "/app/data")
EXPORT_DIRECTORY = config["server"].get("export_directory", "/app/exports")
IMPORT_DIRECTORY = config["server"].get("import_directory", "/app/import")
BACKUP_DIRECTORY = config["server"].get("backup_directory", "/app/backup")

LIMIT = 100

REFRESH = REFRESH_INTERVAL >= 0

if REFRESH_INTERVAL >= 30:
    logger.info(f"Refresh set to {REFRESH_INTERVAL} seconds")
elif REFRESH_INTERVAL == -1:
    logger.info("Refresh deactivated")
elif REFRESH_INTERVAL == 0:
    logger.info("Refresh only at startup")
else:
    logger.info("Refresh interval incorrect and refresh disabled! A minimum of 30 seconds is required!")

def set_defaults(lib_cfg: dict, master_cfg: dict, mode: str = "default", merge_keys: list = None) -> dict:
    merged = lib_cfg.copy()
    for key, value in master_cfg.items():
        if key not in merged:
            merged[key] = value
        elif mode == "override":
            merged[key] = value
        elif mode == "merge":
            if merge_keys and key in merge_keys and isinstance(value, dict) and isinstance(merged[key], dict):
                merged[key] = set_defaults(merged[key], value, mode="merge", merge_keys=merge_keys)
    return merged

# --- Zotero Config ----
ZOTERO_DEFAULT_CONFIGS = zotero_config.get("defaults", {})
ZOTERO_DEFAULT_MODE = ZOTERO_DEFAULT_CONFIGS.get("mode", "default")
ZOTERO_CONFIGS = zotero_config.get("context", {})

ZOTERO_LIBRARIES_CONFIGS = [
    set_defaults(lib_cfg, ZOTERO_DEFAULT_CONFIGS, ZOTERO_DEFAULT_MODE)
    for lib_cfg in zotero_config.get("libraries", [])
]

# --- Constants ---
ZOT_NS = ZOTERO_CONFIGS.get("vocab", "http://www.zotero.org/namespaces/export#")
ZOT_API_URL = ZOTERO_CONFIGS.get("api_url", "https://api.zotero.org/")
ZOT_BASE_URL = ZOTERO_CONFIGS.get("base_url", "https://www.zotero.org/")
ZOT_SCHEMA = ZOTERO_CONFIGS.get("schema") # "https://api.zotero.org/schema"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
XSD_NS = "http://www.w3.org/2001/XMLSchema#"
SKOS_ALT = "http://www.w3.org/2004/02/skos/core#altLabel"
PREFIXES = {"zot":ZOT_NS, "rdfs":"http://www.w3.org/2000/01/rdf-schema#", "owl":"http://www.w3.org/2002/07/owl#", "rdf":"http://www.w3.org/1999/02/22-rdf-syntax-ns#", "xsd":XSD_NS, "skos":"http://www.w3.org/2004/02/skos/core#"}

LANG_MAP = {
                "de": ["deutsch", "german", "allemand", "alemán", "tedesco", "deu", "ger", "de"],
                "en": ["englisch", "english", "anglais", "inglés", "inglese", "eng", "en"],
                "fr": ["französisch", "french", "français", "francese", "fre", "fra", "fr"],
                "it": ["italienisch", "italian", "italien", "italiano", "ita", "it"],
                "es": ["spanisch", "spanish", "español", "espanol", "esp", "spa", "es"],
                "la": ["latein", "latin", "latino", "lat", "la"],
                "pt": ["portugiesisch", "portuguese", "português", "por", "pt"],
                "ru": ["russisch", "russian", "русский", "rus", "ru"],
                "ja": ["japanisch", "japanese", "日本語", "jpn", "ja"],
                "zh": ["chinesisch", "chinese", "中文", "漢語", "汉语", "chi", "zho", "zh"],
                "ar": ["arabisch", "arabic", "العربية", "ara", "ar"],
                "default": "und" # used if none found
            }


# --- App ---
router = APIRouter()
store = None

# --- Class ---

class ZoteroLibrary:
    def __init__(self, config: dict):
        self.name = config["name"]
        self.load_mode = config.get("load_mode", "json")
        self.library_type = config.get("library_type", None)
        self.library_id = config.get("library_id", None)
        self.api_key = config.get("api_key", None)
        self.rdf_export_format = config.get("rdf_export_format", "rdf_zotero")
        self.api_query_params = config.get("api_query_params") or {}
        self.base_api_url = f"{ZOT_API_URL}{self.library_type}/{self.library_id}".strip("#/")
        self.base_url = str(config.get("base_uri", f"{ZOT_BASE_URL}{self.library_type}/{self.library_id}")).strip("/#")
        self.knowledge_base_graph = str(config.get("knowledge_base_graph", self.base_url)).strip("/#")
        self.load_from = str(config.get("load_from",os.path.join(IMPORT_DIRECTORY, self.name))).replace("$",str(self.library_id))
        self.save_to = config.get("save_to")
        if self.save_to:
            self.save_to = str(self.save_to).replace("$",str(self.library_id))
        self.headers = {"Zotero-API-Key": self.api_key} if self.api_key else {}
        self.map = config.get("map") or {}
        self.parser = config.get("notes_parser") or {}
        # check settings

        passing = True
        if not any([str(self.base_url).startswith("http"),str(self.base_api_url).startswith("http"),str(self.knowledge_base_graph).startswith("http")]):
            passing = False
            logger.warning(f"{self.name}: Some library config variable is expected to be a IRI/URI but is not!")
        if not str(self.library_id).isdigit() and not self.library_type == "knowledge base":
            passing = False
            logger.error(f"{self.name}: Invalid library ID --> {type(self.library_id)}!")
        if not self.load_mode in ["json", "rdf", "manual_import"]:
            passing = False
            logger.warning(f"{self.name}: Invalid load_mode {self.load_mode}!")
        if not self.library_type in ["groups", "user", "knowledge base"]:
            passing = False
            logger.error(f"{self.name}: Invalid library_type {self.library_type}!")
        if not self.rdf_export_format in ["rdf_zotero", "rdf_bibliontology"] and self.load_mode == "rdf":
            passing = False
            logger.warning(f"{self.name}: Invalid rdf_export_format {self.rdf_export_format}!")
        if any([(self.name and not isinstance(self.name,str)),(self.api_key and not isinstance(self.api_key,str)),(self.map and not isinstance(self.map,dict)),(self.api_query_params and not isinstance(self.api_query_params,dict)),(self.map.get("white") and not isinstance(self.map["white"],list))]):
            passing = False
            logger.warning(f"{self.name}: Invalid optional argument!")

        if not passing:
            logger.error(f"####################################################")
            logger.error(f"####################################################")
            logger.error(f"####################################################")
            logger.error(f"{self.name}: Problematic library config, check warnings!")
            logger.error(f"####################################################")
            logger.error(f"####################################################")
            logger.error(f"####################################################")
        else:
            logger.info(f"{self.name}: Valid library config!") 

    def fetch_paginated(self, endpoint: str) -> list:
        results = []
        start = 0
        logger.info("Initialize session")

        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retries)

        with requests.Session() as session:
            session.mount("https://", adapter)
            session.mount("http://", adapter)

            while True:
                params = {
                    "format": "json",
                    "limit": LIMIT,
                    "start": start,
                    **self.api_query_params
                }
                req = requests.Request(
                    method="GET",
                    url=f"{self.base_api_url}/{endpoint}",
                    headers=self.headers,
                    params=params
                )
                prepared = req.prepare()
                logger.debug(f"Sending API request: {prepared.method} {prepared.url}")
                for k, v in prepared.headers.items():
                    logger.debug(f"Header: {k}: {v}")

                try:
                    response = session.send(prepared, timeout=(5, 30))
                    response.raise_for_status()
                    data = response.json()
                except ReadTimeout:
                    logger.error(f"Timeout after 30s at {prepared.url}")
                    raise
                except RequestException as e:
                    logger.error(f"Request error: {e}")
                    raise

                if not data:
                    logger.info(f"No more data (start={start})")
                    break

                results.extend(data)
                logger.info(f"Fetched {len(data)} items (start={start})")
                start += LIMIT

                time.sleep(1)

        return results

    def fetch_items(self, json_path:str = None) -> list:
        if self.load_mode == "manual_import":
            if not json_path or not os.path.isfile(json_path):
                raise FileNotFoundError(f"JSON path not found: {json_path}")

            with open(json_path, "r", encoding="utf-8") as f:
                items = json.load(f)

            if not isinstance(items, list):
                raise ValueError(f"Expected list of items in JSON file, got {type(items).__name__}")

            return items
        elif self.load_mode == "json":
            return self.fetch_paginated("items")
        else:
            return None

    def fetch_collections(self, json_path:str = None) -> list:
        if self.load_mode == "manual_import":
            if not json_path or not os.path.isfile(json_path):
                raise FileNotFoundError(f"JSON path not found: {json_path}")

            with open(json_path, "r", encoding="utf-8") as f:
                cols = json.load(f)

            if not isinstance(cols, list):
                raise ValueError(f"Expected list of collections in JSON file, got {type(cols).__name__}")

            return cols
        if self.load_mode == "json":
            return self.fetch_paginated("collections")
        else:
            return None

    def fetch_rdf_export(self) -> bytes:
        params = {"format": self.rdf_export_format, "limit": LIMIT, **self.api_query_params}
        response = requests.get(f"{self.base_api_url}/items", headers=self.headers, params=params)
        response.raise_for_status()
        return response.content  # RDF XML as Bytes


# --- Functions ---


def safeNamedNode(uri: str, enforce: bool = True) -> NamedNode | Literal:
    INTERNAL_IRI_PREFIX = "http://internal.invalid/"
    if not isinstance(uri, str):
        logger.warning(f"Invalid IRI input (not a string), converting to Literal or synthetic IRI: {uri}")
        if enforce:
            fallback = quote(str(uri), safe="")
            return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
        return safeLiteral(uri)

    parsed = urlparse(uri)
    if not parsed.scheme:
        logger.warning(f"Invalid IRI input (missing scheme), converting to Literal or synthetic IRI: {uri}")
        if enforce:
            fallback = quote(uri, safe="")
            return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
        return safeLiteral(uri)

    try:
        safe_iri = quote(uri, safe=':/#?&=%')
        return NamedNode(safe_iri)
    except ValueError as e:
        logger.warning(f"Invalid IRI converted to Literal or synthetic IRI: {uri} – {e}")
        if enforce:
            fallback = quote(uri, safe="")
            return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
        return safeLiteral(uri)

def safeLiteral(value) -> Literal:
    try:
        return Literal(str(value))
    except Exception as e:
        logger.error(f"Literal creation failed for value '{value}': {e} – using fallback 'n/a'")
        return Literal("n/a")

def fuzzy_match_label(store:Store, label:str, type_node:NamedNode, threshold=90, graph_name:NamedNode = None, predicates:list = [SKOS_ALT], test=False):
    best_score = 0
    best_match = None
    best_label = None
    logger.debug(f"Fuzzy matching '{label}' against existing {type_node} labels (threshold: {threshold})")
    if test:
        logger.info(f"### {label} a {type_node}, look in {predicates}, in {graph_name}, found...")
        candidates = list(store.quads_for_pattern(
            None,
            NamedNode(RDF_TYPE),
            type_node,
            graph_name=graph_name
        ))
        logger.info("→ finde %d Instanzen von %s im Graph %s", 
                    len(candidates), type_node, graph_name)
        for c in candidates:
            logger.info("   → %s", c.subject)


    for quad in store.quads_for_pattern(None, NamedNode(RDF_TYPE), type_node, graph_name=graph_name):
        subject = quad.subject
        for pred in predicates: # [SKOS_ALT, RDFS_LABEL] Not really needed as every label should also be a altLabel

            if test:
                labels = list(store.quads_for_pattern(
                    subject,
                    NamedNode(pred),
                    None,
                    #graph_name=graph_name
                ))
                logger.info("→ altLabels auf %s via %s: %r", subject, pred, labels)

            for label_quad in store.quads_for_pattern(
                subject, 
                NamedNode(pred), 
                None, 
                graph_name=graph_name
                ):
                existing_label = str(label_quad.object.value)
                score = fuzz.ratio(existing_label.lower(), label.lower())
                logger.debug(f"Compared '{label}' with '{existing_label}' → score: {score}")
                if score > best_score:
                    best_score = score
                    best_match = subject
                    best_label = existing_label

   
    if best_score >= threshold:
        logger.debug(f"Best match: {best_match} with label '{best_label}' (score: {best_score})")
        return best_match, best_score, best_label
    else:
        logger.debug("No fuzzy match found above threshold.")
        return None, 0, None

def process_language_and_title(
    title: str | None,
    language_field: str | None = "default",
    mapping: dict = LANG_MAP
) -> Literal:
    normalized = language_field.strip().lower() if isinstance(language_field, str) else ""
    for code, variants in mapping.items():
        if code == "default":
            continue
        if normalized and normalized in variants:
            return Literal(title, language=code) if title else Literal(code)
    fallback = mapping.get("default", "und")
    return Literal(title, language=fallback) if title else Literal(language_field)

def import_rdf_from_disk(lib: ZoteroLibrary, store: Store):

    subdir = lib.load_from if lib.load_from else os.path.join(IMPORT_DIRECTORY, lib.name)
    if not os.path.isdir(subdir):
        logger.warning(f"Directory not found for manual import: {subdir}")
        return

    logger.info(f"Importing RDF files for '{lib.name}' from {subdir} to {lib.base_url}")
    for filename in os.listdir(subdir):
        logger.info(f"Found: {filename}")
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
        elif filename.endswith(".json"): # call for JSON
            json_path = os.path.join(subdir, filename)
            build_graph_for_library(lib, store, json_path=json_path)
            fmt = None
        else:
            logger.info(f"Skipping unsupported file: {filename}")
            continue
        
        before = len(store)
        if fmt:
            store.bulk_load(path=filepath, format=fmt, base_iri=f"{lib.base_url}/items/", to_graph=NamedNode(lib.base_url))
        after = len(store)
        logger.info(f"Imported {after - before} triples from {filename}")


def add_rdf_from_dict(store: Store, subject: NamedNode | BlankNode, data: dict, ns_prefix: str, base_uri: str, map: dict, knowledge_base_graph: str = None, language: str = None):
    GRAPH_URI = safeNamedNode(base_uri)
    
    if knowledge_base_graph is None:
        knowledge_base_graph = base_uri

    knowledge_base_graph=knowledge_base_graph
    ENTITY_GRAPH_URI = safeNamedNode(knowledge_base_graph)

    ENTITY_UUID = uuid5(NAMESPACE_URL, knowledge_base_graph)
    white = map.get("white") or []
    black = map.get("black") or []
    lang_map = map.get("language_map", LANG_MAP)
    rdf_mapping = map.get("rdf_mapping") or []
    fuzzy_threshold = map.get("fuzzy", 90)
    def zotero_property_map(predicate_str: str, object: str | dict | list, map: dict):

        def parse_date(text, dayfirst=True):
            text = text.strip()
            RANGE_SEPARATORS = r"\s*[-–—]\s*"
            if re.search(RANGE_SEPARATORS, text):
                parts = re.split(RANGE_SEPARATORS, text)
                if len(parts) == 2:
                    try:
                        start = parser.parse(parts[0], dayfirst=dayfirst, default=datetime(1,1,1))
                        end = parser.parse(parts[1], dayfirst=dayfirst, default=datetime(1,1,1))
                        # return (start, end)
                        return start
                    except Exception:
                        return text
            try:
                return parser.parse(str(text), dayfirst=dayfirst, default=datetime(1, 1, 1))
            except (ValueError, TypeError):
                return text
        def make_entity(object_value,my_type,):
            # Normalize and split values
            value = object_value.strip()
            items = [p.strip() for p in re.split(r"[;]", value) if p.strip()] # Do not split on comma!

            for item in items:
                node, score, matched_label = fuzzy_match_label(
                    store,
                    item,
                    type_node=NamedNode(f"{ns_prefix}{my_type}"),
                    threshold=fuzzy_threshold,
                    graph_name=ENTITY_GRAPH_URI
                )

                if not node:
                    iri_suffix = uuid5(ENTITY_UUID, item)
                    node = safeNamedNode(f"{knowledge_base_graph}/{my_type}/{iri_suffix}")
                    store.add(Quad(node, NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}{my_type}"), graph_name=ENTITY_GRAPH_URI))
                    store.add(Quad(node, NamedNode(RDFS_LABEL), Literal(item), graph_name=ENTITY_GRAPH_URI))

                    logger.debug(f"Created new {my_type}: {item}")
                else:
                    logger.debug(f"{my_type.capitalize()} '{item}' matched as '{matched_label}' (score {score})")

                alts = {(q.object.value).lower() for q in store.quads_for_pattern(node, NamedNode(SKOS_ALT), None, graph_name=ENTITY_GRAPH_URI)}
                if item.lower() not in alts:
                    store.add(Quad(node, NamedNode(SKOS_ALT), Literal(item), graph_name=ENTITY_GRAPH_URI))
                pred_node = safeNamedNode(f"{ns_prefix}{predicate_str}")
                store.add(Quad(subject, pred_node, node, graph_name=GRAPH_URI))

            return None
        
        try:
            if not object:
                return None
            
            if rdf_mapping and predicate_str not in rdf_mapping: # no mapping if none specified or predicate not specified for mapping
                return None if isinstance(object, dict) else Literal(str(object))
            predicate_node = NamedNode(f"{ns_prefix}{predicate_str}")
            if isinstance(object, dict): # dicts as named nodes
                
                ### TAGS ###

                if predicate_str == "tags" and "tag" in object: # tags
                    tag_value = object["tag"]
                    tag_iri = uuid5(ENTITY_UUID, tag_value)
                    tag_node = NamedNode(f"{knowledge_base_graph}/tag/{tag_iri}")
                    store.add(Quad(subject, NamedNode(f"{ns_prefix}tags"), tag_node, graph_name=GRAPH_URI))                    
                    if not any (store.quads_for_pattern(tag_node, NamedNode(RDF_TYPE), NamedNode(f"{ns_prefix}tag"), graph_name=ENTITY_GRAPH_URI)):
                        store.add(Quad(tag_node, NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}tag"), graph_name=ENTITY_GRAPH_URI))
                        store.add(Quad(tag_node, NamedNode(RDFS_LABEL), Literal(tag_value), graph_name=ENTITY_GRAPH_URI))
                        logger.debug(f"Tag added: {tag_value}")
                        for key, val in object.items():
                            if val:
                                pred = NamedNode(f"{ns_prefix}{key}")                                
                                store.add(Quad(tag_node, pred, Literal(str(val)), graph_name=ENTITY_GRAPH_URI))
                                
                    else:
                        logger.debug(f"Tag already exists: {tag_value}")              
                    return None
                
                ### CREATORS ###

                if predicate_str == "creators":
                    if "name" in object:
                        label = object["name"]
                    else:
                        label = f"{object.get('lastName', '')}, {object.get('firstName', '')}"

                    bnode = BlankNode()
                    store.add(Quad(subject, predicate_node, bnode, graph_name=GRAPH_URI))                    
                    store.add(Quad(bnode, NamedNode(RDF_TYPE), NamedNode(f"{ns_prefix}creatorRole"), graph_name=GRAPH_URI))
                    creator_node, score, matched_label = fuzzy_match_label(store, label, type_node=NamedNode(f"{ns_prefix}person"), threshold=fuzzy_threshold, graph_name=ENTITY_GRAPH_URI)
                    if not creator_node:
                        creator_uuid = uuid5(ENTITY_UUID, label)
                        creator_node = safeNamedNode(f"{knowledge_base_graph}/person/{creator_uuid}")
                        
                        store.add(Quad(creator_node, NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}person"), graph_name=ENTITY_GRAPH_URI))
                        
                        store.add(Quad(creator_node, NamedNode(RDFS_LABEL), Literal(str(label)), graph_name=ENTITY_GRAPH_URI))

                        logger.debug(f"Creator added: {label}")
                        for key, val in object.items():
                            if key != "creatorType" and val:
                                pred = safeNamedNode(f"{ns_prefix}{key}")
                                store.add(Quad(creator_node, pred, Literal(str(val)), graph_name=ENTITY_GRAPH_URI))       
                            elif key == "creatorType" and val:
                                store.add(Quad(bnode, NamedNode(RDFS_LABEL), Literal(str(val)), graph_name=GRAPH_URI))
                                store.add(Quad(bnode, safeNamedNode(f"{ns_prefix}{key}"), safeNamedNode(f"{ns_prefix}{val}"), graph_name=GRAPH_URI))
                                store.add(Quad(bnode, NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}{val}"), graph_name=GRAPH_URI))
                    else:
                        logger.debug(f"Creator already exists: {label} as {matched_label} ({score})")

                    alts = {(q.object.value).lower() for q in store.quads_for_pattern(creator_node, NamedNode(SKOS_ALT), None, graph_name=ENTITY_GRAPH_URI)}
                    if label.lower() not in alts:
                        store.add(Quad(creator_node, NamedNode(SKOS_ALT), Literal(label), graph_name=ENTITY_GRAPH_URI))

                    store.add(Quad(bnode, NamedNode(f"{ns_prefix}hasCreator"), creator_node, graph_name=GRAPH_URI))
                    return None

            ### DATATYPES ###

            elif isinstance(object, (str, int, datetime, float)):
                val = str(object)
                logger.debug(f"{predicate_str}: {type(object)} {val[:100] + ('...' if len(val) > 100 else '')}")           

                # ZOTERO Links #
                if predicate_str == "collections": # collections
                    return safeNamedNode(f"{base_uri}/collections/{object}")
                if predicate_str in ["parentItem"]: # parent items
                    return safeNamedNode(f"{base_uri}/items/{object}")
                if predicate_str in ["parentCollection"]: # parent collections
                    return safeNamedNode(f"{base_uri}/collections/{object}")
                
                # TITLE and LANGUAGE #
                elif isinstance(object, (str)) and predicate_str in ["title","bookTitle"] and language:
                    process_language_and_title(title=object,language_field="en",mapping=lang_map)
                elif isinstance(object, (str)) and predicate_str in ["language"] and language:
                    process_language_and_title(title=None, language_field="en",mapping=lang_map)

                # URL #
                elif predicate_str in ["url","dc:relation","doi","owl:sameAs"] and object.startswith("http"): # url
                    vals = [object.strip()] #for v in object.split(",")] # TODO no splitting or URLs!
                    for val in vals:
                        if len(vals)>1:
                            logger.debug(f"Parse Multi-URL for {subject}: {val}") 
                        store.add(Quad(subject, predicate_node, safeNamedNode(val, enforce=False), graph_name=GRAPH_URI))

                    return None
                
                # DOI #
                elif predicate_str in ["doi"] and not object.startswith("http") and len(object)>5:
                    return safeNamedNode(f"https://doi.org/{str(object)}".strip())
                
                # INT #
                elif predicate_str in ["numPages","numberOfVolumes","volume","series number"] and str(object).isdigit(): # int
                    return Literal(str(object),datatype=NamedNode(f"{XSD_NS}int"))
                
                # DATE #
                elif predicate_str == "date":
                    date_val = parse_date(str(object))
                    match = re.search(r"\b(1[5-9]\d{2}|20\d{2}|2100)\b", str(object))
                    if re.fullmatch(r"\d{4}", str(object)):
                        return Literal(str(object), datatype=NamedNode(f"{XSD_NS}gYear"))
                    elif match:
                        return Literal(match.group(1), datatype=NamedNode(f"{XSD_NS}gYear"))
                    elif isinstance(date_val, datetime):                        
                        return Literal(str(date_val.date().isoformat()), datatype=NamedNode(f"{XSD_NS}dateTime"))
                    else:
                        return Literal(str(object))
                    
                elif predicate_str in ["dateModified","accessDate","dateAdded"]: # dateTime
                    return Literal(str(object),datatype=NamedNode(f"{XSD_NS}dateTime"))
                
                # ENTITY #
                elif isinstance(object, str) and ((not rdf_mapping and predicate_str in ["place","publisher","series"]) or predicate_str in rdf_mapping):
                    logger.debug(f"UUID Entity for {predicate_str}: {object}")
                    make_entity(object,predicate_str)
                    return None
                
                # LITERAL #
                else:
                    return Literal(str(object))
                
            else:
                logger.error(f"Error: pass dict or str but got {type(object)}: {object}")

        except Exception as e:
            logger.error(f"Error: {e}")
            return None
        
    #############################################
    ######## main function starts here! #########
    #############################################

    for field, value in data.items():
        try:
            predicate = safeNamedNode(f"{ns_prefix}{field}")

            if white:
                if field not in white and field not in rdf_mapping:
                    logger.debug(f"Skipping {field} (not in whitelist)")
                    continue
            elif black and field in black:
                logger.debug(f"Skipping {field} (in blacklist)")
                continue
            
            if isinstance(value, dict):
                obj = zotero_property_map(field, value, map)
                if obj is None:
                    continue
                bnode = BlankNode()
                store.add(Quad(subject, predicate, bnode, graph_name=GRAPH_URI))
                add_rdf_from_dict(store, bnode, value, ns_prefix, base_uri, map, knowledge_base_graph)

            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        if zotero_property_map(field, item, map) is None:
                            continue
                        bnode = BlankNode()
                        store.add(Quad(subject, predicate, bnode, graph_name=GRAPH_URI))
                        add_rdf_from_dict(store, bnode, item, ns_prefix, base_uri, map, knowledge_base_graph)
                    else:
                        obj = zotero_property_map(field, item, map)
                        if obj is not None:
                            store.add(Quad(subject, predicate, obj, graph_name=GRAPH_URI))

            elif value is not None:
                obj = zotero_property_map(field, value, map)
                if obj is not None:
                    store.add(Quad(subject, predicate, obj, graph_name=GRAPH_URI))
        except Exception as e:
            logger.error(f"Invalid data for: [{field}, {value}]")
            continue        

def apply_rdf_types(store: Store, node: NamedNode, data: dict, type_fields: list[str], default_type: str, base_ns: str, prefix_ns: str):
    GRAPH_URI = NamedNode(base_ns)
    RDF_TYPE_NODE = NamedNode(RDF_TYPE)

    if not type_fields:
        default_node = NamedNode(f"{prefix_ns}{default_type}")
        store.add(Quad(node, RDF_TYPE_NODE, default_node, graph_name=GRAPH_URI))
        logger.debug(f"No type_fields for rdf:type – added default: {default_node}")
    else:
        for field in type_fields:
            if field.startswith("_"):
                raw_val = field.lstrip("_")
            else:
                raw_val = data.get(field)
                if not raw_val:
                    continue

            try:
                val_strs = [v.strip() for v in str(raw_val).split(",")]
                if len(val_strs) > 1:
                    logger.debug(f"Multiple rdf:type values for {node}: {val_strs}")

                for val_str in val_strs:
                    type_node = (
                        safeNamedNode(val_str)
                        if val_str.startswith("http")
                        else safeNamedNode(f"{prefix_ns}{val_str}")
                    )
                    store.add(Quad(node, RDF_TYPE_NODE, type_node, graph_name=GRAPH_URI))
                    logger.debug(f"Added rdf:type: {type_node}")

            except Exception as e:
                logger.error(f"Invalid rdf:type at {node} for value '{raw_val}': {e}")
                continue

def apply_additional_properties(store: Store, node: NamedNode, data: dict, specs: list[dict], base_ns: str, prefix_ns: str):
    GRAPH_URI = NamedNode(base_ns)
    for spec in specs:
        try:
            property_str = spec.get("property")
            value_spec = spec.get("value")
            named_node = spec.get("named_node", False)

            if not property_str or not value_spec:
                continue

            predicate = safeNamedNode(property_str) if property_str.startswith("http") else safeNamedNode(f"{prefix_ns}{property_str}")

            if value_spec.startswith("_"):
                raw_value = value_spec.lstrip("_")
            else:
                raw_value = data.get(value_spec)
                if not raw_value:
                    continue

            if named_node:
                obj = safeNamedNode(raw_value,enforce=False)
                store.add(Quad(node, predicate, obj, graph_name=GRAPH_URI))
                continue
    
            obj = Literal(str(raw_value))

            store.add(Quad(node, predicate, obj, graph_name=GRAPH_URI))
        except Exception as e:
            logger.error(f"Invalid data at {node} for {raw_value}")
            continue

def add_timestamp(store: Store, node: NamedNode, graph: NamedNode):
    store.add(Quad(node, NamedNode("http://www.w3.org/ns/prov#generatedAtTime"), Literal(datetime.now(timezone.utc).isoformat(),datatype=NamedNode(f"{XSD_NS}dateTime")), graph_name=graph))

def library_href(library_meta: dict):
    return (
        library_meta.get("library", {})
        .get("links", {})
        .get("alternate", {})
        .get("href")
    )


def build_graph_for_library(lib: ZoteroLibrary, store: Store, json_path:str = None):    
    json_path_items = None
    json_path_collections = None

    if json_path:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                preview = json.load(f)
                if not isinstance(preview, list):
                    raise ValueError(f"Expected a list in JSON file: {json_path}")
                if all("data" in e and "itemType" in e["data"] for e in preview):
                    json_path_items = json_path
                elif all("data" in e and "name" in e["data"] for e in preview):
                    json_path_collections = json_path
                else:
                    raise ValueError(f"Could not classify JSON as items or collections: {json_path}")
        except Exception as e:
            logger.error(f"Error reading or classifying JSON file {json_path}: {e}")
            return

    collections = []
    items = []

    try:
        if not json_path_collections:
            items = lib.fetch_items(json_path=json_path_items)
    except Exception as e:
        logger.warning(f"Could not fetch items for {lib.library_id}: {e}")

    try:
        if not json_path_items:
            collections = lib.fetch_collections(json_path=json_path_collections)
    except Exception as e:
        logger.warning(f"Could not fetch collections for {lib.library_id}: {e}")
        
    #if log_level=="DEBUG":
    if lib.save_to:
        try:
            path = lib.save_to #.join(EXPORT_DIRECTORY, "Zotero JSON", lib.name)
            os.makedirs(path, exist_ok=True)
            if items:
                with open(os.path.join(path, f"{lib.library_id}_items.json"), "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2)
            if collections:
                with open(os.path.join(path, f"{lib.library_id}_collections.json"), "w", encoding="utf-8") as f:
                    json.dump(collections, f, ensure_ascii=False, indent=2)        
            logger.info(f"Stored JSON for {lib.library_id} in {path}")
        except Exception as e:
            logger.error(f"Error saving JSON for {lib.library_id} to {lib.save_to}: {e}")

    map = lib.map
    sample_entry = (items or collections or [None])[0]

    if sample_entry is not None:
        a_library_href = library_href(sample_entry) or lib.base_url
        logger.debug(f"Example JSON: {sample_entry}")
    else:
        a_library_href = lib.base_url
        logger.warning(f"No items or collections found for library {lib.name}")

    logger.info(f"[{lib.name} at {a_library_href}] Fetched {len(items) if items else 0} items and {len(collections) if collections else 0} collections.")

    GRAPH_URI = safeNamedNode(lib.base_url)

    if lib.map.get("named_library") and sample_entry and sample_entry.get("library"):
        store.add(Quad(safeNamedNode(a_library_href), NamedNode(RDF_TYPE), safeNamedNode(f"{ZOT_NS}library"), graph_name=GRAPH_URI))
        add_rdf_from_dict(
            store,
            safeNamedNode(a_library_href),
            sample_entry["library"],
            ZOT_NS,
            lib.base_url,
            map,
            lib.knowledge_base_graph
        )
        apply_additional_properties(
            store,
            safeNamedNode(a_library_href),
            sample_entry["library"],
            map.get("additional", []),
            lib.base_url,
            ZOT_NS
        )

    if collections:
        for col in collections:
            col_data = col["data"]
            key = col_data.get("key", uuid4())
            node_uri = NamedNode(f"{lib.base_url}/collections/{key}")
            if lib.map.get("named_library"):
                property_str = lib.map.get("named_library", "inLibrary")
                store.add(Quad(node_uri, safeNamedNode(property_str) if property_str.startswith("http") else safeNamedNode(f"{ZOT_NS}{property_str}"), safeNamedNode(a_library_href), graph_name=GRAPH_URI))

            collection_type_fields = map.get("collection_type") or []
            apply_rdf_types(store, node_uri, col_data, collection_type_fields, "collection", lib.base_url, ZOT_NS)

            collection_additional = map.get("additional") or []
            apply_additional_properties(store, node_uri, col_data, collection_additional, lib.base_url, ZOT_NS)

            add_rdf_from_dict(store, node_uri, col_data, ZOT_NS, lib.base_url, map, lib.knowledge_base_graph)
            add_timestamp(store=store, node=node_uri, graph=GRAPH_URI)
        logger.info(f"--> Loaded {len(collections)} collections for {lib.name} to store")
    else:
        logger.warning("No collections!") if not json_path_items else None

    if items:
        item_type_fields = lib.map.get("item_type") or []
        for item in items:
            try:
                item_data = item.get("data", {})
                creators = item_data.get("creators") or []
                first_creator = creators[0].get("lastName") if creators and "lastName" in creators[0] else "NO CREATOR"
                title = item_data.get("title") or "NO TITLE"
                date = item_data.get("date") or "NO DATE"
                label = f"{first_creator}: {title} ({date})"
                language = item_data.get("language")
                key = item_data.get("key",uuid4())            
                node_uri = NamedNode(f"{lib.base_url}/items/{key}")
                if lib.map.get("named_library"):
                    property_str = lib.map.get("named_library", "inLibrary")
                    store.add(Quad(node_uri, safeNamedNode(property_str) if property_str.startswith("http") else safeNamedNode(f"{ZOT_NS}{property_str}"), safeNamedNode(a_library_href), graph_name=GRAPH_URI))

                if label:
                    store.add(Quad(node_uri, NamedNode(RDFS_LABEL), Literal(label), graph_name=GRAPH_URI))

                apply_rdf_types(store, node_uri, item_data, item_type_fields, "item", lib.base_url, ZOT_NS)

                item_additional = map.get("additional") or []
                apply_additional_properties(store, node_uri, item_data, item_additional, lib.base_url, ZOT_NS)

                add_rdf_from_dict(store, node_uri, item_data, ZOT_NS, lib.base_url, map, lib.knowledge_base_graph,language)
                add_timestamp(store=store, node=node_uri, graph=GRAPH_URI)
    
            except Exception as e:
                logger.error(f"Invalid data at {node_uri}. See next errors for details!")
                continue
        logger.info(f"--> Loaded {len(items)} items for {lib.name} to store")
    else:
        logger.warning("No items!") if not json_path_collections else None

def parse_all_notes(lib: ZoteroLibrary, store: Store, note_predicate : NamedNode = NamedNode(f"{ZOT_NS}note"), query_str: str = None, replace:bool = False, push:bool=True):
    from zotero_rdf_server.plugins.parse_note import ParseNotePlugin
    from rdflib import Graph
    GRAPH_URI = NamedNode(lib.base_url)

    # Mapping
    raw_mapping = lib.parser.get("mapping")
    mapping = {}

    try:
        if isinstance(raw_mapping, dict):
            mapping = raw_mapping

        elif isinstance(raw_mapping, str):
            if os.path.exists(raw_mapping):
                with open(raw_mapping) as f:
                    mapping = json.load(f)
                logger.info(f"Parser mapping loaded from file: {raw_mapping}")
            else:
                mapping = json.loads(raw_mapping)
                logger.info("Parser mapping loaded from JSON string")
        else:
            raise ValueError("Invalid mapping input")

    except Exception as e:
        logger.warning(f"No mapping found, using fallback: {e}")
        mapping = {
            '@context': {
                '@base': lib.base_url,
                '@vocab': ZOT_NS
            }
        }

    raw_metadata = lib.parser.get("metadata")
    metadata = {}

    try:
        if isinstance(raw_metadata, dict):
            metadata = raw_metadata

        elif isinstance(raw_metadata, str):
            if os.path.exists(raw_metadata):
                with open(raw_metadata) as f:
                    metadata = json.load(f)
                logger.info(f"Parser metadata loaded from file: {raw_metadata}")
            else:
                metadata = json.loads(raw_metadata)
                logger.info("Parser metadata loaded from JSON string")
        else:
            raise ValueError("Invalid metadata input")

    except Exception as e:
        logger.warning(f"No metadata found, using fallback: {e}")
        metadata = {
            "wasGeneratedBy": os.path.basename(__file__)
        }
    map_KB = lib.parser.get("knowledge_base_mapping", False)
    if map_KB:        
        fuzzy_threshold = lib.parser.get("fuzzy", 90)
        knowledge_base = mapping.pop("KnowledgeBase") or []
        entity_graph_uri = safeNamedNode(lib.knowledge_base_graph)
        logger.debug(f"Map smenatic entites to KB following: {knowledge_base}")
    
    def map_semantic_entities(
        mem_store,
        knowledge_base: list = knowledge_base
    ):
        
        for rule in knowledge_base:            
            try:
                domain_type     = rule["domainTypes"]
                range_type      = rule["rangeType"]
                domain_prop     = rule["domainProperty"]
                target_prop     = rule["targetProperty"]
                map_prop        = rule["mapProperty"]
            except:
                logger.error("Missing key in KB Mapping dict")
                break

            for quad in mem_store.quads_for_pattern(
                None,
                NamedNode(RDF_TYPE),
                safeNamedNode(domain_type)
            ):
                domain_node = quad.subject
                logger.debug(f"Testing {quad.subject}")
                for dp in mem_store.quads_for_pattern(
                    domain_node,
                    safeNamedNode(domain_prop),
                    None
                ):
                    lit_value = str(dp.object.value)                    
                    logger.debug(f"Comparing semantic note label {lit_value} to KB labels with threshold {fuzzy_threshold}%")
                    try:
                        matched_node, score, label = fuzzy_match_label(
                            store,
                            lit_value,
                            type_node=safeNamedNode(f"{range_type}"),
                            threshold=fuzzy_threshold,
                            graph_name=entity_graph_uri,
                            predicates=[target_prop]
                        )

                        if matched_node:
                            logger.debug(f"Matched semantic note label {lit_value} to KB label {label} with {score}%: {domain_node} to {matched_node}")
                            mem_store.add(Quad(
                                domain_node,
                                safeNamedNode(map_prop),
                                matched_node
                            ))
                    except Exception as e:
                        logger.error(f"Error matching KB: {e}")
                    
 

        return mem_store


    plugin = ParseNotePlugin(mapping=mapping, metadata=metadata)
    logger.debug("Plugin initialized")
    count = 0
    if query_str and "SELECT" in query_str:
        logger.debug(f"using query pattern: {query_str}")
        note_quads = store.query(query_str,default_graph=GRAPH_URI)
    else:
        logger.debug(f"using predicate pattern: {note_predicate}")
        note_quads = store.quads_for_pattern(None, note_predicate, None, GRAPH_URI)

    # if replace: #TODO delete only quads for pares notes
    #     for quad in note_quads:
    #         store.remove(quad)


    for quad in note_quads:
        subject = quad.subject
        obj = quad.object

        if isinstance(obj, Literal):
            count += 1
            html = obj.value
            note_uri = subject.value if hasattr(subject, "value") else str(subject)
            result = plugin.run(html_str=html, note_uri=note_uri)
            logger.debug(json.dumps(result, indent=2))
            g = Graph()
            g.parse(data=json.dumps(result), format="json-ld")
            logger.debug("JSON-LD parsed")
            
            if push:
                try:
                    mem_store = Store()
                    mem_store.load(g.serialize(format="turtle"), format=RdfFormat.TURTLE, to_graph=GRAPH_URI)                
                    store.extend(map_semantic_entities(mem_store)) if map_KB else store.extend(mem_store)
                    logger.info(f"Extended store: {len(mem_store)} triples")
                except Exception as e:
                    logger.error(f"Error when extending store: {e}")
            else:
                logger.info("Serialized only")
                g.serialize(format="turtle")


        # Map Semantic-HTML entities to domain knowledge base



    return count

def zotero_schema(schema, vocab_iri="http://www.zotero.org/namespaces/export#"):
    GRAPH_URI = safeNamedNode(vocab_iri.strip("#/"))

    def uri(term): # TODO create from context dict
        if term.startswith("owl:"):
            return safeNamedNode("http://www.w3.org/2002/07/owl#" + term[4:])
        if term.startswith("rdfs:"):
            return safeNamedNode("http://www.w3.org/2000/01/rdf-schema#" + term[5:])
        if term.startswith("rdf:"):
            return safeNamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#" + term[4:])
        return safeNamedNode(vocab_iri + term)
    
    def make_rdf_list(elements):
        if not elements:
            return NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")
        head = BlankNode()
        current = head
        for i, elem in enumerate(elements):
            store.add(Quad(current, uri("rdf:first"), uri(elem), graph_name=GRAPH_URI))
            next_node = BlankNode() if i < len(elements) - 1 else NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")
            store.add(Quad(current, uri("rdf:rest"), next_node, graph_name=GRAPH_URI))
            current = next_node
        return head

    def add_union_triple(subject, predicate, types):
        if len(types) == 1:
            store.add(Quad(subject, uri(predicate), uri(types[0]), graph_name=GRAPH_URI))
        else:
            union_node = BlankNode()
            store.add(Quad(subject, uri(predicate), union_node, graph_name=GRAPH_URI))
            store.add(Quad(union_node, uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
            rdf_list = make_rdf_list(types)
            store.add(Quad(union_node, uri("owl:unionOf"), rdf_list, graph_name=GRAPH_URI))

    # Labels
    locales = schema.get("locales", {})
    class_labels = defaultdict(list)
    property_labels = defaultdict(list)

    for lang, content in locales.items():
        for t, label in content.get("itemTypes", {}).items():
            class_labels[t].append(Literal(label, language=lang))
        for t, label in content.get("creatorTypes", {}).items():
            class_labels[t].append(Literal(label, language=lang))
        for f, label in content.get("fields", {}).items():
            property_labels[f].append(Literal(label, language=lang))

    item_types = schema.get("itemTypes", [])
    # Create Main Classes not set in Schema
    for main_class in ["item", "library", "collection", "tag", "creatorRole"]: # TODO make dynamic
        store.add(Quad(uri(main_class), uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
        store.add(Quad(uri(main_class), uri("rdfs:label"), Literal(main_class), graph_name=GRAPH_URI))

    for item_type in item_types:
        class_name = item_type["itemType"]
        class_node = uri(class_name)
        store.add(Quad(class_node, uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
        store.add(Quad(class_node, uri("rdfs:subClassOf"), uri("item"), graph_name=GRAPH_URI)) # subclass of item
        for label in class_labels.get(class_name, []):
            store.add(Quad(class_node, uri("rdfs:label"), label, graph_name=GRAPH_URI))

    field_domains = defaultdict(set)
    base_fields = {}
    for item_type in item_types:
        class_name = item_type["itemType"]
        for field in item_type.get("fields", []):
            field_name = field["field"]
            field_domains[field_name].add(class_name)
            if "baseField" in field:
                base_fields[field_name] = field["baseField"]

    for field, domains in field_domains.items():
        prop_node = uri(field)
        store.add(Quad(prop_node, uri("rdf:type"), uri("owl:DatatypeProperty"), graph_name=GRAPH_URI))
        add_union_triple(prop_node, "rdfs:domain", list(domains))
        store.add(Quad(prop_node, uri("rdfs:range"), uri("rdfs:Literal"), graph_name=GRAPH_URI))
        for label in property_labels.get(field, []):
            store.add(Quad(prop_node, uri("rdfs:label"), label, graph_name=GRAPH_URI))
        if field in base_fields:
            store.add(Quad(prop_node, uri("owl:equivalentProperty"), uri(base_fields[field]), graph_name=GRAPH_URI))

    for item_type in item_types:
        class_name = item_type["itemType"]
        creators = item_type.get("creatorTypes", [])
        if creators:
            creator_types = [c["creatorType"] for c in creators]
            for ct in creator_types:
                ct_node = uri(ct)
                store.add(Quad(ct_node, uri("rdf:type"), uri("owl:Class"), graph_name=GRAPH_URI))
                store.add(Quad(ct_node, uri("rdfs:subClassOf"), uri("creatorRole"), graph_name=GRAPH_URI)) # subclass of item
                for label in class_labels.get(ct, []):
                    store.add(Quad(ct_node, uri("rdfs:label"), label, graph_name=GRAPH_URI))
            prop_node = uri("creators")
            store.add(Quad(prop_node, uri("rdf:type"), uri("owl:ObjectProperty"), graph_name=GRAPH_URI))
            store.add(Quad(prop_node, uri("rdfs:label"), Literal("Creators"), graph_name=GRAPH_URI))
            add_union_triple(prop_node, "rdfs:range", creator_types)
            add_union_triple(prop_node, "rdfs:domain", [class_name])

def initialize_store():
    global store
    if STORE_MODE == "memory":
        store = Store()
    elif STORE_MODE == "directory":
        os.makedirs(STORE_DIRECTORY, exist_ok=True)
        store = Store(path=STORE_DIRECTORY)
    else:
        raise ValueError(f"Invalid store_mode: {STORE_MODE}")
    global GRAPHS 
    GRAPHS = [str(g) for g in store.named_graphs()]

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

                if ZOT_SCHEMA:
                    try:
                        schema = requests.get(ZOT_SCHEMA).json()
                        zotero_schema(schema,ZOT_NS)
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

# --- API Endpoints ---

@router.get("/export", summary="Create export", description=f"Exports the store or a named graph to {EXPORT_DIRECTORY}", tags=["data"])
async def export_graph(
    format: str = Query("trig"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional)")
):
    graph = f"<{graph.strip().strip('<>').strip()}>"
    global store
    graphs = [str(g) for g in store.named_graphs()]
    if graph and graph not in graphs:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {graphs}")

    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)

    format_map = {
        "trig": (RdfFormat.TRIG, "trig"),
        "nquads": (RdfFormat.N_QUADS, "nq"),
        "ttl": (RdfFormat.TURTLE, "ttl"),
        "nt": (RdfFormat.N_TRIPLES, "nt"),
        "n3": (RdfFormat.N3, "n3"),
        "xml": (RdfFormat.RDF_XML, "rdf")
    }
    # prefixes = dict(PREFIXES)

    # for i, graph_uri in enumerate(store.named_graphs(), start=1):
    #     prefix = f"z{i}"
    #     prefixes[prefix] = str(graph_uri).strip("<>")

    if format not in format_map:
        raise HTTPException(status_code=400, detail="Unsupported export format")

    rdf_format, extension = format_map[format]
    filename_base = iri_to_filename(graph) if graph else "zotero_store"
    path = os.path.join(EXPORT_DIRECTORY, f"{filename_base}.{extension}")

    no_named_graph_support = rdf_format in {
        RdfFormat.TURTLE, RdfFormat.N_TRIPLES, RdfFormat.N3, RdfFormat.RDF_XML
    }

    kwargs = {}
    if graph:
        clean_graph = graph.strip("<>")
        kwargs["from_graph"] = safeNamedNode(clean_graph)
        logger.info(f"Export from graph: {clean_graph}")
    elif no_named_graph_support:        
        kwargs["from_graph"] = DefaultGraph()
    else:
        logger.info(f"Export from graphs: {list(store.named_graphs())}")

    store.dump(output=path, format=rdf_format, prefixes=PREFIXES, **kwargs)
    return {"success":f"Export to: {path}"}
    # return FileResponse(path, filename=os.path.basename(path))

@router.get("/backup", summary="Create backup", description=f"Creates a complete backup of the store to {BACKUP_DIRECTORY}", tags=["data"])
async def backup_store():
    global store
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
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}

@router.get("/optimize", summary="Optimize Store", description="Will optimize the oxigraph store", tags=["data"])
async def optimize_store():
    global store
    store.optimize()
    return {"success":"Store optimized"}


@router.get("/libs", summary="List of all libraries", description="Returns all available libraries with configuration.", tags=["config"])
async def get_libs():
    result = [ZoteroLibrary(cfg) for cfg in ZOTERO_LIBRARIES_CONFIGS]
    return {"success": result}

@router.get("/graphs", summary="List of all named graphs", description="Returns all available named graphs.", tags=["RDF"])
async def get_graphs():
    global store
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}

@router.get("/parse_notes", summary="Parse notes", description="Triggers the parsing of all Zotero notes with semantic-html plugin", tags=["RDF"])
async def parse_notes(
    replace: bool = Query(default=False, description="Replaces current triples for notes"),
    graph: str | None = Query(default=None, description="Named graph IRI (optional)"),
    note_predicate: str | None  = Query(default=f"{ZOT_NS}note", description="predicate for note HTML"),
    query: str | None = Query(default=None, description="Query to retrieve notes (optional)"),
    push: bool | None = Query(default=True, description="Push triples to store (optional)")
    ):

    global store
    graphs = [str(g) for g in store.named_graphs()]
    graph = f"<{graph.strip().strip('<>').strip()}>"
    if graph and graph not in graphs:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {graphs}")
    if not note_predicate:
        predicate = safeNamedNode(f"{ZOT_NS}note")
    else:
        predicate = safeNamedNode(f"{note_predicate}")


    for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
        lib = ZoteroLibrary(lib_cfg)
        if not graph or graph == lib.base_url:
            result=parse_all_notes(lib, store, note_predicate=predicate, query_str=query, replace=replace,push=push)
    return {"success":f"{result} notes parsed"}

@router.get("/csv", summary="Export CSV", description="Exports a named graph or the entire store as CSV or loads a CSV as RDF into the store", tags=["RDF"])
async def get_csv(
    graph: str | None = Query(default=None, description="Named graph IRI (optional)"),
    load_csv: str | None = Query(default=None, description="Load a CSV file into the store"),
    delete: bool | None = Query(default=False, description="Removes triples from graph if true, done before loading triples (you may only use subject IRIs to just delete)")
    ):
    from collections import defaultdict
    import csv

    graph_uri = safeNamedNode(graph) if graph else None
    os.makedirs(EXPORT_DIRECTORY, exist_ok=True)
    output_file = os.path.join(EXPORT_DIRECTORY, f"export.csv")
    delimiter = " | "
    global store
    graphs = [str(g) for g in store.named_graphs()]
    graph = f"<{graph.strip().strip('<>').strip()}>"
    if graph and graph not in graphs:
        raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {graphs}")
    # subject → { predicate → [objects...] }
    # NamedNodes as objects are wrapped in <> for both export and import
    records = defaultdict(lambda: defaultdict(list))
    all_predicates = set()
    for quad in store.quads_for_pattern(None, None, None, graph_uri):
        subj = (quad.subject.value)
        pred = (quad.predicate.value)
        obj = obj.value if isinstance(obj,Literal) else str(obj)
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

    if load_csv and os.path.exists(load_csv) and load_csv is not output_file:
        if delete:
            subjects = set()
            with open(load_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    subj_iri = row["IRI"].strip()
                    if subj_iri:
                        subjects.add(safeNamedNode(subj_iri))
            for subj in subjects:
                for quad in store.quads_for_pattern(subj, None, None, graph_uri):
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
                            quad = Quad(subj, predicate, obj, graph_uri)
                            store.add(quad)
    graphs = [str(g) for g in store.named_graphs()]
    return {"status": "success", "store":{"named_graphs":graphs, "len":len(store)}}
# --- Start server ---

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    initialize_store()
    if log_level != "DEBUG": # delay start to have oxigraph initialize first
        logger.info(f"Delay loading for {DELAY} seconds")
        time.sleep(DELAY)
    threading.Thread(target=refresh_store, daemon=True).start()
    yield

app = FastAPI(lifespan=app_lifespan, docs_url="/")
app.include_router(router)