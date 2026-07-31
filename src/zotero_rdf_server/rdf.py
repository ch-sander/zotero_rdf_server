from uuid import uuid5, NAMESPACE_URL, uuid4
import json, re, requests, tempfile, time
from datetime import datetime
from dateutil import parser
from pathlib import Path
from requests.exceptions import RequestException
from typing import Iterable, Optional, Any

from .global_store import Store, Quad, NamedNode, Literal, RdfFormat, BlankNode
from pyoxigraph import parse, serialize
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
            resp = requests.get(url, stream=True, timeout=(5, 60), allow_redirects=True, headers=APP_USER)
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
    subpath = Path(lib.load_from) if lib.load_from else Path(IMPORT_DIRECTORY) / lib.name
    subpath = subpath.resolve()

    if subpath.is_file():
        yield SourceFile(path=subpath, name=subpath.name, cleanup=None)
        return

    if subpath.is_dir():
        for filepath in subpath.iterdir():
            if filepath.is_file():
                yield SourceFile(path=filepath, name=filepath.name, cleanup=None)
        return

    logger.warning(f"Path not found for manual import: {subpath}")
    return

def import_rdf(lib: ZoteroLibrary, store: Store):
    logger.info(f"Importing RDF for '{lib.name}' into {lib.base_uri}")

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
                base_iri=f"{lib.base_uri}/items/",
                to_graph=NamedNode(lib.base_uri),
            )
            after = len(store)
            logger.info(f"Imported {after - before} triples from {src.name}")

        finally:
            if src.cleanup:
                try:
                    src.cleanup()
                except Exception:
                    pass

def resolve_value_spec(value_spec: str, data: dict | None = None, node: str | None = None) -> str | None:
    """
    Resolve a value specification using the global template resolver.
    Supports _field, {{field}}, ${field}, and {{node}} placeholders.
    """
    if value_spec is None:
        return None

    resolved = resolve_template(str(value_spec), data=data, node=node)

    if resolved is None:
        return None

    resolved = resolved.strip()

    return resolved if resolved != "" else None
    
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

def resolve_to_graph(
    g,
    data: dict | None = None,
    node: str | None = None,
) -> NamedNode | BlankNode | DefaultGraph | None:
    if g is None or g == "":
        return None

    g = resolve_value_spec(g, data=data, node=node)

    if not g:
        return None

    value = str(g)

    if value.lower() in {"default", "defaultgraph", "default_graph"}:
        return DefaultGraph()

    if value.startswith("_:"):
        return BlankNode(value[2:])

    return safeNamedNode(value, enforce=True)

