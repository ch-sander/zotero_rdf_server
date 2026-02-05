from uuid import uuid5, NAMESPACE_URL, uuid4
import json, re, requests, tempfile
from datetime import datetime
from dateutil import parser
from pathlib import Path
from requests.exceptions import RequestException
from typing import Iterable, Optional

from .store import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode
from .logging_config import logger
from .config import *
from .models import ZoteroLibrary
from .utils import *
from dataclasses import dataclass

DEFAULT_ENTITIES = ["place","publisher","series"]

@dataclass
class SourceFile:
    path: Path
    name: str
    cleanup: Optional[callable] = None

def _iter_sources(lib) -> Iterable[SourceFile]:

    if lib.load_from and is_url(str(lib.load_from)):
        url = str(lib.load_from)
        logger.info(f"Downloading RDF source from URL: {url}")

        tmpdir = tempfile.TemporaryDirectory(prefix="rdf_import_")
        tmpdir_path = Path(tmpdir.name)

        try:
            # stream
            resp = requests.get(url, stream=True, timeout=(5, 60), allow_redirects=True)
            resp.raise_for_status()
        except RequestException as e:
            logger.warning(f"Failed to download {url}: {e}")
            tmpdir.cleanup()
            return

        ext = guess_ext_from_headers(resp.headers, url)
        if not ext:
            ext = "ttl"

        filename = f"downloaded.{ext}"
        target = tmpdir_path / filename

        try:
            with open(target, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            logger.warning(f"Failed to write downloaded content to disk ({target}): {e}")
            tmpdir.cleanup()
            return

        yield SourceFile(path=target, name=target.name, cleanup=tmpdir.cleanup)
        return

    # Default: local
    subdir = Path(lib.load_from) if lib.load_from else Path(IMPORT_DIRECTORY) / lib.name
    subdir = subdir.resolve()
    if not subdir.is_dir():
        logger.warning(f"Directory not found for manual import: {subdir}")
        return

    for filepath in subdir.iterdir():
        if filepath.is_file():
            yield SourceFile(path=filepath, name=filepath.name, cleanup=None)
    
def import_rdf(lib: ZoteroLibrary, store: Store):
    logger.info(f"Importing RDF for '{lib.name}' into {lib.base_url}")

    for src in _iter_sources(lib):
        try:
            logger.info(f"Found: {src.name}")
            ext = src.path.suffix.lstrip(".").lower()

            if ext == "json":  # Zotero Export JSON
                logger.warning(
                    f"{src.name} will be parsed as Zotero Export JSON. "
                    f"For JSON-LD, use .jsonld extension instead!"
                )
                build_graph_for_library(lib, store, json_path=src.path)
                continue

            fmt = RdfFormat.from_extension(ext)
            if fmt is None:
                logger.info(f"Skipping unsupported file: {src.name}")
                continue

            before = len(store)
            store.bulk_load(
                path=src.path,
                format=fmt,
                base_iri=f"{lib.base_url}/items/",
                to_graph=NamedNode(lib.base_url),
            )
            after = len(store)
            logger.info(f"Imported {after - before} triples from {src.name}")

        finally:
            if src.cleanup:
                try:
                    src.cleanup()
                except Exception:
                    pass

def import_rdf_from_disk(lib: ZoteroLibrary, store: Store): # TODO deprecate
    subdir = Path(lib.load_from) if lib.load_from else Path(IMPORT_DIRECTORY) / lib.name
    subdir = subdir.resolve()
    if not subdir.is_dir():
        logger.warning(f"Directory not found for manual import: {subdir}")
        return

    logger.info(f"Importing RDF files for '{lib.name}' from {subdir} to {lib.base_url}")
    for filepath in subdir.iterdir():
        if not filepath.is_file():
            continue
        logger.info(f"Found: {filepath.name}")
        ext = filepath.suffix.lstrip('.').lower()
        if ext == "json":  # call for JSON
            logger.warning(f"A {filepath.name} will be parsed as Zotero Export JSON file. For JSON-LD, use .jsonld extension instead!")
            build_graph_for_library(lib, store, json_path=filepath)
            continue

        fmt = RdfFormat.from_extension(ext)
        if fmt is None:
            logger.info(f"Skipping unsupported file: {filepath.name}")
            continue

        before = len(store)
        store.bulk_load(
            path=filepath,
            format=fmt,
            base_iri=f"{lib.base_url}/items/",
            to_graph=NamedNode(lib.base_url)
        )
        after = len(store)
        logger.info(f"Imported {after - before} triples from {filepath.name}")

def add_rdf_from_dict(store: Store, subject: NamedNode | BlankNode, data: dict, ns_prefix: str, base_uri: str, map: dict, knowledge_base_graph: str = None, mapping_base_graph: str = None,language: str = None):
    GRAPH_URI = safeNamedNode(base_uri)
    
    if not knowledge_base_graph:
        knowledge_base_graph = base_uri
    if not mapping_base_graph:
        mapping_base_graph = knowledge_base_graph
    ENTITY_GRAPH_URI = safeNamedNode(knowledge_base_graph)
    ENTITY_UUID = uuid5(NAMESPACE_URL, knowledge_base_graph)
    MAP_GRAPH_URI = safeNamedNode(mapping_base_graph)

    white = map.get("white") or []
    black = map.get("black") or []
    lang_map = map.get("language_map", LANG_MAP)
    rdf_mapping = map.get("rdf_mapping") or {}    
    fuzzy_threshold = map.get("fuzzy", FUZZY)

    def get_field_map(key: str) -> dict:
        return rdf_mapping.get(key) or {}
    
    def get_properties(key: str) -> list:        
        field_map = get_field_map(key)        
        props = field_map.get("properties") or [key]        
        if len(props)>1:
            logger.debug(f"{len(props)} properties added for {key}: {props}")
        return make_iri(props, pref=ns_prefix, enforce_list=True)

    
    def zotero_property_map(predicate_str: str, object: str | dict | list, map: dict):
        
        field_map = get_field_map(predicate_str)
        
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
        
        def normalize_split_list(
            value: str | list,
            pattern: str = None # r"\s*;\s*",
        ) -> list:
            if value is None:
                return []
            
            seq = value if isinstance(value, list) else [value]
            if not pattern:
                return seq
            items = []
            try:            
                pattern = re.compile(pattern)
                for s in seq:
                    if isinstance(s, str):
                        s = s.strip()
                        if s:
                            items.extend(p for p in pattern.split(s) if p)
            except Exception as e:
                logger.error(f"Normalizing split of {seq} failed: {e}")
                return seq
            return items
          
        def make_entity(object_value, my_types, specific_threshold=fuzzy_threshold, on_create=None, return_entries=False):
            items = normalize_split_list(object_value, field_map.get("re_split"))
            nodes = []
            entries = []

            pool_store = quads_by_type(store, [MAP_ENTRY_TYPE], MAP_GRAPH_URI)

            def find_entry_for_target(target: NamedNode):
                for q in store.quads_for_pattern(None, safeNamedNode(MAP_TARGET), target, graph_name=MAP_GRAPH_URI):
                    return q.subject
                return None

            def ensure_entry(target: NamedNode, type_hints=None):
                entry = find_entry_for_target(target)
                if entry:
                    if type_hints:
                        for th in type_hints:
                            store.add(Quad(entry, safeNamedNode(MAP_TYPE_HINT), safeNamedNode(th), graph_name=MAP_GRAPH_URI))
                    return entry

                entry_suffix = uuid5(ENTITY_UUID, str(target))
                entry = safeNamedNode(f"{MAPPING_BASE}{entry_suffix}")

                store.add(Quad(entry, NamedNode(RDF_TYPE), safeNamedNode(MAP_ENTRY_TYPE), graph_name=MAP_GRAPH_URI))
                store.add(Quad(entry, safeNamedNode(MAP_TARGET), target, graph_name=MAP_GRAPH_URI))

                if type_hints:
                    for th in type_hints:
                        store.add(Quad(entry, safeNamedNode(MAP_TYPE_HINT), safeNamedNode(th), graph_name=MAP_GRAPH_URI))

                add_timestamp(store=store, node=entry, graph=MAP_GRAPH_URI)
                return entry

            for item in items:
                node, score, matched_label = fuzzy_match_label(
                    pool_store,
                    item,
                    threshold=specific_threshold,
                    graph_name=MAP_GRAPH_URI,
                    predicates=[MAP_LABEL],
                    regex=False
                )

                if not node:
                    iri_suffix = uuid5(ENTITY_UUID, item) if specific_threshold <= 100 else uuid4()
                    node = safeNamedNode(f"{knowledge_base_graph}/{iri_suffix}")

                    apply_rdf_types(
                        store=store,
                        node=node,
                        data={},
                        type_fields=my_types,
                        default_type=predicate_str,
                        base_ns=ENTITY_GRAPH_URI.value,
                        prefix_ns=ns_prefix
                    )

                    store.add(Quad(node, NamedNode(RDFS_LABEL), Literal(item), graph_name=ENTITY_GRAPH_URI))
                    add_timestamp(store=store, node=node, graph=ENTITY_GRAPH_URI)

                    if on_create:
                        on_create(node, item)

                    logger.debug(f"Created new {my_types[0]}: {item}")
                else:
                    logger.debug(f"{my_types[0].capitalize()} '{item}' matched as '{matched_label}' (score {score})")

                entry = ensure_entry(node, type_hints=my_types)
                ensure_mapping_literal(store, entry, item, safeNamedNode(MAP_LABEL), MAP_GRAPH_URI)

                nodes.append(node)
                entries.append(entry)

            return (nodes, entries) if return_entries else nodes

        try:
            if not object:
                return None
            
            if field_map.get("value"):
                new_object = field_map.get("value")
                logger.warning(f"Overwriting {predicate_str}: '{new_object}' instead of '{object}'")
                object = new_object

            fuzzy_threshold_specific = field_map.get("fuzzy") or fuzzy_threshold           

            if isinstance(object, dict): # dicts as named nodes

                ### TAGS ###
                if predicate_str == "tags" and "tag" in object:
                    type_nodes = make_iri(field_map.get("types", ["tag"]), ns_prefix, enforce_list=True)
                    tag_value = object.get("tag")
                    fuzzy_threshold_specific = field_map.get("fuzzy") or 100

                    tag_properties = make_iri(field_map.get("tag_properties", ["tag"]), ns_prefix, True)

                    def on_create_tag(tag_node, _label):
                        for key, val in object.items():
                            if not val:
                                continue
                            if key == "tag":
                                for t_pred in tag_properties:
                                    store.add(Quad(tag_node, safeNamedNode(t_pred), Literal(str(val)), graph_name=ENTITY_GRAPH_URI))
                            else:
                                store.add(Quad(tag_node, safeNamedNode(f"{ns_prefix}{key}"), Literal(str(val)), graph_name=ENTITY_GRAPH_URI))

                    (nodes, entries) = make_entity(
                        tag_value,
                        my_types=type_nodes,
                        specific_threshold=fuzzy_threshold_specific,
                        on_create=on_create_tag,
                        return_entries=True
                    )
                    tag_node = nodes[0]
                    entry = entries[0]

                    for key, val in object.items():
                        if val and isinstance(val, str) and key != "tag":
                            ensure_mapping_literal(store, entry, val, safeNamedNode(MAP_LABEL), MAP_GRAPH_URI)

                    return tag_node
                                
                ### CREATORS ###
                elif predicate_str == "creators":
                    if "name" in object and object["name"]:
                        label = object["name"]
                    else:
                        label = f"{object.get('lastName', '')}, {object.get('firstName', '')}".strip().strip(",")

                    type_nodes = make_iri(field_map.get("types", ["actor"]), ns_prefix, enforce_list=True)
                    role_types = make_iri(field_map.get("role_types", ["creatorRole"]), ns_prefix, enforce_list=True)
                    role_properties = make_iri(field_map.get("role_properties", "hasCreator"), ns_prefix, True)

                    fuzzy_threshold_specific = field_map.get("fuzzy") or fuzzy_threshold

                    role_node = safeNamedNode(f"{base_uri}/{uuid4()}")
                    apply_rdf_types(store, role_node, {}, role_types, "creatorRole", base_uri, ns_prefix)

                    creator_type = object.get("creatorType")  # "author"
                    if creator_type:
                        store.add(Quad(role_node, NamedNode(RDFS_LABEL), Literal(str(creator_type)), graph_name=GRAPH_URI))
                        store.add(Quad(role_node, safeNamedNode(f"{ns_prefix}creatorType"), safeNamedNode(f"{ns_prefix}{creator_type}"), graph_name=GRAPH_URI))
                        store.add(Quad(role_node, NamedNode(RDF_TYPE), safeNamedNode(f"{ns_prefix}{creator_type}"), graph_name=GRAPH_URI))

                    def on_create_creator(creator_node, _label):
                        for key, val in object.items():
                            if not val or key == "creatorType":
                                continue
                            store.add(Quad(creator_node, safeNamedNode(f"{ns_prefix}{key}"), Literal(str(val)), graph_name=ENTITY_GRAPH_URI))

                    (nodes, entries) = make_entity(
                        label,
                        my_types=type_nodes,
                        specific_threshold=fuzzy_threshold_specific,
                        on_create=on_create_creator,
                        return_entries=True
                    )
                    creator_node = nodes[0]
                    entry = entries[0]

                    ln, fn = object.get("lastName"), object.get("firstName")
                    if ln or fn:
                        ensure_mapping_literal(store, entry, f"{fn} {ln}".strip(), safeNamedNode(MAP_LABEL), MAP_GRAPH_URI)

                    for role_property in role_properties:
                        store.add(Quad(role_node, safeNamedNode(role_property), creator_node, graph_name=GRAPH_URI))

                    return role_node
                
                ### RELATIONS ###

                elif predicate_str == "relations" and ("dc:relation" in object or "owl:sameAs" in object or "dc:replaces" in object):
                    related_item = object.pop("dc:relation", None)
                    same_item = object.pop("owl:sameAs", None)
                    # TODO dc:replaces
                    related_items = related_item if isinstance(related_item, list) else ([related_item] if related_item is not None else [])
                    same_items = same_item if isinstance(same_item, list) else ([same_item] if same_item is not None else [])

                    rel_predicates = make_iri(field_map.get("relation_properties") or [predicate_str, PURL_RELATED],
                                            pref=ns_prefix, enforce_list=True)
                    same_predicates = make_iri(field_map.get("same_properties") or [OWL_SAME_AS],
                                            pref=ns_prefix, enforce_list=True)

                    for si in same_items:
                        if isinstance(si, str) and si.strip():
                            for p in same_predicates:
                                store.add(Quad(subject, safeNamedNode(p), safeNamedNode(si), graph_name=GRAPH_URI))

                    for ri in related_items:
                        if isinstance(ri, str) and ri.strip():
                            for p in rel_predicates:
                                store.add(Quad(subject, safeNamedNode(p), safeNamedNode(ri), graph_name=GRAPH_URI))

                    if object:
                        logger.warning(f"Dropping from relations: {object}")
                    return None
                
                # elif predicate_str == "links" and "alternate" in object: # links
                #     alternate_item = object.pop("alternate")
                #     href_alternate_item = alternate_item.pop("href")
                #     if href_alternate_item or alternate_item:
                #         logger.warning(f"Dropping from relations: {object}")
                #     return safeNamedNode(href_alternate_item)

                else: ### UNEXPECTED DICT ###
                    logger.info(f"RDF Mapping for unexpected dict in key {predicate_str}, {object}, {subject.value}")
                    b = BlankNode()
                    # for k, v in object.items():
                    #     preds = get_properties(k)
                    #     vals = v if isinstance(v, list) else [v]
                    #     for vv in vals:
                    #         oo = zotero_property_map(k, vv, map)
                    #         if oo:
                    #             for p in preds:
                    #                 store.add(Quad(b, safeNamedNode(p), oo, graph_name=GRAPH_URI))
                    return b
                
            # ENTITY #
            elif isinstance(object, str) and (field_map.get('fuzzy') or field_map.get('types')):
                logger.debug(f"UUID Entity for {predicate_str}: {object}")
                ent_types = make_iri(field_map.get("types", [predicate_str]), ns_prefix, True)
                
                return make_entity(object, ent_types,fuzzy_threshold_specific)                

            elif isinstance(object, (str, int, datetime, float)):

                def is_datatype(predicate: str, field_map: dict, datatype: str, defaults: list[str]) -> bool:                    
                    if "datatyping" in field_map:
                        return field_map.get("datatyping") == datatype                    
                    return predicate in defaults
                
                def resolve_literal_datatype(dt: str) -> NamedNode:
                    dt = str(dt).strip()
                    if dt.startswith("http://") or dt.startswith("https://"):
                        return safeNamedNode(dt)
                    if dt.lower().startswith("xsd:"):
                        local = dt.split(":", 1)[1]
                        return NamedNode(f"{XSD_NS}{local}")
                    if dt.startswith(XSD_NS):
                        return NamedNode(dt)
                    return safeNamedNode(dt)
                
                re_split = normalize_split_list(object, field_map.get("re_split"))
                
                vals = [object]
                if re_split:
                    vals = re_split
                
                literal_dt = field_map.get("datatype")                 
                objects = []
                          
                logger.debug("%s: %s %.100r", predicate_str, type(object), object)

                # ZOTERO Links #
                if predicate_str in ["parentItem"]: # parent items
                    return safeNamedNode(f"{base_uri}/items/{object}")
                elif predicate_str in ["collections", "parentCollection"]: # parent collections
                    return safeNamedNode(f"{base_uri}/collections/{object}")
                
                for val in vals:
                    val = str(val).strip()

                    # TITLE and LANGUAGE #
                    if language and is_datatype(predicate_str, field_map, "title", ["title", "bookTitle"]):
                        o = process_language_and_title(title=val,language_field=data.get("language", "en"),mapping=lang_map)
                        objects.append(o)
                    elif language and is_datatype(predicate_str, field_map, "language", ["language"]):
                        o = process_language_and_title(title=None, language_field=val,mapping=lang_map)
                        objects.append(o)

                    # URL #
                    elif is_datatype(predicate_str, field_map, "url", ["url", "dc:relation", "doi", "owl:sameAs", "href"]) and (val.startswith("http") or val.startswith("www.")): # url                    
                        o = safeNamedNode(val, enforce=True) 
                        objects.append(o)
                    
                    # DOI #
                    elif is_datatype(predicate_str, field_map, "doi", ["doi"]) and not val.startswith("http") and len(val) > 5:
                        o = safeNamedNode(f"https://doi.org/{val}".strip())
                        objects.append(o)
                    
                    # INT #
                    elif is_datatype(
                        predicate_str,
                        field_map,
                        "int",
                        ["numPages", "numberOfVolumes", "volume", "series number"],
                    ) and val.isdigit():
                        o = Literal(val,datatype=NamedNode(f"{XSD_NS}int"))
                        objects.append(o)
                    
                    # DATE #
                    elif is_datatype(predicate_str, field_map, "date", ["date"]):
                        date_val = parse_date(val)
                        match = re.search(r"\b(1[5-9]\d{2}|20\d{2}|2100)\b", val)
                        if re.fullmatch(r"\d{4}", val):
                            o = Literal(val, datatype=NamedNode(f"{XSD_NS}gYear"))                             
                        elif match:
                            o = Literal(match.group(1), datatype=NamedNode(f"{XSD_NS}gYear"))                             
                        elif isinstance(date_val, datetime):
                            o =      Literal(str(date_val.date().isoformat()), datatype=NamedNode(f"{XSD_NS}dateTime"))                         
                        else:
                            o = Literal(val)
                        objects.append(o)
                        
                    elif is_datatype(predicate_str, field_map, "datetime", ["dateModified", "accessDate", "dateAdded"]): # dateTime
                        o = Literal(val,datatype=NamedNode(f"{XSD_NS}dateTime"))
                        objects.append(o)
                    
                    elif (field_map.get("datatyping", "str") == "str") and literal_dt:
                        o = Literal(val, datatype=resolve_literal_datatype(literal_dt))
                        objects.append(o)
                    
                    # LITERAL #
                    else:
                        o = safeLiteral(val)
                        objects.append(o)
                
                return objects
            
            else:
                logger.error(f"Error: pass dict or str but got {type(object)}: {object}")

        except Exception as e:
            logger.error(f"Mapping error: {e}")
            return None
        
    #############################################
    ######## main function starts here! #########
    #############################################

    for field, value in data.items():
        try:
            predicates = get_properties(field)
            if white:
                if field not in white and not rdf_mapping.get(field):
                    logger.debug(f"Skipping {field} (not in whitelist)")
                    continue
            elif black and field in black:
                logger.debug(f"Skipping {field} (in blacklist)")
                continue

            values = value if isinstance(value, list) else [value]

            for item in values:
                obj = zotero_property_map(field, item, map) or None
                if obj:
                    obj = obj if isinstance(obj, list) else [obj]
                    for o in obj:
                        for pred in predicates:                    
                            predicate = safeNamedNode(pred)
                            if isinstance(o, (BlankNode, NamedNode, Literal)):
                                store.add(Quad(subject, predicate, o, graph_name=GRAPH_URI))
                                if isinstance(item, dict) and isinstance(o, (BlankNode)):                  
                                    # Recurse if unexpected dict that return BlankNode
                                    add_rdf_from_dict(store, o, item, ns_prefix, base_uri, map, knowledge_base_graph, mapping_base_graph=mapping_base_graph)
                            else:
                                logger.warning(f"Received unexpected item in mapping for {pred}: {o}")
        except Exception as e:
            logger.error(f"Invalid data for: [{field}, {value}]: {e}")
            continue        

def apply_rdf_types(store: Store, node: NamedNode, data: dict, type_fields: list[str], default_type: str, base_ns: str, prefix_ns: str = ZOT_NS):
    GRAPH_URI = NamedNode(base_ns)
    RDF_TYPE_NODE = NamedNode(RDF_TYPE)

    if not type_fields:
        if default_type:
            default_node = NamedNode(f"{prefix_ns}{default_type}")
            store.add(Quad(node, RDF_TYPE_NODE, default_node, graph_name=GRAPH_URI))
            logger.info(f"No type_fields for rdf:type – added default: {default_node}")
        else:
            logger.error(f"No rdf:type default: {default_node}")
    else:
        for field in type_fields:

            if field.startswith("_"):                
                type_str = data.get(field.lstrip("_"))
                if not type_str:
                    continue
            else:
                type_str = field.strip()

            type_str = make_iri(type_str, prefix_ns)

            try:
                store.add(Quad(node, RDF_TYPE_NODE, safeNamedNode(type_str), graph_name=GRAPH_URI))
                logger.debug(f"Added rdf:type: {type_str}")

            except Exception as e:
                logger.error(f"Invalid rdf:type at {node} for value '{type_str}': {e}")
                continue


_PLACEHOLDER_NODE_RE = re.compile(r"\{\{\s*node\s*\}\}|\{\s*node\s*\}")
_DATA_TOKEN_RE = re.compile(r"(?<!\w)_(?P<key>[A-Za-z0-9]+)(?!\w)")

def apply_additional_properties(
    store: Store,
    node: NamedNode,
    data: dict,
    specs: list[dict],
    base_ns: str,
    prefix_ns: str = ZOT_NS,
    context: str = None
):
    GRAPH_URI = NamedNode(base_ns)
    
    def resolve_data_token(s: str) -> str:
        if s is None:
            return s

        s = str(s)

        s = _PLACEHOLDER_NODE_RE.sub(node.value, s)

        def repl(m: re.Match) -> str:
            k = m.group("key")
            v = data.get(k)
            return str(v) if v is not None else m.group(0)

        return _DATA_TOKEN_RE.sub(repl, s)

    def resolve_value_spec(value_spec: str) -> str | None:
        if value_spec is None:
            return None

        value_spec = str(value_spec)

        if value_spec.startswith("_") and value_spec.strip() == value_spec and " " not in value_spec:
            raw = data.get(value_spec.lstrip("_"))
            return str(raw) if raw is not None and raw != "" else None

        return resolve_data_token(value_spec).strip()

    def resolve_rdf_format(fmt) -> RdfFormat | None:
        if not fmt:
            return None

        if isinstance(fmt, RdfFormat):
            return fmt

        s = str(fmt).strip()

        s = s.replace("-", "_").upper()

        if hasattr(RdfFormat, s):
            return getattr(RdfFormat, s)


        rf = RdfFormat.from_extension(s.lower())
        if rf:
            return rf

        rf = RdfFormat.from_media_type(s)
        if rf:
            return rf

        raise ValueError(f"Unknown RDF format: {fmt}")

    def resolve_to_graph(g) -> NamedNode | BlankNode | DefaultGraph | None:
        if g is None or g == "":
            return None

        g = resolve_value_spec(g)
        if not g:
            return None

        if str(g).lower() in {"default", "defaultgraph", "default_graph"}:
            return DefaultGraph()

        if str(g).startswith("_:"):
            return BlankNode(str(g)[2:])
        return safeNamedNode(str(g), enforce=True)

    for spec in specs:
        raw_value = None
        restrict_to = spec.get("add_to", ["item", "collection", "library"])
        if context and context in restrict_to:
            try:
                if spec.get("load"):
                    load_block = spec["load"] or []

                    load_specs = load_block if isinstance(load_block, list) else [load_block]

                    for load_spec in load_specs:
                        load_spec = load_spec or {}
                        restrict_to = load_spec.get("add_to", ["item", "collection", "library"])
                        
                        if context and context in restrict_to:
                            input_ = load_spec.get("input", None)
                            path_ = load_spec.get("path", None)

                            if input_ is not None:
                                input_ = resolve_data_token(input_)

                            if path_ is not None:
                                input_ = resolve_data_token(
                                    load_text_like(resolve_data_token(path_), label="RDF loading")
                                )
                                path_ = None

                            fmt = resolve_rdf_format(load_spec.get("format", None))

                            base_iri = load_spec.get("base_iri", None)
                            if base_iri is not None:
                                base_iri = resolve_value_spec(base_iri)

                            to_graph = resolve_to_graph(load_spec.get("to_graph", GRAPH_URI.value))
                            lenient = bool(load_spec.get("lenient", False))

                            store.load(
                                input=input_,
                                format=fmt,
                                path=path_,
                                base_iri=base_iri,
                                to_graph=to_graph,
                                lenient=lenient,
                            )

                    logger.debug("Loaded RDF via store.load() (one or many specs)")
                    continue

                property_str = spec.get("property")
                value_spec = spec.get("value")
                prefix = spec.get("prefix")
                named_node = spec.get("named_node", False)

                if not property_str or value_spec is None:
                    continue

                predicate = safeNamedNode(make_iri(property_str, prefix_ns))

                raw_value = resolve_value_spec(value_spec)
                if raw_value is None or raw_value == "":
                    continue

                if prefix:
                    raw_value = make_iri(raw_value, prefix)

                if named_node:
                    obj = safeNamedNode(raw_value, enforce=True)
                    store.add(Quad(node, predicate, obj, graph_name=GRAPH_URI))
                    logger.debug(f"Added named node {obj.value}")
                else:
                    obj = Literal(str(raw_value))
                    store.add(Quad(node, predicate, obj, graph_name=GRAPH_URI))

            except Exception as e:
                logger.error(f"Invalid additional spec at {node}: {e} (raw_value={raw_value})")
                continue

def build_graph_for_library(lib: ZoteroLibrary, store: Store, json_path:str | Path = None, write_to_store:bool = True):    
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

    if lib.save_to:
        try:
            path = Path(lib.save_to) #.join(EXPORT_DIRECTORY, "Zotero JSON", lib.name)
            path.mkdir(parents=True, exist_ok=True)
            path=path.resolve()
            if items:
                with (path / f"{lib.library_id}_items.json").open("w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2)

            if collections:
                with (path / f"{lib.library_id}_collections.json").open("w", encoding="utf-8") as f:
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
            lib.knowledge_base_graph,
            mapping_base_graph=lib.mapping_base_graph
        )
        apply_additional_properties(
            store,
            safeNamedNode(a_library_href),
            sample_entry["library"],
            map.get("additional", []),
            lib.base_url,
            ZOT_NS,
            "library"
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
            apply_additional_properties(store, node_uri, col_data, collection_additional, lib.base_url, ZOT_NS,"collection")

            add_rdf_from_dict(store, node_uri, col_data, ZOT_NS, lib.base_url, map, lib.knowledge_base_graph, mapping_base_graph=lib.mapping_base_graph)
            add_timestamp(store=store, node=node_uri, graph=GRAPH_URI)
        logger.info(f"--> Loaded {len(collections)} collections for {lib.name} to store")
    else:
        logger.warning("No collections!") if not json_path_items else None

    
    if items:
        all_items = []
        item_type_fields = lib.map.get("item_type") or []
        # ignore_tags = lib.map.get("ignore_tags") or []
        for item in items:
            node_uri = None
            try:
                item_data = item.get("data", {})
                
                creators = item_data.get("creators") or []
                if creators:
                    if "lastName" in creators[0]:
                        first_creator = creators[0].get("lastName") 
                    elif "name" in creators[0]:
                        first_creator = creators[0].get("name") 
                    else:
                        first_creator = "NO CREATOR"
                else:
                        first_creator = str(item_data.get("itemType", "Zotero item")).upper()

                title = item_data.get("title")
                if not title:
                    title = item_data.get("key","NO KEY")
                date = item_data.get("date")
                volume = item_data.get("volume")
                label = (
                            f"{first_creator}: {title}"
                            f"{f' vol. {volume}' if volume else ''}"
                            f"{f' ({date})' if date else ''}"
                        ).strip()

                language = item_data.get("language")
                key = item_data.get("key",uuid4())            
                node_uri = NamedNode(f"{lib.base_url}/items/{key}")

                if write_to_store == False:
                    all_items.append({
                        "creator": first_creator,
                        "title": title,
                        "date": date,
                        "label": label,
                        "language": language,
                        "key": key,
                        "node_uri": node_uri.value,
                        "item_type":  item_data.get("itemType") or "item",
                        "item_tags":  item_data.get("tags") or [],
                        "item_raw": item,
                    })
            except Exception as e:
                logger.error(f"Invalid data preparation for items!")
                continue    
            
            if write_to_store:
                try:
                    if lib.map.get("named_library"):
                        property_str = lib.map.get("named_library", "inLibrary")
                        store.add(Quad(node_uri, safeNamedNode(property_str) if property_str.startswith("http") else safeNamedNode(f"{ZOT_NS}{property_str}"), safeNamedNode(a_library_href), graph_name=GRAPH_URI))

                    if label:
                        store.add(Quad(node_uri, NamedNode(RDFS_LABEL), Literal(label), graph_name=GRAPH_URI))

                    apply_rdf_types(store, node_uri, item_data, item_type_fields, "item", lib.base_url, ZOT_NS)

                    item_additional = map.get("additional") or []
                    apply_additional_properties(store, node_uri, item_data, item_additional, lib.base_url, ZOT_NS,"item")

                    add_rdf_from_dict(store, node_uri, item_data, ZOT_NS, lib.base_url, map, lib.knowledge_base_graph,mapping_base_graph=lib.mapping_base_graph,language=language)
                    add_timestamp(store=store, node=node_uri, graph=GRAPH_URI)
        
                except Exception as e:
                    logger.error(f"Invalid data at {node_uri}. See next errors for details!")
                    continue
        if write_to_store:
            logger.info(f"--> Loaded {len(items)} items for {lib.name} to store")
        elif write_to_store == False:
            logger.info(f"--> Loaded {len(items)} items for {lib.name} to dictionnary")
            return all_items
    else:
        logger.warning("No items!") if not json_path_collections else None