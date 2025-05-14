import os
from uuid import uuid5, NAMESPACE_URL, uuid4
import json, re
from datetime import datetime
from dateutil import parser

from .store import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode
from .logging_config import logger
from .config import *
from .models import ZoteroLibrary
from .utils import *



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
                    iri_suffix = uuid5(ENTITY_UUID, item) if fuzzy_threshold <= 100 else uuid4()
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
                        creator_uuid = uuid5(ENTITY_UUID, label) if fuzzy_threshold <= 100 else uuid4()
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
                        store.add(Quad(subject, predicate_node, safeNamedNode(val, enforce=True), graph_name=GRAPH_URI))

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
            prefix = spec.get("prefix","")
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
                else:
                    raw_value = prefix + raw_value

            if named_node:                
                obj = safeNamedNode(raw_value,enforce=True)
                store.add(Quad(node, predicate, obj, graph_name=GRAPH_URI))
                logger.debug(f"Added named node {obj.value}")
                continue
    
            obj = Literal(str(raw_value))

            store.add(Quad(node, predicate, obj, graph_name=GRAPH_URI))
        except Exception as e:
            logger.error(f"Invalid data at {node} for {raw_value}")
            continue

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
        # entity_graph_uri = safeNamedNode(lib.knowledge_base_graph)
        logger.debug(f"Map semantic entites to KB following: {knowledge_base}")
    
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
                KB_graph        = rule.get("knowledgeBaseGraph", None)
            except:
                logger.error("Missing key in KB Mapping dict")
                break

            entity_graph_uri = NamedNode(KB_graph) or safeNamedNode(lib.knowledge_base_graph)

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
                            type_node=safeNamedNode(range_type),
                            threshold=fuzzy_threshold,
                            graph_name=entity_graph_uri,
                            predicates=[target_prop]
                        )

                        if matched_node:
                            logger.debug(f"Matched semantic note label {lit_value} to KB label {label} with {score}%: {domain_node} to {matched_node}")
                            mem_store.add(Quad(
                                domain_node,
                                safeNamedNode(map_prop),
                                matched_node,
                                GRAPH_URI
                            ))
                        elif not matched_node and KB_graph and isinstance(KB_graph, str):   # maybe by trigger or argument in mapping?            
                            ENTITY_UUID = uuid5(NAMESPACE_URL, str(KB_graph))
                            iri_suffix = uuid5(ENTITY_UUID, lit_value)
                            domain_node = safeNamedNode(f"{KB_graph}/semantic_html/{iri_suffix}")
                            mem_store.add(Quad( # not sure this works as expected, maybe load to local store insted?
                                domain_node,
                                NamedNode(RDF_TYPE),
                                safeNamedNode(range_type),
                                safeNamedNode(KB_graph)                           
                            ))
                            mem_store.add(Quad(domain_node, NamedNode(RDFS_LABEL), Literal(lit_value), graph_name=safeNamedNode(KB_graph)))
                            mem_store.add(Quad(
                                domain_node,
                                safeNamedNode(map_prop),
                                domain_node,
                                safeNamedNode(KB_graph)                           
                            ))
                            logger.debug(f"Added label {lit_value} to KB as {domain_node}")

                        alts = {(q.object.value).lower() for q in store.quads_for_pattern(domain_node, NamedNode(SKOS_ALT), None, graph_name=safeNamedNode(KB_graph))}
                        if lit_value.lower() not in alts:
                            mem_store.add(Quad(domain_node, NamedNode(SKOS_ALT), Literal(lit_value), graph_name=safeNamedNode(KB_graph)))                     
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

    # if replace: #TODO delete only quads for parent notes
    #     for quad in note_quads:
    #         store.remove(quad)


    for quad in note_quads: # TODO first serialize all parsed notes and then extend in bulk
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
                    logger.debug(f"Extended store: {len(mem_store)} triples")
                except Exception as e:
                    logger.error(f"Error when extending store: {e}")
            else:
                logger.info("Serialized only")
                g.serialize(format="turtle")


        # Map Semantic-HTML entities to domain knowledge base

    logger.info(f"Semantic-HTML parsing completed, {count} notes parsed")

    return count