def load_rdf_from_spec(
    spec,
    *,
    context,
    data,
    node_value,
    store,
    default_graph_uri,
):
    load_block = spec.get("load")
    if not load_block:
        return False

    load_specs = load_block if isinstance(load_block, list) else [load_block]
    loaded = False

    for load_spec in load_specs:
        load_spec = load_spec or {}

        restrict_to = load_spec.get(
            "add_to",
            ["item", "collection", "library"],
        )
        if isinstance(restrict_to, str):
            restrict_to = [restrict_to]
        if context is not None and context not in restrict_to:
            continue

        input_ = load_spec.get("input")
        path_ = load_spec.get("path")

        if input_ is not None and path_ is not None:
            raise ValueError(
                "RDF load block may define only 'input' or 'path'"
            )
        
        if input_ is not None:
            input_ = resolve_template(
                input_,
                data=data,
                node=node_value,
            )

        if path_ is not None:
            resolved_path = resolve_template(
                path_,
                data=data,
                node=node_value,
            )

            input_ = resolve_template(
                load_text_like(
                    resolved_path,
                    label=None,
                ),
                data=data,
                node=node_value,
            )

            path_ = None

        fmt = ensure_rdf_format(
            load_spec.get("format", RdfFormat.TURTLE)
        )

        base_iri = load_spec.get("base_iri")
        
        graph_spec = load_spec.get("to_graph")
        if graph_spec in (None, ""):
            to_graph = default_graph_uri
        else:
            to_graph = resolve_to_graph(
                graph_spec,
                data=data,
                node=node_value,
            )

        lenient = bool(load_spec.get("lenient", False))

        store.load(
            input=input_,
            format=fmt,
            path=path_,
            base_iri=base_iri,
            to_graph=to_graph,
            lenient=lenient,
        )

        loaded = True

    return loaded

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
    rdf_mapping = load_dict_like(map.get("rdf_mapping"),default={},label="RDF Mappiing",required=True)    
    fuzzy_threshold = map.get("fuzzy", FUZZY)

    def get_field_map(key: str):
        return rdf_mapping.get(key) or {}

    def get_field_maps(key: str) -> list[dict]:
        result = get_field_map(key)
        return result if isinstance(result, list) else [result]
    
    def get_properties(key: str, field_map: dict) -> list:
        props = field_map.get("properties") or [key]

        if len(props) > 1:
            logger.debug(f"{len(props)} properties added for {key}: {props}")

        return make_iri(props, pref=ns_prefix, enforce_list=True)

    
    def zotero_property_map(predicate_str: str, object: str | dict | list, map: dict, field_map: dict):        
        
        def parse_date(text, dayfirst=True):
            text = text.strip()
            if re.fullmatch(r"\d{4}", text):
                year = int(text)
                return datetime(year, 1, 1)
            # RANGE_SEPARATORS = r"\s*[-–—]\s*"
            RANGE_SEPARATORS = r"(?:\s+-\s+|\s*[–—]\s*)" # more restrictive
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
            
        def get_language_tag(field_map: dict) -> str | None:
            lang = field_map.get("datatyping")

            if not lang:
                return None

            lang = str(lang).strip()

            if not lang.startswith("@"):
                return None

            lang = lang[1:].strip()

            return lang or None 
               
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
          
        def make_entity_deprecated(object_value, my_types, specific_threshold=fuzzy_threshold, on_create=None, return_entries=False):
            items = normalize_split_list(object_value, field_map.get("re_split"))
            nodes = []
            entries = []
            pool_store = quads_by_type(store, [MAP_ENTRY_TYPE], MAP_GRAPH_URI)
            my_rdf_types = [
                    make_iri(type_str, ns_prefix, False)
                    for field in my_types
                    if (type_str := resolve_template(field, data=data))
                ]
            pool_store = quads_by_type(pool_store, my_rdf_types, MAP_GRAPH_URI,type=NamedNode(MAP_TYPE_HINT))
            
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
                    # iri_suffix = uuid5(ENTITY_UUID, item) if specific_threshold < 100 else uuid4()
                    iri_suffix = stable_entity_uuid(item, my_types, ENTITY_UUID=ENTITY_UUID)
                    node = safeNamedNode(f"{knowledge_base_graph}/{iri_suffix}")

                    apply_rdf_types(
                        store=store,
                        node=node,
                        data=data,
                        type_fields=my_types,
                        default_type=predicate_str,
                        base_ns=ENTITY_GRAPH_URI.value,
                        prefix_ns=ns_prefix
                    )
                    label_language = get_language_tag(field_map)

                    store.add(Quad(
                        node,
                        NamedNode(RDFS_LABEL),
                        Literal(item, language=label_language) if label_language else Literal(item),
                        graph_name=ENTITY_GRAPH_URI
                    ))

                    entity_spec = map.get("entity", {})

                    load_rdf_from_spec(
                        entity_spec,
                        context=None,
                        data={
                            **data,
                            "value": item,
                            "label": item,
                        },
                        node_value=node.value,
                        store=store,
                        default_graph_uri=ENTITY_GRAPH_URI,
                    )
                    # add_timestamp(store=store, node=node, graph=ENTITY_GRAPH_URI)

                    if on_create:
                        on_create(node, item)

                    logger.debug(f"Created new {my_types[0]}: {item}")
                else:
                    logger.debug(f"{my_types[0].capitalize()} '{item}' matched as '{matched_label}' (score {score})")

                entry = ensure_entry(store, node, map_graph=MAP_GRAPH_URI, type_hints=my_rdf_types)
                ensure_mapping_literal(store, entry, item, safeNamedNode(MAP_LABEL), MAP_GRAPH_URI)

                nodes.append(node)
                entries.append(entry)

            return (nodes, entries) if return_entries else nodes
        
        def make_entity(
            object_value,
            my_types,
            specific_threshold=fuzzy_threshold,
            on_create=None,
            return_entries=False,
            type_source="mapping_or_rule",
        ):
            items = normalize_split_list(
                object_value,
                field_map.get("re_split"),
            )

            nodes = []
            entries = []

            my_rdf_types = [
                make_iri(type_str, ns_prefix, False)
                for field in my_types
                if (type_str := resolve_template(field, data=data))
            ]

            pool_store = quads_by_type(
                store,
                [MAP_ENTRY_TYPE],
                MAP_GRAPH_URI,
            )

            if my_rdf_types:
                pool_store = quads_by_type(
                    pool_store,
                    my_rdf_types,
                    MAP_GRAPH_URI,
                    type=NamedNode(MAP_TYPE_HINT),
                )

            def create_entity_node(item: str,rdf_types:list=None) -> NamedNode:
                effective_rdf_types = (
                        list(rdf_types)
                        if rdf_types is not None
                        else my_rdf_types
                    )
                iri_suffix = stable_entity_uuid(
                    item,
                    effective_rdf_types,
                    ENTITY_UUID=ENTITY_UUID,
                )

                node = safeNamedNode(
                    f"{knowledge_base_graph}/{iri_suffix}"
                )

                apply_rdf_types(
                    store=store,
                    node=node,
                    data=data,
                    type_fields=effective_rdf_types,
                    default_type=predicate_str,
                    base_ns=ENTITY_GRAPH_URI.value,
                    prefix_ns=ns_prefix,
                )

                label_language = get_language_tag(field_map)

                label_literal = (
                    Literal(item, language=label_language)
                    if label_language
                    else Literal(item)
                )

                store.add(
                    Quad(
                        node,
                        NamedNode(RDFS_LABEL),
                        label_literal,
                        graph_name=ENTITY_GRAPH_URI,
                    )
                )

                entity_spec = map.get("entity", {})

                load_rdf_from_spec(
                    entity_spec,
                    context=None,
                    data={
                        **data,
                        "value": item,
                        "label": item,
                    },
                    node_value=node.value,
                    store=store,
                    default_graph_uri=ENTITY_GRAPH_URI,
                )

                if on_create is not None:
                    on_create(node, item)

                entity_type = effective_rdf_types[0] if effective_rdf_types else "entity"

                logger.debug(
                    f"Created new {entity_type}: '{item}' as {node}"
                )

                return node

            def ensure_entry_target(
                entry: NamedNode,
                target: NamedNode,
            ) -> None:
                existing_target = next(
                    store.quads_for_pattern(
                        entry,
                        safeNamedNode(MAP_TARGET),
                        None,
                        graph_name=MAP_GRAPH_URI,
                    ),
                    None,
                )

                if existing_target is not None:
                    if existing_target.object != target:
                        logger.warning(
                            f"Mapping entry {entry} already targets "
                            f"{existing_target.object}; not replacing it with {target}"
                        )

                    return

                store.add(
                    Quad(
                        entry,
                        safeNamedNode(MAP_TARGET),
                        target,
                        graph_name=MAP_GRAPH_URI,
                    )
                )

                logger.debug(
                    f"Linked mapping entry {entry} to target {target}"
                )

            for item in items:

                node, score, matched_label, matched_entry = fuzzy_match_label(
                    pool_store=pool_store,
                    label=item,
                    threshold=specific_threshold,
                    graph_name=MAP_GRAPH_URI,
                    predicates=[MAP_LABEL],
                    regex=False,
                    max_matches=1,
                )

                if node is not None:
                    # A complete mapping was found.
                    entry = matched_entry

                    entity_type = my_rdf_types[0] if my_rdf_types else "entity"

                    logger.debug(
                        f"{entity_type.capitalize()} '{item}' matched "
                        f"'{matched_label}' with score {score}: {node}"
                    )

                elif matched_entry is not None:
                    # A mapping entry was found, but its target is missing.
                    entry = matched_entry

                    entry_label_quad = next(
                        pool_store.quads_for_pattern(
                            entry,
                            RDFS_LABEL_NODE,
                            None,
                            MAP_GRAPH_URI,
                        ),
                        None,
                    )

                    if entry_label_quad is None:
                        entry_label_quad = next(
                            pool_store.quads_for_pattern(
                                entry,
                                safeNamedNode(MAP_LABEL),
                                None,
                                MAP_GRAPH_URI,
                            ),
                            None,
                        )

                    entity_label = (
                        entry_label_quad.object.value
                        if entry_label_quad is not None
                        else item
                    )
                    entry_type_hints = [
                        q.object.value
                        for q in pool_store.quads_for_pattern(
                            entry,
                            safeNamedNode(MAP_TYPE_HINT),
                            None,
                            MAP_GRAPH_URI,
                        )
                    ]
                    effective_types = select_entity_types(
                        rule_types=my_rdf_types,
                        mapping_types=entry_type_hints,
                        type_source=type_source,
                    )
                    node = create_entity_node(entity_label,effective_types)

                    ensure_entry_target(
                        entry=entry,
                        target=node,
                    )

                    logger.info(
                        f"Repaired mapping entry {entry} for '{item}' "
                        f"by creating and linking target {node}"
                    )

                else:
                    # No mapping exists, so create both the entity and its entry.
                    effective_types = select_entity_types(
                        rule_types=my_rdf_types,
                        mapping_types=[],
                        type_source=type_source,
                    ) # not used as type_source may prevent any type if type_source="mapping"
                    node = create_entity_node(item,my_rdf_types)

                    entry = ensure_entry(
                        store,
                        node,
                        map_graph=MAP_GRAPH_URI,
                        type_hints=my_rdf_types,
                    )

                ensure_mapping_literal(
                    store,
                    entry,
                    item,
                    safeNamedNode(MAP_LABEL),
                    MAP_GRAPH_URI,
                )

                nodes.append(node)
                entries.append(entry)

            if return_entries:
                return nodes, entries

            return nodes
        

        try:
            if not object:
                return None
            
            if field_map.get("value"):
                new_object = resolve_template(
                    field_map.get("value"),
                    data=data,
                    node=subject.value
                )

                logger.warning(f"Overwriting {predicate_str}: '{new_object}' instead of '{object}'")
                object = new_object

            fuzzy_threshold_specific = field_map.get("fuzzy") or fuzzy_threshold           

            if isinstance(object, dict): # dicts as named nodes

                ### TAGS ###
                if predicate_str == "tags" and isinstance(object, dict) and "tag" in object:
                    type_nodes = field_map.get("types", ["Tag"])
                    type_source = field_map.get("entityTypeSource", "mapping_or_rule")
                    tag_value = object.get("tag")
                    fuzzy_threshold_specific = field_map.get("fuzzy") or 100

                    def on_create_tag(tag_node, _label):
                        add_rdf_from_dict(
                            store=store,
                            subject=tag_node,
                            data=object,
                            ns_prefix=ns_prefix,
                            base_uri=knowledge_base_graph,
                            map=map,
                            knowledge_base_graph=knowledge_base_graph,
                            mapping_base_graph=mapping_base_graph,
                            language=language
                        )

                    (nodes, entries) = make_entity(
                        tag_value,
                        my_types=type_nodes,
                        specific_threshold=fuzzy_threshold_specific,
                        on_create=on_create_tag,
                        return_entries=True,
                        type_source=type_source
                    )

                    tag_node = nodes[0]
                    entry = entries[0]

                    for key, val in object.items():
                        if val and isinstance(val, str) and key != "tag":
                            ensure_mapping_literal(
                                store,
                                entry,
                                val,
                                safeNamedNode(MAP_LABEL),
                                MAP_GRAPH_URI
                            )

                    return tag_node
                                
                ### CREATORS ###
                elif predicate_str == "creators":
                    if "name" in object and object["name"]:
                        label = object["name"]
                    else:
                        label = f"{object.get('lastName', '')}, {object.get('firstName', '')}".strip().strip(",")

                    type_nodes = field_map.get("types", ["Agent"])
                    type_source = field_map.get("entityTypeSource", "mapping_or_rule")

                    # Keep raw values here so "_creatorType" can be resolved by apply_rdf_types().
                    role_types = field_map.get("role_types", ["creatorRole"])
                    role_types = role_types if isinstance(role_types, list) else [role_types]

                    role_properties = make_iri(
                        field_map.get("role_properties", ["hasCreator"]),
                        ns_prefix,
                        True
                    )

                    creator_type_properties_raw = (
                        field_map["creator_type_properties"]
                        if "creator_type_properties" in field_map
                        else ["creatorType"]
                    )

                    creator_type_properties = (
                        make_iri(creator_type_properties_raw, ns_prefix, True)
                        if creator_type_properties_raw
                        else []
                    )

                    fuzzy_threshold_specific = field_map.get("fuzzy") or fuzzy_threshold

                    role_node = safeNamedNode(f"{base_uri}/{uuid4()}")

                    apply_rdf_types(
                        store,
                        role_node,
                        object,
                        role_types,
                        "creatorRole",
                        base_uri,
                        ns_prefix
                    )
                    entity_spec = field_map.get("role_add", map.get("entity", {}))

                    load_rdf_from_spec(
                        entity_spec,
                        context=None,
                        data={
                            **data,
                            **object,
                            "value": label,
                            "label": label,
                        },
                        node_value=role_node.value,
                        store=store,
                        default_graph_uri=GRAPH_URI,
                    )

                    creator_type = object.get("creatorType")
                    
                    if creator_type:
                        creator_type_objects = field_map.get("creator_type_objects", ["_creatorType"])
                        creator_type_objects = creator_type_objects if isinstance(creator_type_objects, list) else [creator_type_objects]

                        creator_type_types = field_map.get("creator_type_types", [])
                        creator_type_types = creator_type_types if isinstance(creator_type_types, list) else [creator_type_types]

                        creator_type_nodes = []

                        for creator_type_object in creator_type_objects:
                            resolved_object = resolve_template(
                                creator_type_object,
                                data=object,
                                node=role_node.value
                            )

                            if not resolved_object:
                                continue

                            creator_type_node = safeNamedNode(
                                make_iri(str(resolved_object), ns_prefix)
                            )

                            creator_type_nodes.append(creator_type_node)

                            for creator_type_property in creator_type_properties:
                                store.add(Quad(
                                    role_node,
                                    safeNamedNode(creator_type_property),
                                    creator_type_node,
                                    graph_name=GRAPH_URI
                                ))

                            for creator_type_type in creator_type_types:
                                store.add(Quad(
                                    creator_type_node,
                                    NamedNode(RDF_TYPE),
                                    safeNamedNode(make_iri(creator_type_type, ns_prefix)),
                                    graph_name=GRAPH_URI
                                ))
                                
                    def on_create_creator(creator_node, _label):
                        creator_data = {
                            key: val
                            for key, val in object.items()
                            if val and key != "creatorType"
                        }

                        add_rdf_from_dict(
                            store=store,
                            subject=creator_node,
                            data=creator_data,
                            ns_prefix=ns_prefix,
                            base_uri=knowledge_base_graph,
                            map=map,
                            knowledge_base_graph=knowledge_base_graph,
                            mapping_base_graph=mapping_base_graph,
                            language=language
                        )

                    (nodes, entries) = make_entity(
                        label,
                        my_types=type_nodes,
                        specific_threshold=fuzzy_threshold_specific,
                        on_create=on_create_creator,
                        return_entries=True,
                        type_source=type_source
                    )

                    creator_node = nodes[0]
                    entry = entries[0]

                    ln, fn = object.get("lastName"), object.get("firstName")
                    if ln or fn:
                        creator_label = f"{fn} {ln}".strip()
                        ensure_mapping_literal(
                            store,
                            entry,
                            creator_label,
                            safeNamedNode(MAP_LABEL),
                            MAP_GRAPH_URI
                        )
                    else:
                        creator_label = object.get("name", "unspecified").strip()
                    
                    role_label = f"{creator_label} as {str(creator_type).upper()}"
                    
                    store.add(Quad(
                        role_node,
                        NamedNode(RDFS_LABEL),
                        Literal(role_label, language='en'),
                        graph_name=GRAPH_URI
                    ))

                    for role_property in role_properties:
                        store.add(Quad(
                            role_node,
                            safeNamedNode(role_property),
                            creator_node,
                            graph_name=GRAPH_URI
                        ))

                    return role_node
                
                ### Metadata: Creator ###
                if predicate_str in {"lastModifiedByUser", "createdByUser" } and isinstance(object, dict) and "id" in object:
                    try:
                        type_nodes = make_iri(
                            field_map.get("types", ["User"]),
                            ns_prefix,
                            enforce_list=True
                        )

                        user_id = object.get("id") or uuid4()
                        user_label = object.get("name") or user_id
                        user_node = safeNamedNode(f"{knowledge_base_graph}/users/{user_id}")
                        pool_store = quads_by_type(store, type_nodes, safeNamedNode(knowledge_base_graph))
                        # ORCiD

                                
                        if any(pool_store.quads_for_pattern(user_node, None, None, None)):
                            return user_node
                        
                        orcid_user_nodes = list(pool_store.quads_for_pattern(None, NamedNode(OWL_SAME_AS), user_node, None))
                        if any(orcid_user_nodes):
                            for orcid_user_node in orcid_user_nodes:
                                if orcid_user_node.subject.value.startswith("https://orcid.org"):
                                    logger.warning(f"Set {user_node} to {orcid_user_node}")
                                    user_node=orcid_user_node
                                    return user_node   
                    
                        apply_rdf_types(
                            store=store,
                            node=user_node,
                            data={},
                            type_fields=type_nodes,
                            default_type="User",
                            base_ns=knowledge_base_graph,
                            prefix_ns=ns_prefix
                        )

                        store.add(Quad(
                            user_node,
                            NamedNode(RDFS_LABEL),
                            Literal(user_label),
                            graph_name=safeNamedNode(knowledge_base_graph)
                        ))
                        add_rdf_from_dict(
                            store=store,
                            subject=user_node,
                            data=object,
                            ns_prefix=ns_prefix,
                            base_uri=knowledge_base_graph,
                            map=map,
                            knowledge_base_graph=knowledge_base_graph,
                            mapping_base_graph=mapping_base_graph,
                        )

                        # TODO
                        # user_spec = map.get("entity", {})

                        # load_rdf_from_spec(
                        #     user_spec,
                        #     context=None,
                        #     data={
                        #         **data,
                        #         **object,
                        #         "value": user_label,
                        #         "label": user_label,
                        #         "user_id": str(user_id),
                        #     },
                        #     node_value=user_node.value,
                        #     store=store,
                        #     default_graph_uri=safeNamedNode(knowledge_base_graph),
                        # )
                        add_timestamp(store=store, node=user_node,graph=safeNamedNode(knowledge_base_graph))
                    except Exception as e:
                        logger.error(f"Adding metadata failed: {e}")
                        raise

                    return user_node
                                
                ### RELATIONS ###

                elif predicate_str == "relations" and ("dc:relation" in object or "owl:sameAs" in object or "dc:replaces" in object):
                    related_item = object.pop("dc:relation", None)
                    same_item = object.pop("owl:sameAs", None)
                    replaces_item = object.pop("dc:replaces", None)

                    # TODO dc:replaces
                    related_items = related_item if isinstance(related_item, list) else ([related_item] if related_item is not None else [])
                    same_items = same_item if isinstance(same_item, list) else ([same_item] if same_item is not None else [])
                    replaces_items = replaces_item if isinstance(replaces_item, list) else ([replaces_item] if replaces_item is not None else [])
                    same_items.extend([x for x in replaces_items if x is not None])
                    rel_predicates = make_iri(field_map.get("relation_properties") or [predicate_str, PURL_RELATED],
                                            pref=ns_prefix, enforce_list=True)
                    same_predicates = make_iri(field_map.get("same_properties") or [OWL_SAME_AS],
                                            pref=ns_prefix, enforce_list=True)

                    for si in same_items:
                        if isinstance(si, str) and si.strip():
                            for p in same_predicates:
                                store.add(Quad(subject, safeNamedNode(p), safeNamedNode(normalize_iri_scheme(si)), graph_name=GRAPH_URI))

                    for ri in related_items:
                        if isinstance(ri, str) and ri.strip():
                            for p in rel_predicates:
                                store.add(Quad(subject, safeNamedNode(p), safeNamedNode(normalize_iri_scheme(ri)), graph_name=GRAPH_URI))

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
                ent_types = field_map.get("types", [ucfirst(predicate_str)])
                type_source = field_map.get("entityTypeSource", "mapping_or_rule")
                return make_entity(object, ent_types,fuzzy_threshold_specific,type_source=type_source)                

            # LITERALS #
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

                    # LangString #
                    elif get_language_tag(field_map):
                        o = Literal(val, language=get_language_tag(field_map))
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
                            o =      Literal(str(date_val.date().isoformat()), datatype=NamedNode(f"{XSD_NS}date"))                         
                        else:
                            o = Literal(val)
                        objects.append(o)
                        
                    elif is_datatype(predicate_str, field_map, "datetime", ["dateModified", "accessDate", "dateAdded"]): # dateTime
                        date_val = parse_date(val)

                        if isinstance(date_val, datetime):
                            o = Literal(
                                date_val.isoformat(timespec="seconds"),
                                datatype=NamedNode(f"{XSD_NS}dateTime")
                            )
                        else:
                            o = Literal(val)

                        objects.append(o)                        
                        # o = Literal(val,datatype=NamedNode(f"{XSD_NS}dateTime"))
                        # objects.append(o)
                    
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
        for field_map in get_field_maps(field):
            try:
                predicates = get_properties(field, field_map)
                if white:
                    if field not in white and not rdf_mapping.get(field):
                        logger.debug(f"Skipping {field} (not in whitelist)")
                        continue
                elif black and field in black:
                    logger.debug(f"Skipping {field} (in blacklist)")
                    continue

                values = value if isinstance(value, list) else [value]

                for item in values:
                    obj = zotero_property_map(field, item, map, field_map=field_map) or None

                    if obj:
                        obj = obj if isinstance(obj, list) else [obj]

                        for o in obj:
                            if isinstance(o, (BlankNode, NamedNode, Literal)):
                                loaded = load_rdf_from_spec(
                                    field_map,
                                    context=None,
                                    data=data,
                                    node_value=o.value,
                                    store=store,
                                    default_graph_uri=ENTITY_GRAPH_URI,
                                )

                                if loaded:
                                    logger.debug("Add data for %s in %s", field, o.value)
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

