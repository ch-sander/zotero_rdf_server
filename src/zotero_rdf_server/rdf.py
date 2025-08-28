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
        ext = os.path.splitext(filename)[1].lstrip('.').lower()
        if ext == "json":  # call for JSON
            logger.warning(f"A {filename} will be parsed as Zotero Export JSON file. For JSON-LD, use .jsonld extension instead!")
            json_path = os.path.join(subdir, filename)
            build_graph_for_library(lib, store, json_path=json_path)
            continue

        fmt = RdfFormat.from_extension(ext)
        if fmt is None:
            logger.info(f"Skipping unsupported file: {filename}")
            continue

        before = len(store)
        store.bulk_load(
            path=filepath,
            format=fmt,
            base_iri=f"{lib.base_url}/items/",
            to_graph=NamedNode(lib.base_url)
        )
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
    creator_types = map.get("creator_types") or []
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
            
        def make_entity(object_value,my_type):
            # Normalize and split values
            value = object_value.strip()
            items = [p.strip() for p in re.split(r"[;]", value) if p.strip()] # Do not split on comma!

            for item in items:
                pool_store = Store()
                pool_store.bulk_extend(store.quads_for_pattern(None,NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}{my_type}"),ENTITY_GRAPH_URI))
                node, score, matched_label = fuzzy_match_label(
                    pool_store,
                    item,
                    threshold=fuzzy_threshold
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
                ### TODO: "person" not hard-coded

                if predicate_str == "creators":
                    if "name" in object:
                        label = object["name"]
                    else:
                        label = f"{object.get('lastName', '')}, {object.get('firstName', '')}"

                    bnode = BlankNode()
                    store.add(Quad(subject, predicate_node, bnode, graph_name=GRAPH_URI))                    
                    store.add(Quad(bnode, NamedNode(RDF_TYPE), NamedNode(f"{ns_prefix}creatorRole"), graph_name=GRAPH_URI))
                    pool_store = Store()
                    pool_store.bulk_extend(store.quads_for_pattern(None,NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}person"),ENTITY_GRAPH_URI))

                    creator_node, score, matched_label = fuzzy_match_label(
                        pool_store,
                        label,
                        threshold=fuzzy_threshold
                    )

                    if not creator_node:
                        creator_uuid = uuid5(ENTITY_UUID, label) if fuzzy_threshold <= 100 else uuid4()
                        creator_node = safeNamedNode(f"{knowledge_base_graph}/person/{creator_uuid}")
                        
                        # TODO: Check if working
                        # store.add(Quad(creator_node, NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}person"), graph_name=ENTITY_GRAPH_URI))
                        
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

                    apply_rdf_types(store, creator_node, object, creator_types, "person", knowledge_base_graph, ns_prefix)

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
                    vals = [object.strip()] #for v in object.split(",")] # no splitting of URLs!
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
        # ignore_tags = lib.map.get("ignore_tags") or []
        for item in items:
            node_uri = None
            try:
                item_data = item.get("data", {})

                # item_tags = [t.get("tag") for t in item_data.get("tags", []) if isinstance(t, dict)]
                # if any(ig in item_tags for ig in ignore_tags):
                #     continue
                
                creators = item_data.get("creators") or []
                if creators and "lastName" in creators[0]:
                    first_creator = creators[0].get("lastName") 
                elif creators and "name" in creators[0]:
                    first_creator = creators[0].get("name") 
                else:
                    first_creator = "NO CREATOR"

                title = item_data.get("title") or "NO TITLE"
                date = item_data.get("date") or "NO DATE"
                label = f"{first_creator}: {title} ({date})"
                language = item_data.get("language")
                key = item_data.get("key",uuid4())            
                node_uri = NamedNode(f"{lib.base_url}/items/{key}")
            except Exception as e:
                logger.error(f"Invalid data preparation for items!")
                continue    

            try:
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

def parse_all_notes(lib: ZoteroLibrary, store: Store, note_predicate : NamedNode = NamedNode(f"{ZOT_NS}note"), query_str: str = None, delete:bool = False, push:bool=True):
    from zotero_rdf_server.plugins.parse_note import ParseNotePlugin

    GRAPH_URI = safeNamedNode(lib.base_url) # Source graph of notes
    SEMANTIC_HTML_GRAPH = safeNamedNode(lib.parser.get("base_uri", {lib.base_url}))
    KB_GRAPH = safeNamedNode(lib.knowledge_base_graph)
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
        logger.warning(f"No metadata found: {e}")
        metadata = {
            "wasGeneratedBy": os.path.basename(__file__)
        }
        logger.warning(f"Using fallback: {metadata}")

    map_KB = lib.parser.get("knowledge_base_mapping", False)
    tag_filter = lib.parser.get("tag_filter")
    predicate = lib.parser.get("predicate")
    query = lib.parser.get("query")
    if predicate and isinstance(predicate, str): note_predicate = safeNamedNode(predicate)
    if query and isinstance(query, str) and "SELECT" in str(query).upper(): query_str = query


    if map_KB:        
        fuzzy_threshold = lib.parser.get("fuzzy", 90)
        knowledge_base = mapping.pop("KnowledgeBase") or []
        # entity_graph_uri = safeNamedNode(lib.knowledge_base_graph)
        logger.debug(f"Map semantic entites to KB following: {knowledge_base}")

    def map_semantic_entities(
        source_store,
        target_store,
        knowledge_base: list = None,
        fuzzy_threshold: int = 85
    ):
        result_store = Store()

        def _subjects(store, graph=None):
            return {q.subject for q in store.quads_for_pattern(None, None, None, graph)}

        if knowledge_base is None:
            return result_store
        for rule in knowledge_base:
            fuzzy_rules = rule.get("FUZZY", [])
            pool_rules = rule.get("POOL", [])
            same_rules = rule.get("SAME", [])
            map_prop = safeNamedNode(rule.get("mapProperty", OWL_SAME_AS))
            KB_graph = rule.get("knowledgeBaseGraph", None)
            entity_graph_uri = safeNamedNode(KB_graph) if KB_graph else KB_GRAPH
            alt_label_prop = safeNamedNode(rule.get("altLabel", SKOS_ALT))
            add_jsonld = rule.get("ADD")
            allow_create = rule.get("allowCreate", False)
            # AND
            filter_source_subjects = set()
            filter_target_subjects = set()
            filter_source_store = Store()
            filter_target_store = Store()
            logger.debug("KB definition found!")
            if pool_rules:
                logger.debug("POOL Rule definition found!")
                for p in pool_rules:

                    operator = p.get('operator', "OR")
                    comment = p.get('comment')
                    if comment and isinstance(comment,str):
                        logger.debug(comment)
                    domainProperty = p.get('domainProperty', None)
                    domainObject = p.get('domainObject', None)                    
                    targetProperty = p.get('targetProperty', None)
                    targetObject = p.get('targetObject', None)

                    if domainProperty or domainObject:
                        if operator == "AND":
                            pool_source = filter_source_store
                        else:
                            pool_source = source_store

                        src_qs = list(pool_source.quads_for_pattern(
                            None,
                            safeNamedNode(domainProperty, allow_None = True),
                            safeNamedNode(domainObject, allow_None = True),
                            None
                        ))

                        filter_source_subjects.update(q.subject for q in src_qs)
                        logger.info(f"POOL rule found (domain). Added {len(filter_source_subjects)}")
                        for q in src_qs:
                            filter_source_store.bulk_extend(pool_source.quads_for_pattern(q.subject, None, None, None))                        
                    else:
                        logger.warning(f"No domain POOL rule applied, couldn't find {domainProperty} or {domainObject}")

                    if targetProperty or targetObject:
                        if operator == "AND":
                            pool_target = filter_target_store
                        else:
                            pool_target = target_store
                        
                        tgt_qs = list(pool_target.quads_for_pattern(
                            None,
                            safeNamedNode(targetProperty, allow_None = True),
                            safeNamedNode(targetObject, allow_None = True),
                            entity_graph_uri
                        ))

                        filter_target_subjects.update(q.subject for q in tgt_qs)
                        logger.info(f"POOL rule found (target). Added {len(filter_target_subjects)}")
                        for q in tgt_qs:
                            filter_target_store.bulk_extend(pool_target.quads_for_pattern(q.subject, None, None, entity_graph_uri))
                    else:
                        logger.warning(f"No target POOL rule applied, couldn't find {targetProperty} or {targetObject}")

            else: # fallback if no POOL rules
                logger.debug("No POOL Rule definition found!")
                filter_source_subjects = _subjects(source_store, None)
                filter_target_subjects = _subjects(target_store, entity_graph_uri)

            # for s in filter_source_subjects:
            #     filter_source_store.bulk_extend(source_store.quads_for_pattern(s, None, None, None))
            # for s in filter_target_subjects:
            #     filter_target_store.bulk_extend(target_store.quads_for_pattern(s, None, None, entity_graph_uri))
                
            logger.info(f"LEN filtered source store: {len(filter_source_store)}. Found graphs in filtered source store: {list(filter_source_store.named_graphs())}")

            logger.info(f"LEN filtered target store: {len(filter_target_store)}. Found graphs in filtered target store: {list(filter_target_store.named_graphs())}")

            if same_rules:
                logger.debug(f"{len(same_rules)} SAME Rules found!")
            if fuzzy_rules:
                logger.debug(f"{len(fuzzy_rules)} FUZZY Rules found!")

            for domain_node in filter_source_subjects:
                value_matched = False

                # SAME
                for same_rule in same_rules:                    
                    try:
                        dom_prop = safeNamedNode(same_rule["domainProperty"])
                        tgt_prop = safeNamedNode(same_rule["targetProperty"])
                    except KeyError:
                        continue

                    for dp in filter_source_store.quads_for_pattern(domain_node, dom_prop, None):
                        lit_value = str(dp.object.value)

                        for tq in filter_target_store.quads_for_pattern(None, tgt_prop, Literal(lit_value), entity_graph_uri):
                            result_store.add(Quad(
                                domain_node,
                                map_prop,
                                tq.subject,
                                dp.graph_name
                            ))
                            logger.debug(f"[SAME] Matched {lit_value} by identity: {domain_node} → {tq.subject}")
                            value_matched = True
                            ensure_alt_label(result_store, tq.subject, lit_value, alt_label_prop, entity_graph_uri)

                            break
                        if value_matched:
                            break
                    if value_matched:
                        break

                if value_matched:
                    continue  # no FUZZY needed

                # FUZZY
                for fuzzy in fuzzy_rules:
                    try:
                        domain_prop = safeNamedNode(fuzzy["domainProperty"])
                        target_prop = safeNamedNode(fuzzy["targetProperty"])
                        fuzzy_threshold = fuzzy.get('threshold', fuzzy_threshold)
                        regex = fuzzy.get('regex', False)
                    except KeyError:
                        continue


                    # logger.info("############################################")
                    # logger.info(len(filter_source_store))

                    for dp in filter_source_store.quads_for_pattern(domain_node, domain_prop, None):
                        lit_value = str(dp.object.value)

                        try:
                            
                            matched_node, score, label = fuzzy_match_label(
                                filter_target_store,
                                lit_value,
                                threshold=fuzzy_threshold,
                                predicates=[target_prop],
                                regex=regex
                            )

                            if matched_node:
                                result_store.add(Quad(
                                    domain_node,
                                    map_prop,
                                    matched_node,
                                    dp.graph_name
                                ))
                                logger.debug(f"[FUZZY] Matched {lit_value} to {label} ({score}%)")
                                ensure_alt_label(result_store, matched_node, lit_value, alt_label_prop, entity_graph_uri)

                            elif allow_create:
                                ENTITY_UUID = uuid5(NAMESPACE_URL, str(entity_graph_uri.value))
                                iri_suffix = uuid5(ENTITY_UUID, lit_value)
                                base_uri = lib.parser.get('base_uri', f"{str(entity_graph_uri.value).rstrip('/')}") 
                                new_node = safeNamedNode(f"{base_uri}/{iri_suffix}") # {KB_graph}/semantic_html/{iri_suffix}

                                for p in pool_rules:
                                    try:
                                        result_store.add(Quad(
                                            new_node,
                                            safeNamedNode(p["targetProperty"]),
                                            safeNamedNode(p["targetObject"]),
                                            entity_graph_uri
                                        ))
                                    except KeyError:
                                        continue

                                result_store.add(Quad(new_node, target_prop, Literal(lit_value), entity_graph_uri))
                                if target_prop != NamedNode(RDFS_LABEL):
                                    result_store.add(Quad(new_node, NamedNode(RDFS_LABEL), Literal(lit_value), entity_graph_uri))
                                
                                result_store.add(Quad(domain_node, map_prop, new_node, dp.graph_name))
                                result_store.add(Quad(new_node, alt_label_prop, Literal(lit_value), entity_graph_uri))
                                
                                if add_jsonld:
                                    try:
                                        jsonld_copy = add_jsonld
                                        if "@graph" in jsonld_copy:
                                            logger.warning(f"[ADD] '@graph' found in ADD block and is ignored. Only single object additions are supported.")
                                        else:                                            
                                            jsonld_copy["@id"] = str(new_node)
                                            result_store.load(json.dumps(jsonld_copy), to_graph=entity_graph_uri, format=RdfFormat.JSON_LD)
                                            logger.debug(f"[ADD] Added JSON-LD supplement for {new_node}")
                                    except Exception as e:
                                        logger.warning(f"[ADD] Failed to add JSON-LD for {new_node}: {e}")

                                logger.debug(f"[CREATE] New KB node for {lit_value} → {new_node}")

                        except Exception as e:
                            logger.error(f"[ERROR] Fuzzy match failed for '{lit_value}' with prop {domain_prop} → {target_prop}: {e}")

        logger.info(f"Returning parser mapping result store with {len(result_store)} quads")
        return result_store


    plugin = ParseNotePlugin(mapping=mapping, metadata=metadata)
    logger.debug("Plugin initialized")
    count = 0


    # Search notes in library graph
    # TODO: predicate not sufficient?
    if tag_filter and not query_str:
        # PREFIX zot: <{ZOT_NS}>
        # SELECT DISTINCT ?s ?p ?o WHERE {{
        # ?s zot:tags/zot:tag "{tag_filter}".
        # BIND({str(predicate) if predicate else "zot:note"} as ?p)
        # ?s ?p ?o.
        # }}
        query_str = f"""
        PREFIX zot: <{ZOT_NS}>
        SELECT DISTINCT ?s ?p ?o
        WHERE {{
        GRAPH <{GRAPH_URI.value}> {{ ?s zot:note ?o . BIND({str(predicate) if predicate else "zot:note"} AS ?p) ?s zot:tags ?t . }}
        GRAPH <{KB_GRAPH.value}>  {{ ?t zot:tag ?val . FILTER(STR(?val) = "{tag_filter}") }}
        }}
        """


    if query_str and "SELECT" in query_str.upper(): 
        logger.debug(f"using query pattern: {query_str}")
        bindings = store.query(query_str, use_default_graph_as_union=True)
        results = list(bindings)
        logger.info("Number of rows: %s", len(results))
        note_quads = []
        for row in results:  # QuerySolutions            
            tmp_predicate = row['p'] if 'p' in row else note_predicate
            
            quad = Quad(
                subject=row["s"], # the note IRI
                predicate=tmp_predicate,
                object=row["o"], # the HTML
                graph_name=GRAPH_URI
            )
            note_quads.append(quad)
    else:
        logger.debug(f"using predicate pattern: {note_predicate}")
        note_quads = list(store.quads_for_pattern(None, note_predicate, None, GRAPH_URI))

    
    if delete:
        logger.warning("Deleting quads!")

        # delete_query = f"""
        #     DELETE {{
        #     GRAPH <{GRAPH_URI.value}> {{
        #         ?s2 ?p ?o .
        #     }}
        #     }}
        #     WHERE {{
        #     GRAPH <{GRAPH_URI.value}> {{
        #         ?s1 <{note_predicate.value}> ?o1 .
        #         ?s2 ?p2 ?s1 .
        #         ?s2 ?p ?o .
        #     }}
        #     }}
        #     """ if note_predicate and not query_str else query_str
        # logger.info(f"Query: {delete_query}")
        try:
            # store.update(delete_query)
            if SEMANTIC_HTML_GRAPH and SEMANTIC_HTML_GRAPH != GRAPH_URI:
                store.remove_graph(SEMANTIC_HTML_GRAPH)
                logger.info(f"Removed named graph {SEMANTIC_HTML_GRAPH}!")
            else:
                logger.warning(f"Did not delete graph, as identical to library graph!")
        except Exception as e:
            logger.error(f"Error when deleting triples: {e}")
    logger.info("Number of note_quads: %s", len(note_quads))
    parser_store = Store()
    for quad in note_quads:
        subject = quad.subject
        obj = quad.object

        if isinstance(obj, Literal):
            count += 1
            html = obj.value
            note_uri = subject.value if hasattr(subject, "value") else str(subject)
            result = plugin.run(html_str=html, note_uri=note_uri)
            logger.debug(json.dumps(result, indent=2))    # TODO debug        
            
            try:
                tmp_store = Store()
                tmp_store.load(json.dumps(result["JSON-LD"]), format=RdfFormat.JSON_LD, to_graph=SEMANTIC_HTML_GRAPH)
                parser_store.extend(tmp_store)
                # logger.debug(tmp_store.dump(format=RdfFormat.TRIG).decode("utf-8"))
                logger.debug("JSON-LD parsed")                    
            except Exception as e:
                logger.error(f"Error when parsing note: {e}")
    logger.info(f"Parsed {count} notes!")

    try:
        if push and count > 0:            
            store.bulk_extend(parser_store)
            logger.info(f"Extended store from parser results: {len(parser_store)} quads")
            if map_KB and knowledge_base:
                target_store = Store()
                target_store.bulk_extend(store.quads_for_pattern(None,None,None,KB_GRAPH))
                logger.info(f"PUSH: Len source store ({SEMANTIC_HTML_GRAPH}): {len(parser_store)}, LEN target store ({KB_GRAPH}): {len(target_store)}")
                logger.info(f"Extending store from mapping with {len(knowledge_base)} rules")
                result_store = map_semantic_entities(parser_store, target_store, knowledge_base, fuzzy_threshold)
                store.bulk_extend(result_store)
                logger.info(f"Extended store from mapping: {len(result_store)} quads")
            else:                
                logger.info(f"No mapping for parser provided!")
        else:
            logger.info("Serialized only")                    
            
    except Exception as e:
        logger.error(f"Error when extending store: {e}")

    logger.info(f"Semantic-HTML parsing completed, {count} notes parsed")

    return count