def run_update_queries(lib: ZoteroLibrary, store: Store):
    if not lib.update_queries:
        return

    total = len(lib.update_queries)
    before_all = len(store)

    logger.warning(
        "Running %s SPARQL UPDATE queries for %s; store size before=%s",
        total,
        lib.base_uri,
        before_all,
    )

    for i, q in enumerate(lib.update_queries, start=1):
        update = None
        before = len(store)

        try:
            if not isinstance(q, dict) or "query" not in q:
                logger.warning(
                    "Skipping invalid SPARQL UPDATE config entry #%s for %s: %r",
                    i,
                    lib.base_uri,
                    q,
                )
                continue

            update = load_text_like(q["query"], label="SPARQL UPDATE query")

            logger.warning(
                "Running SPARQL UPDATE #%s/%s for %s; store size before=%s",
                i,
                total,
                lib.base_uri,
                before,
            )

            store.update(update=update)

            after = len(store)

            logger.warning(
                "Finished SPARQL UPDATE #%s/%s for %s; store size after=%s; delta=%+d",
                i,
                total,
                lib.base_uri,
                after,
                after - before,
            )

        except Exception as e:
            failed_size = None
            try:
                failed_size = len(store)
            except Exception:
                pass

            logger.error(
                "SPARQL UPDATE #%s/%s for %s failed: %s; store size now=%s\n\n%s",
                i,
                total,
                lib.base_uri,
                e,
                failed_size,
                update or q,
                exc_info=True,
            )
            raise

    after_all = len(store)

    logger.warning(
        "Finished all SPARQL UPDATE queries for %s; store size before=%s; after=%s; delta=%+d",
        lib.base_uri,
        before_all,
        after_all,
        after_all - before_all,
    )

def index_citation_sources(
    lib: ZoteroLibrary,
    store: Store,
) -> list[dict[str, Any]]:
    """Index all configured citation sources serially."""
    if not lib.citation_sources:
        return []

    try:
        from .plugins.extraction.citation2rdf import index_document
        from .utils import _index_url, _sources_from_sparql
    except ImportError:
        logger.exception("Failed to import citation extraction plugin")
        raise

    indexed_results: list[dict[str, Any]] = []

    for source_config in lib.citation_sources:
        source = source_config["source"]

        context = load_dict_like(
            source_config.get("context"),
            default={},
            label="Context for citation extraction",
        )

        try:
            kind = source["kind"]

            if kind == "path":
                source_results = [
                    index_document(
                        source["path"],
                        document_uri=source_config["document_uri"],
                        graph_uri=source_config.get("graph_uri"),
                        context=context,
                    )
                ]

            elif kind == "url":
                source_results = [
                    _index_url(
                        source["url"],
                        index_document=index_document,
                        document_uri=source_config["document_uri"],
                        graph_uri=source_config.get("graph_uri"),
                        context=context,
                    )
                ]


            elif kind == "sparql":
                query = load_text_like(
                    source["query"],
                    label="SPARQL for citation extraction",
                )

                url_variable = source.get("url_variable", "url")
                doc_variable = source.get("document_uri_variable", "doc")

                citation_sources = list(
                    _sources_from_sparql(
                        store,
                        query,
                        url_variable=url_variable,
                        document_uri_variable=doc_variable,
                    )
                )

                source_results = [
                    _index_url(
                        url,
                        index_document=index_document,
                        document_uri=document_uri,
                        graph_uri=source_config.get("graph_uri"),
                        context=context,
                    )
                    for url, document_uri in citation_sources
                ]



            else:
                raise ValueError(
                    f"Unsupported citation source kind: {kind}"
                )

            for result in source_results:
                if not result:
                    continue

                meta_path = source_config.get("meta")
                if meta_path:
                    meta = load_dict_like(
                        meta_path,
                        default={},
                        label="Meta for citation extraction",
                    )

                    graph = result.get("@graph")
                    if graph:
                        graph[0].update(meta)
                    else:
                        result.update(meta)

                tmp_store = Store()

                graph_uri = (
                    source_config.get("graph_uri")
                    or lib.base_uri
                )
                to_graph = (
                    safeNamedNode(graph_uri)
                    if graph_uri
                    else None
                )

                tmp_store.load(
                    json.dumps(result),
                    format=RdfFormat.JSON_LD,
                    to_graph=to_graph,
                )

                store.extend(tmp_store)
                indexed_results.append(result)

                logger.info(
                    "Extended store with citations: %s quads",
                    len(tmp_store),
                )

        except Exception:
            logger.exception(
                "Failed extracting citations from source %r in %s",
                source,
                lib.name,
            )
            continue

    return indexed_results

def index_citation_sources_deprecated(lib: ZoteroLibrary, store: Store) -> list[dict[str, Any]]:
    """Index all configured citation sources serially."""
    if not lib.citation_sources:
        return
    
    result = None

    try:
        from .plugins.extraction.citation2rdf import index_document
        import urllib.request
    except:
        logger.error("Failed to import citation extraction plugin")
        raise

    for source_config in lib.citation_sources:
        try:
            source = source_config["source"]
            logger.info(f"Extracting citations from {source}")
            context = load_dict_like(source_config.get("context"),default={},label="Context for citation extraction")
            kind = source["kind"]
            
            if source["kind"] == "path":
                result = index_document(
                    source["path"],
                    document_uri=source_config["document_uri"],
                    graph_uri=source_config.get("graph_uri"),
                    context=context,
                )

            elif source["kind"] == "url":
                with tempfile.TemporaryDirectory(
                    prefix="zotero-citation-download-"
                ) as temp_dir:
                    path = Path(temp_dir) / "document"

                    urllib.request.urlretrieve(
                        source["url"],
                        path,
                    )

                    result = index_document(
                        path,
                        document_uri=source_config["document_uri"],
                        graph_uri=source_config.get("graph_uri"),
                        context=context,
                    )
            else:
                raise ValueError(
                    f"Unsupported citation source kind: {source['kind']}"
                )
    
        except Exception as e:
            logger.error(f"Failed extracting citations from {lib.name}; {e}")
            continue
        meta_path = source_config.get("meta")
        if meta_path and result:
            meta = load_dict_like(meta_path,default={},label="Meta for citation extraction")

            if "@graph" in result:
                result["@graph"][0].update(meta)
            else:
                result.update(meta)

        tmp_store = Store()

        graph_uri = source_config.get("graph_uri") or lib.base_uri
        to_graph = safeNamedNode(graph_uri) if graph_uri else None

        tmp_store.load(
            json.dumps(result),
            format=RdfFormat.JSON_LD,
            to_graph=to_graph,
        )

        store.extend(tmp_store)
        logger.info(f"Extended store with citations: {len(tmp_store)} quads!")


def apply_rdf_types(store: Store, node: NamedNode, data: dict, type_fields: list[str], default_type: str, base_ns: str, prefix_ns: str = ZOT_NS):
    GRAPH_URI = NamedNode(base_ns)
    RDF_TYPE_NODE = NamedNode(RDF_TYPE)

    if not type_fields:
        if default_type:
            default_node = NamedNode(f"{prefix_ns}{default_type}")
            store.add(Quad(node, RDF_TYPE_NODE, default_node, graph_name=GRAPH_URI))
            logger.info(f"No type_fields for rdf:type – added default: {default_node}")
        else:
            logger.error("No rdf:type default configured")
    else:
        for field in type_fields:

            type_str = resolve_template(field, data=data, node=node.value)

            if not type_str:
                continue

            type_str = make_iri(type_str, prefix_ns)

            try:
                store.add(Quad(node, RDF_TYPE_NODE, safeNamedNode(type_str), graph_name=GRAPH_URI))
                logger.debug(f"Added rdf:type: {type_str}")

            except Exception as e:
                logger.error(f"Invalid rdf:type at {node} for value '{type_str}': {e}")
                continue

_TEMPLATE_RE = re.compile(
    r"""
    (?:
        \{\{\s*(?P<braces_cap>\^?)(?P<braces>[A-Za-z_][A-Za-z0-9_-]*)\s*\}\}
    )
    |
    (?:
        \$\{\s*(?P<dollar_cap>\^?)(?P<dollar>[A-Za-z_][A-Za-z0-9_-]*)\s*\}
    )
    """,
    re.VERBOSE
)

def resolve_template(
    s: str,
    data: dict | None = None,
    node: str | None = None,
) -> str:

    if s is None:
        return s

    s = str(s)
    data = data or {}

    # exact _field shortcut
    #
    # _publisher -> data["publisher"]
    # _Publisher -> ucfirst(data["publisher"])
    #               fallback: data["Publisher"]
    m = re.fullmatch(r"_([A-Za-z][A-Za-z0-9_-]*)", s)
    if m:
        token = m.group(1)

        if token[:1].isupper():
            key = token[:1].lower() + token[1:]

            if key in data:
                return ucfirst(data.get(key))

            if token in data:
                return data.get(token)

            return None

        return data.get(token)

    def repl(m: re.Match) -> str:
        key = m.group("braces") or m.group("dollar")

        capitalize = bool(
            m.group("braces_cap")
            or m.group("dollar_cap")
        )

        if key == "node":
            value = node
        elif key == "now":
            value =  (
                        datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
        else:
            value = data.get(key)

        if value is None:
            return m.group(0)

        return ucfirst(value) if capitalize else str(value)
    
    return _TEMPLATE_RE.sub(repl, s)

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

    for spec in specs:
        raw_value = None
        restrict_to = spec.get("add_to", ["item", "collection", "library"])
        if context and context in restrict_to:
            try:
                loaded = load_rdf_from_spec(
                    spec,
                    context=context,
                    data=data,
                    node_value=node.value,
                    store=store,
                    default_graph_uri=GRAPH_URI,
                )
                if loaded:
                    logger.debug("Loaded RDF via store.load() (one or many specs)")
                    continue

                property_str = spec.get("property")
                value_spec = spec.get("value")
                prefix = spec.get("prefix")
                named_node = spec.get("named_node", False)

                if not property_str or value_spec is None:
                    continue

                predicate = safeNamedNode(make_iri(property_str, prefix_ns))

                raw_value = resolve_value_spec(value_spec, data=data, node=node.value)
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
                logger.info(f"Stored JSON for {lib.library_id} in {path}: {len(items)} items")
            if collections:
                with (path / f"{lib.library_id}_collections.json").open("w", encoding="utf-8") as f:
                    json.dump(collections, f, ensure_ascii=False, indent=2)      
                logger.info(f"Stored JSON for {lib.library_id} in {path}: {len(collections)} collections")
        except Exception as e:
            logger.error(f"Error saving JSON for {lib.library_id} to {lib.save_to}: {e}")

    map = lib.map
    sample_entry = (items or collections or [None])[0]
    library_data = sample_entry.get("library", {})

    if sample_entry is not None:        
        logger.debug(f"Example JSON: {sample_entry}")
    else:
        logger.warning(f"No items or collections found for library {lib.name}")
    library_property = lib.map.get("named_library")
    library_uri = safeNamedNode(lib.base_uri)
    a_library_href = library_href(library_data)
    logger.info(f"[{lib.name} at {a_library_href}] Fetched {len(items) if items else 0} items and {len(collections) if collections else 0} collections.")

    GRAPH_URI = safeNamedNode(lib.base_uri)

    if library_property and sample_entry and sample_entry.get("library"): # TODO write_to_store
        library_type = map.get("library_type") or []
        apply_rdf_types(store, library_uri, library_data, library_type, "Library", lib.base_uri, ZOT_NS)   
        # store.add(Quad(library_uri, NamedNode(RDF_TYPE), safeNamedNode(f"{ZOT_NS}Library"), graph_name=GRAPH_URI))
        # store.add(Quad(library_uri, NamedNode(OWL_SAME_AS), safeNamedNode(a_library_href), graph_name=GRAPH_URI))
        add_rdf_from_dict(
            store,
            library_uri,
            library_data,
            ZOT_NS,
            lib.base_uri,
            map,
            lib.knowledge_base_graph,
            mapping_base_graph=lib.mapping_base_graph
        )
        apply_additional_properties(
            store,
            library_uri,
            library_data,
            map.get("additional", []),
            lib.base_uri,
            ZOT_NS,
            "library"
        )
        # add_timestamp(store=store, node=library_uri, graph=GRAPH_URI)

    if collections: # TODO write_to_store
        logger.info(f"Loading {len(collections)} collections for {lib.name} to store")
        for col in collections:
            col_data = col["data"]
            if library_property:
                col_data[library_property] = str(library_uri.value)       
            col_data_long = merge_with_prefix(col_data, library_data, "library_")
            key = col_data.get("key", uuid4())
            node_uri = NamedNode(f"{lib.base_uri}/collections/{key}")
            # if library_property:
            #     col_data_long[library_property] = str(library_uri.value)
                # property_str = lib.map.get("named_library", "inLibrary")
                # store.add(Quad(node_uri, safeNamedNode(property_str) if property_str.startswith("http") else safeNamedNode(f"{ZOT_NS}{property_str}"), library_uri, graph_name=GRAPH_URI))

            collection_type_fields = map.get("collection_type") or []
            apply_rdf_types(store, node_uri, col_data, collection_type_fields, "Collection", lib.base_uri, ZOT_NS)           

            additional = map.get("additional") or []
            apply_additional_properties(store, node_uri, col_data_long, additional, lib.base_uri, ZOT_NS,"collection")

            add_rdf_from_dict(store, node_uri, col_data, ZOT_NS, lib.base_uri, map, lib.knowledge_base_graph, mapping_base_graph=lib.mapping_base_graph)
            # add_timestamp(store=store, node=node_uri, graph=GRAPH_URI)

            metadata = col.get("meta")
            if metadata and isinstance(metadata,dict) and lib.metadata_graph:
                # additional = lib.metadata_map.get("additional") or []
                logger.info(f"Adding collection metadata to {lib.metadata_graph} for {str(node_uri)}")
                apply_additional_properties(store, node_uri, metadata, additional, lib.metadata_graph, ZOT_NS, "metadata")

                add_rdf_from_dict(store, node_uri, metadata, ZOT_NS, lib.metadata_graph,map, lib.metadata_graph, mapping_base_graph=lib.metadata_graph)
                # add_timestamp(store=store, node=node_uri, graph=safeNamedNode(lib.metadata_graph))

        logger.info(f"--> Loaded {len(collections)} collections for {lib.name} to store")
    else:
        logger.warning("No collections!") if not json_path_items else None

    
    if items:
        all_items = []
        item_type_fields = lib.map.get("item_type") or []
        start = time.perf_counter()
        last = start
        total = len(items)
        mark = LIMIT or min(1000, 10 ** (len(str(total)) - 1))
        # ignore_tags = lib.map.get("ignore_tags") or []
        logger.info(f"Loading {total} items for {lib.name} to store")
        for i, item in enumerate(items, 1):
            if i % mark == 0:
                now = time.perf_counter()

                elapsed = now - start
                rate = mark / (now - last) * 60
                last = now

                remaining = elapsed / i * (total - i)
                hours, rest = divmod(int(remaining), 3600)
                minutes, seconds = divmod(rest, 60)

                logger.info(
                    f"Processing item {i}/{total} for {lib.name} "
                    f"- {rate:.0f} items/min "
                    f"- ca. {hours}:{minutes:02}:{seconds:02} left"
                )
            node_uri = None
            try:
                item_data = item.get("data", {})
                if library_property:
                        item_data[library_property] = str(library_uri.value)
                item_data_long = merge_with_prefix(item_data, library_data, "library_")
                item_bib = item.get("bib")

                item_citation = item.get("citation")
                item_meta = item.get("meta", {})
                item_creatorSummary = item_meta.get("creatorSummary")
                item_parsedDate = item_meta.get("parsedDate")
                creators = item_data.get("creators") or []
                if not item_creatorSummary and creators:
                    if "lastName" in creators[0]:
                        first_creator = creators[0].get("lastName") 
                    elif "name" in creators[0]:
                        first_creator = creators[0].get("name") 
                    else:
                        first_creator = "NO CREATOR"
                else:
                        first_creator = str(item_creatorSummary) or str(item_data.get("itemType", "Zotero item")).upper()

                title = item_data.get("title")
                if not title:
                    title = item_data.get("key","NO KEY")
                date = item_parsedDate or item_data.get("date")
                volume = item_data.get("volume")
                label = html_to_string(item_bib or item_citation) or (
                            f"{first_creator}: {title}"
                            f"{f' vol. {volume}' if volume else ''}"
                            f"{f' ({date})' if date else ''}"
                        ).strip()
                
                language = item_data.get("language")
                key = item_data.get("key",uuid4())            
                node_uri = NamedNode(f"{lib.base_uri}/items/{key}")

                if write_to_store == False:
                    all_items.append({
                        "creator": first_creator,
                        "title": title,
                        "date": date,
                        "label": label,
                        "language": language,
                        "key": key,
                        "node_uri": node_uri.value,
                        "item_type":  item_data.get("itemType") or "Item",
                        "item_tags":  item_data.get("tags") or [],
                        "item_raw": item,
                    })
            except Exception as e:
                logger.error(f"Invalid data preparation for items!")
                continue    
            
            if write_to_store:
                try:
                    # if library_property:
                    #     item_data_long[library_property] = str(library_uri.value)
                    #     property_str = lib.map.get("named_library", "inLibrary")
                    #     store.add(Quad(node_uri, safeNamedNode(property_str) if property_str.startswith("http") else safeNamedNode(f"{ZOT_NS}{property_str}"), library_uri, graph_name=GRAPH_URI))

                    if label:
                        store.add(Quad(node_uri, NamedNode(RDFS_LABEL), Literal(label), graph_name=GRAPH_URI))

                    if item_bib:
                        store.add(Quad(node_uri, safeNamedNode(f"{ZOT_NS}bib"), Literal(item_bib, datatype=NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML")), graph_name=GRAPH_URI))

                    apply_rdf_types(store, node_uri, item_data, item_type_fields, "Item", lib.base_uri, ZOT_NS)

                    additional = map.get("additional") or []
                    apply_additional_properties(store, node_uri, item_data_long, additional, lib.base_uri, ZOT_NS,"item")

                    add_rdf_from_dict(store, node_uri, item_data, ZOT_NS, lib.base_uri, map, lib.knowledge_base_graph,mapping_base_graph=lib.mapping_base_graph,language=language)
                    # add_timestamp(store=store, node=node_uri, graph=GRAPH_URI)

                    metadata = item.get("meta")
                    if metadata and isinstance(metadata,dict) and lib.metadata_graph:
                        # additional = lib.metadata_map.get("additional") or []
                        logger.debug(f"Adding item metadata to {lib.metadata_graph} for {str(node_uri)}")
                        apply_additional_properties(store, node_uri, metadata, additional, lib.metadata_graph, ZOT_NS, "metadata")

                        add_rdf_from_dict(store, node_uri, metadata, ZOT_NS, lib.metadata_graph,map, lib.metadata_graph, mapping_base_graph=lib.metadata_graph)
                        # add_timestamp(store=store, node=node_uri, graph=safeNamedNode(lib.metadata_graph))
                        
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

def purge_dangling_mappings(
    store: Store,
    *,
    entity_graph: NamedNode,
    map_graph: NamedNode,
    delete: bool = False,
    delete_if_missing_target: bool = True,
    delete_if_target_not_in_kb: bool = False,
):
    missing_target: list[NamedNode] = []
    target_not_in_kb: list[NamedNode] = []

    candidates = list(iter_mapping_entries(store, map_graph))

    for entry in candidates:
        target = get_target_of_entry(store, entry, map_graph)

        if not target:
            if delete_if_missing_target:
                missing_target.append(entry)
            continue

        if delete_if_target_not_in_kb and not has_any_facts(store, target, entity_graph):
            target_not_in_kb.append(entry)

    to_delete = missing_target + target_not_in_kb

    if delete:
        for entry in to_delete:
            delete_subject_facts(store, entry, map_graph)

    return {
        "entity_graph": str(entity_graph),
        "map_graph": str(map_graph),
        "candidates": len(candidates),
        "delete_if_missing_target": delete_if_missing_target,
        "delete_if_target_not_in_kb": delete_if_target_not_in_kb,
        "missing_target": [str(n) for n in missing_target],
        "target_not_in_kb": [str(n) for n in target_not_in_kb],
        "deleted": len(to_delete) if delete else 0,
    }

def purge_orphan_entities(
    store: Store,
    *,
    not_mapped_only: bool = True,
    entity_graph: NamedNode,
    map_graph: NamedNode,
    graphs_to_check_for_objects: list[NamedNode] | None = None,
    delete: bool = False,
    keep_if_sameas_subject: bool = False,
    ignore_object_predicates: set[NamedNode] | None = None,
):
    ignore_object_predicates = ignore_object_predicates or set()

    orphans: list[NamedNode] = []
    candidates = list(iter_entities(store, entity_graph))

    for node in candidates:
        if keep_if_sameas_subject:
            if any(
                True
                for _ in store.quads_for_pattern(
                    node, NamedNode(OWL_SAME_AS), None, graph_name=entity_graph
                )
            ):
                continue

        mapped = is_mapping_target(store, node, map_graph)

        if mapped:
            continue

        if not_mapped_only:
            orphans.append(node)
            continue

        referenced = is_object_somewhere(
            store,
            node,
            graphs=graphs_to_check_for_objects,
            ignore_predicates=ignore_object_predicates,
        )
        if not referenced:
            orphans.append(node)

    if delete:
        for node in orphans:
            delete_subject_facts(store, node, entity_graph)

    return {
        "entity_graph": str(entity_graph),
        "map_graph": str(map_graph),
        "not_mapped_only": not_mapped_only,
        "candidates": len(candidates),
        "orphans": [str(n) for n in orphans],
        "deleted": len(orphans) if delete else 0,
    }

def merge_entities_deprecated(
    store: Store,
    old: NamedNode,
    new: NamedNode,
    *,
    only_redirect: bool = False,
    map_graph: NamedNode,
    KB_graph: NamedNode,
    dedup_mapping: bool = False,
):
    if old == new:
        return
    if map_graph is None or KB_graph is None:
        raise ValueError("map_graph and KB_graph are required")
    
    # Copy KB facts
    migrate_facts(store, old, new, KB_graph)

    # Replace object occurrences globally (all graphs)
    replace_object_everywhere(store, old, new, graph=None)

    # Retarget mapping entries old->new, optionally merge duplicate entries
    retarget_mapping_entries(store, old, new, map_graph, dedup=dedup_mapping)

    if only_redirect:
        store.add(Quad(new, NamedNode(OWL_SAME_AS), old, KB_graph))
    else:
        delete_subject_facts(store, old, KB_graph)

def merge_entities(
    store: Store,
    old: NamedNode,
    new: NamedNode,
    *,
    only_redirect: bool = False,
    delete_old_only: bool = False,
    map_graph: NamedNode,
    KB_graph: NamedNode,
    dedup_mapping: bool = False,
) -> None:
    if old == new:
        return

    if map_graph is None or KB_graph is None:
        raise ValueError("map_graph and KB_graph are required")

    if only_redirect and delete_old_only:
        raise ValueError(
            "only_redirect and delete_old_only cannot both be true"
        )

    if not only_redirect and not delete_old_only:
        migrate_facts(store, old, new, KB_graph)

    replace_object_everywhere(
        store,
        old,
        new,
        graph=None,
    )

    retarget_mapping_entries(
        store,
        old,
        new,
        map_graph,
        dedup=dedup_mapping,
    )

    if only_redirect:
        store.add(
            Quad(
                new,
                NamedNode(OWL_SAME_AS),
                old,
                KB_graph,
            )
        )
    else:
        delete_subject_facts(store, old, KB_graph)

def sync_kb_mapping_deprecated(
    store: Store,
    *,
    entity_graph: NamedNode,
    map_graph: NamedNode,
    direction: str = "auto",     # "auto" | "mapping_to_kb" | "kb_to_mapping" | "both"
    seed_mapping_labels: bool = True,
    create_missing_entities: bool = True,
    default_entity_types: list[str] | None = None,
    entity_spec: dict | None = None,
):

    has_mapping = any(True for _ in iter_mapping_entries(store, map_graph))
    has_entities = any(True for _ in iter_entities(store, entity_graph))

    if direction == "auto":
        if has_mapping and not has_entities:
            direction = "mapping_to_kb"
        elif has_entities and not has_mapping:
            direction = "kb_to_mapping"
        else:
            direction = "both"

    created_entities = 0
    created_entries = 0
    seeded_labels = 0

    # --- A) Mapping -> KB ---
    if direction in ("mapping_to_kb", "both"):
        for entry in iter_mapping_entries(store, map_graph):
            target = get_target_of_entry(store, entry, map_graph)
            if not target:
                logger.warning(f"[SYNC] mapping entry without target: {entry}")
                continue

            if has_any_facts(store, target, entity_graph):
                continue

            if not create_missing_entities:
                logger.warning(f"[SYNC] missing entity for target {target}, creation disabled")
                continue

            lbl = first_literal(store, entry, MAP_LABEL_NODE, map_graph) or str(target)

            type_fields = get_type_hints_of_entry(store, entry, map_graph) or (default_entity_types or [])

            apply_rdf_types(
                store=store,
                node=target,
                data={},
                type_fields=type_fields,
                default_type="unidentified",
                base_ns=entity_graph.value,
            )
            store.add(Quad(target, RDFS_LABEL_NODE, Literal(lbl), graph_name=entity_graph))

            # TODO
            if entity_spec:
                load_rdf_from_spec(
                    entity_spec,
                    context=None,
                    data={
                        "value": lbl,
                        "label": lbl,
                        "types": type_fields,
                    },
                    node_value=target.value,
                    store=store,
                    default_graph_uri=entity_graph,
                )
            else:
                add_timestamp(store=store, node=target, graph=entity_graph)

            created_entities += 1
            logger.info(f"[SYNC] created missing entity {target} (label='{lbl}') from mapping")

    # --- B) KB -> Mapping ---
    if direction in ("kb_to_mapping", "both"):
        for ent in iter_entities(store, entity_graph):
            type_hints = get_rdf_types_of_entity(store, ent, entity_graph)
            entry_before = find_entries_for_target(store, ent, map_graph)
            entry = ensure_entry(store, ent, map_graph, type_hints=type_hints)
            entry_after = find_entries_for_target(store, ent, map_graph)

            if not entry_before and entry_after:
                created_entries += 1

            if seed_mapping_labels:
                lbl = first_literal(store, ent, RDFS_LABEL_NODE, entity_graph)
                if lbl:
                    before = len(list(store.quads_for_pattern(entry, MAP_LABEL_NODE, None, graph_name=map_graph)))
                    ensure_mapping_literal(store, entry, lbl, graph=map_graph)
                    after = len(list(store.quads_for_pattern(entry, MAP_LABEL_NODE, None, graph_name=map_graph)))
                    if after > before:
                        seeded_labels += 1

    return {
        "direction": direction,
        "had_mapping": has_mapping,
        "had_entities": has_entities,
        "created_entities": created_entities,
        "created_entries": created_entries,
        "seeded_labels": seeded_labels,
    }

def sync_kb_mapping(
    store: Store,
    *,
    entity_graph: NamedNode,
    map_graph: NamedNode,
    direction: str = "auto",
    seed_mapping_labels: bool = True,
    create_missing_entities: bool = True,
    create_missing_mappings: bool = True,
    default_entity_types: list[str] | None = None,
    entity_spec: dict | None = None,
):
    has_mapping = any(iter_mapping_entries(store, map_graph))
    has_entities = any(iter_entities(store, entity_graph))

    if direction == "auto":
        if has_mapping and not has_entities:
            direction = "mapping_to_kb"
        elif has_entities and not has_mapping:
            direction = "kb_to_mapping"
        else:
            direction = "both"

    created_entities = 0
    created_entries = 0
    seeded_labels = 0
    skipped_missing_entries = 0

    # Mapping -> KB
    if direction in ("mapping_to_kb", "both"):
        for entry in iter_mapping_entries(store, map_graph):
            target = get_target_of_entry(store, entry, map_graph)

            if not target:
                logger.warning("[SYNC] mapping entry without target: %s", entry)
                continue

            if has_any_facts(store, target, entity_graph):
                continue

            if not create_missing_entities:
                logger.warning(
                    "[SYNC] missing entity for target %s, creation disabled",
                    target,
                )
                continue

            lbl = (
                first_literal(store, entry, MAP_LABEL_NODE, map_graph)
                or str(target)
            )

            type_fields = (
                get_type_hints_of_entry(store, entry, map_graph)
                or default_entity_types
                or []
            )

            apply_rdf_types(
                store=store,
                node=target,
                data={},
                type_fields=type_fields,
                default_type="unidentified",
                base_ns=entity_graph.value,
            )

            store.add(
                Quad(
                    target,
                    RDFS_LABEL_NODE,
                    Literal(lbl),
                    graph_name=entity_graph,
                )
            )

            if entity_spec:
                load_rdf_from_spec(
                    entity_spec,
                    context=None,
                    data={
                        "value": lbl,
                        "label": lbl,
                        "types": type_fields,
                    },
                    node_value=target.value,
                    store=store,
                    default_graph_uri=entity_graph,
                )
            else:
                add_timestamp(
                    store=store,
                    node=target,
                    graph=entity_graph,
                )

            created_entities += 1

    # KB -> Mapping
    if direction in ("kb_to_mapping", "both"):
        for entity in iter_entities(store, entity_graph):
            existing_entries = find_entries_for_target(
                store,
                entity,
                map_graph,
            )

            if existing_entries:
                entry = existing_entries[0]
            elif create_missing_mappings:
                type_hints = get_rdf_types_of_entity(
                    store,
                    entity,
                    entity_graph,
                )

                entry = ensure_entry(
                    store,
                    entity,
                    map_graph,
                    type_hints=type_hints,
                )
                created_entries += 1
            else:
                skipped_missing_entries += 1
                logger.debug(
                    "[SYNC] no mapping entry for entity %s, creation disabled",
                    entity,
                )
                continue

            if seed_mapping_labels:
                lbl = first_literal(
                    store,
                    entity,
                    RDFS_LABEL_NODE,
                    entity_graph,
                )

                if lbl:
                    before = len(
                        list(
                            store.quads_for_pattern(
                                entry,
                                MAP_LABEL_NODE,
                                None,
                                graph_name=map_graph,
                            )
                        )
                    )

                    ensure_mapping_literal(
                        store,
                        entry,
                        lbl,
                        graph=map_graph,
                    )

                    after = len(
                        list(
                            store.quads_for_pattern(
                                entry,
                                MAP_LABEL_NODE,
                                None,
                                graph_name=map_graph,
                            )
                        )
                    )

                    if after > before:
                        seeded_labels += 1

    return {
        "direction": direction,
        "had_mapping": has_mapping,
        "had_entities": has_entities,
        "created_entities": created_entities,
        "created_entries": created_entries,
        "seeded_labels": seeded_labels,
        "skipped_missing_entries": skipped_missing_entries,
    }


def generate_ontospy_doc():
    ZOT_ONTOLOGY_TTL = load_ontology(ZOT_ONTOLOGY_SOURCE)
    if ZOT_ONTOLOGY_TTL.exists():
        try:
            ensure_import("ontospy==2.1.1", requirements=None)
            import ontospy
            from ontospy.gendocs.viz.viz_html_multi import KompleteViz
            output_path = STATIC_ONTODOC_DIRECTORY
            logger.info(f"Rendering Ontospy for {ZOT_ONTOLOGY_TTL}")
            g = ontospy.Ontospy(str(ZOT_ONTOLOGY_TTL))
            logger.info(f"Creating Ontospy docs at {output_path}")
            v = KompleteViz(g, "Zotero RDF Server")
            v.build(str(output_path))
            logger.info(f"Created Ontospy docs at {output_path}")
            return Path(output_path / "index.html")
        except Exception as e:
            logger.error(f"Ontospy failed: {e}")

def load_ontology(source: str | Path) -> Path:
    source = str(source)
    is_url = urlparse(source).scheme in {"http", "https"}

    from tempfile import NamedTemporaryFile
    if is_url:
        response = requests.get(source, timeout=30)
        response.raise_for_status()

        content = response.content
        base_iri = response.url
        rdf_format = (
            RdfFormat.from_media_type(
                response.headers.get("Content-Type", "")
            )
            or RdfFormat.from_extension(
                Path(urlparse(response.url).path).suffix.lstrip(".")
            )
        )
        filename = Path(urlparse(response.url).path).stem or "ontology"
    else:
        path = Path(source)

        if not path.is_file():
            raise FileNotFoundError(path)

        rdf_format = RdfFormat.from_extension(path.suffix.lstrip("."))

        if rdf_format == RdfFormat.TURTLE:
            return path

        content = path.read_bytes()
        base_iri = path.resolve().as_uri()
        filename = path.stem

    if rdf_format is None:
        raise ValueError(f"Could not detect RDF format: {source}")

    with NamedTemporaryFile(
        mode="wb",
        prefix=f"{filename}-",
        suffix=".ttl",
        delete=False,
    ) as target:
        if rdf_format == RdfFormat.TURTLE:
            target.write(content)
        else:
            serialize(
                parse(
                    input=content,
                    format=rdf_format,
                    base_iri=base_iri,
                    without_named_graphs=True,
                ),
                output=target,
                format=RdfFormat.TURTLE,
            )

        return Path(target.name)
    
# End