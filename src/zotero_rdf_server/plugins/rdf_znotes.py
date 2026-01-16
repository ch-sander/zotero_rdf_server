import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional, Dict, Literal, Set, Mapping, Any
from pyoxigraph import Store, RdfFormat, NamedNode, BlankNode, DefaultGraph, Quad
from zotero_rdf_server.utils import ensure_rdf_format, safeNamedNode, store_remove_all, store_move_subject, load_dict_like
from zotero_rdf_server.logging_config import logger
import time, json, uuid
from zotero_rdf_server.config import *
from collections import defaultdict
from datetime import datetime
from zotero_rdf_server.models import ZoteroLibrary

try:
    from pyzotero import zotero
except ImportError:
    import subprocess, sys
    logger.warning("pyzotero not found. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyzotero"])

    try:
        from pyzotero import zotero
    except ImportError:
        logger.error("pyzotero could not be imported after installation.")
        raise

def safe_create_items(zot, items, retries=3, wait=5):
    for attempt in range(retries):
        try:
            return zot.create_items(items)
        except Exception as e:
            logger.warning(f"Retrying ({attempt+1}/{retries})...")           
            time.sleep(wait)
    raise Exception("Failed to create items after retries")

def describe_resources(source, input_format: str = "trig", output_format: str = "ttl", prefixes: dict = None, label_predicate: Optional[str] = RDFS_LABEL, type_predicate: Optional[str] = RDF_TYPE, graph: str | NamedNode = None) -> List[Tuple[str, str, str]]:
    
    if graph:
        graph = safeNamedNode(graph) if not isinstance(graph, NamedNode) else graph

    if isinstance(source, Store):
        store = source        
    else:
        store = Store()
        input_format=ensure_rdf_format(input_format)
        store.bulk_load(input=source, format=input_format)

    label_pred_uri = safeNamedNode(label_predicate) if label_predicate else None
    type_pred_uri = safeNamedNode(type_predicate) if type_predicate else NamedNode(RDF_TYPE)

    blocks = []
    subjects = set(q.subject for q in store.quads_for_pattern(None, None, None, graph))
    for s in subjects:
        substore = Store()
        substore.bulk_extend(store.quads_for_pattern(s, None, None, graph))
        substore.bulk_extend(store.quads_for_pattern(None, None, s, graph))

        rdf_type_quad = next(substore.quads_for_pattern(subject=s,predicate=type_pred_uri,object=None), None)
        label = next(substore.quads_for_pattern(subject=s,predicate=label_pred_uri,object=None), None)
        label_str = label.object.value if label else "n/a"
        
        rdf_type = str(rdf_type_quad.object.value) if rdf_type_quad else None
        if rdf_type and prefixes:
            prefixed_type = next((f"{p}:{rdf_type[len(uri):]}" for p, uri in prefixes.items() if rdf_type.startswith(uri)), rdf_type)
        else:
            prefixed_type = rdf_type

        output_format=ensure_rdf_format(output_format,RdfFormat.TURTLE)

        rdf_str = substore.dump(format=output_format,from_graph=graph if graph else DefaultGraph(),prefixes=prefixes, base_iri= str(graph.value).rstrip('/') + '/' if graph else None).decode("utf-8")
        blocks.append((prefixed_type, label_str, rdf_str))

    logger.info(f"Found {len(blocks)} blocks")
    return blocks

def rdf_to_znotes(blocks: List[Tuple[str, str, str]], api_key, library_id, library_type, collection_id=None, clear_collection=False, chunk_size = 50):
    if library_type == "groups": library_type == "group"
    zot = zotero.Zotero(library_id, library_type, api_key)

    if collection_id is None:
        new_collection = zot.create_collections([{"name": "Imported RDF Notes"}])
        collection_id = new_collection[0]['key'] if new_collection else None

    logger.info(f"Initialized Zotero access for collection {collection_id} in library {library_id}")

    if clear_collection:
        logger.warning(f"Delete all items in collection {collection_id} in library {library_id}")
        existing_items = zot.everything(zot.collection_items(collection_id))
        for item in existing_items:
            zot.delete_item(item)
        logger.info(f"Deleted {len(existing_items)} item in collection {collection_id} in library {library_id}")

    items = []
    for rdf_type, rdfs_label, block in blocks:
        html = ET.Element("div")
        h1 = ET.SubElement(html, "h1")
        if rdf_type and rdfs_label:
            h1.text = f"{rdf_type}: {rdfs_label}"
        elif rdfs_label:
            h1.text = rdfs_label
        else:
            h1.text = "RDF Resource"

        pre = ET.SubElement(html, "pre")
        pre.text = block

        note_html = ET.tostring(html, encoding='unicode', method='html')

        note_item = {
            'itemType': 'note',
            'note': note_html,
            'collections': [collection_id]
        }
        if rdf_type:
            note_item['tags'] = [{"tag": rdf_type}]

        items.append(note_item)
    
    responses = []
    logger.info(f"Start creating Zotero Notes...")
    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]        
        responses.append(safe_create_items(zot,chunk))
        logger.debug(f"{i}: Created {chunk_size} Zotero Notes")
    return responses

def znotes_to_rdf(api_key, library_id, library_type, collection_id, input_format: str = "ttl", output_format: str = None, prefixes: dict = None, graph: str | NamedNode = DefaultGraph()) -> Store | bytes:
    if library_type == "groups": library_type == "group"
    zot = zotero.Zotero(library_id, library_type, api_key)
    notes = zot.everything(zot.collection_items(collection_id))
    tmp_store = Store()
    input_format=ensure_rdf_format(input_format)
    for note in notes:
        html = note.get('data', {}).get('note', '')
        try:
            root = ET.fromstring(f"<div>{html}</div>")
            for pre in root.findall(".//pre"):
                if pre.text:
                    try:
                        tmp_store.bulk_load(input=pre.text.encode('utf-8'), format=input_format, to_graph=graph)
                    except (SyntaxError, ValueError):
                        continue
        except ET.ParseError:
            continue
    output_format=ensure_rdf_format(output_format,None)
    if output_format and isinstance(output_format, RdfFormat):
        return tmp_store.dump(format=output_format, prefixes=prefixes, from_graph=graph, base_iri= str(graph.value).rstrip('/') + '/' if graph else None)
    else:
        return tmp_store
    
def taxonomy_to_html(store: Store, kb_graph:str, map: dict = {}, query:str = None, add_header: bool = True) -> str:
    if query and "SELECT" in query.upper():
        sparql = query
    else:
        if map is None: map = {}
        LABEL_PROPS = map.get("labels", ["skos:prefLabel", RDFS_LABEL])
        PREFERRED_LANGS = ["en", "la", "de", ""] # ordered
        PROPS = map.get("props", [RDF_TYPE])
        OBJECTS = map.get("objects", [SKOS_CONCEPT])
        BROADER = map.get("broaders", [SKOS_BROADER])

        def fmt_term(term: str) -> str:
            t = term.strip()
            if t.startswith("<") and t.endswith(">"):
                return t
            if t.startswith(("http://", "https://", "urn:")):
                return f"<{t}>"
            return t

        objs  = " ".join(fmt_term(t) for t in OBJECTS)
        props = " | ".join(fmt_term(p) for p in PROPS)
        label_path = " | ".join(fmt_term(p) for p in LABEL_PROPS)
        broader_path = " | ".join(fmt_term(b) for b in BROADER)
        prefix_block = "\n".join(f"PREFIX {k}: <{v}>" for k, v in PREFIXES.items())

        sparql = f"""
        {prefix_block}

        SELECT ?c ?parent ?lp ?lv
        WHERE {{
        ?c ({props}) ?t .
        VALUES ?t {{ {objs} }}
        OPTIONAL {{ ?c ({broader_path}) ?parent . }}
        OPTIONAL {{ ?c ({label_path}) ?lv . FILTER(isLiteral(?lv)) }}
        }}
        """        
    KB_GRAPH = safeNamedNode(kb_graph)
    try:
        rows = list(store.query(query=sparql,default_graph=KB_GRAPH))
        logger.info(f"{len(rows)} rows found for taxonomy")
    except Exception as e:
        logger.error(f"Error for query {sparql}: {e}")

    def rank_lang(lang: str) -> int:
        # low = better
        lang = (lang or "").lower()
        try:
            return PREFERRED_LANGS.index(lang)
        except ValueError:
            for i, pref in enumerate(PREFERRED_LANGS):
                if pref and lang.startswith(pref + "-"):
                    return i + 0.1
            return 99

    labels = {}
    parents = {}
    nodes   = set()

    def get_binding(sol, var):
        try:
            return sol[var]
        except KeyError:
            return None
        
    for sol in rows:
        c_term = get_binding(sol, "c")
        if c_term is None:
            continue
        c_iri = str(c_term.value)
        nodes.add(c_iri)

        # parent
        p_term = get_binding(sol, "parent")
        if p_term is not None:
            parents[c_iri] = str(p_term.value)

        # label
        lv_term = get_binding(sol, "lv")
        if lv_term is not None:
            lit = lv_term
            cand = (rank_lang(lit.language), str(lit.value))
            best = labels.get(c_iri)
            if best is None or cand[0] < best[0]:
                labels[c_iri] = cand

    def short(iri: str) -> str:
        if iri in labels:
            return labels[iri][1]
        # Fallback
        if "#" in iri:
            return iri.rsplit("#", 1)[-1]
        return iri.rstrip("/").rsplit("/", 1)[-1]

    children = defaultdict(list)
    for child, parent in parents.items():
        children[parent].append(child)

    roots = [n for n in nodes if n not in parents]

    def sort_key(iri: str):
        return short(iri).lower()

    for lst in children.values():
        lst.sort(key=sort_key)
    roots.sort(key=sort_key)

    def render_node(iri: str, depth: int = 1, seen=None, max_depth=6) -> list[str]:
        if seen is None:
            seen = set()
        if (iri, depth) in seen:
            return []
        seen.add((iri, depth))

        depth = max(1, min(depth, max_depth))
        htag = f"h{depth}"
        label = short(iri)
        # add indentation via inline style
        indent_px = (depth - 1) * 20
        bits = [f'<{htag} style="margin-left: {indent_px}px;"><a href="{iri}">{label}</a></{htag}>']

        for ch in sorted(children.get(iri, []), key=sort_key):
            bits.extend(render_node(ch, depth + 1, seen, max_depth))
        return bits


    html_parts = []
    if add_header:
        html_parts.append("<pre>")
        html_parts.append(f"# Taxonomy Created {datetime.now().isoformat()}")
        html_parts.append(sparql.strip())
        html_parts.append("</pre>")


    for r in roots:
        html_parts.extend(render_node(r, 1))

    html = "\n".join(html_parts)

    return html

def html_to_note(
    *,
    html: str,
    api_key: str,
    library_id: str | int,
    library_type: Literal["user", "group", "groups"] = "group",
    collection_id: str = None,
    note_key: str | None = None,
    mode: Literal["overwrite", "append", "prepend"] = "overwrite",
    separator: str = "\n\n"
) -> str:
    if library_type == "groups": library_type == "group"
    zot = zotero.Zotero(library_id, library_type, api_key)

    if note_key:
        try:
            item = zot.item(note_key)
        except Exception:
            item = None
    else:
        item = None

    if item and "data" in item and item["data"].get("itemType") == "note":
        # Update existing
        current_html: str = item["data"].get("note", "")
        if mode == "overwrite":
            new_html = html
        elif mode == "append":
            new_html = (current_html + separator + html) if current_html else html
        elif mode == "prepend":
            new_html = (html + separator + current_html) if current_html else html
        else:
            raise ValueError(f"Unknown mode: {mode}")

        item["data"]["note"] = new_html
        resp = zot.update_item(item)
        note_key = resp["key"]
    else:
        # Create new
        new_item = {
            "itemType": "note",
            "note": html,
            "collections": [collection_id] if collection_id else [],
        }
        resp = zot.create_items([new_item])
        note_key = resp.get("key", resp)

    return note_key

def note_to_html(
    *,
    api_key: str,
    library_id: str | int,
    library_type: Literal["user", "group",  "groups"] = "group",
    note_key: str,
) -> str:
    if library_type == "groups": library_type == "group"
    zot = zotero.Zotero(library_id, library_type, api_key)
    item = zot.item(note_key)
    if "data" not in item or item["data"].get("itemType") != "note":
        raise ValueError(f"Item {note_key} is not a note or has no 'data' field.")
    current_html: str = item["data"].get("note", "")
    return current_html

def html_to_taxonomy(html:str,note_uri:str=None, mapping:dict = None, metadata:dict = None) -> Store:
    from zotero_rdf_server.plugins.parse_note import ParseNotePlugin
    
    mapping = {
        "@type": ["Taxonomy"],
        "Structure": {
            "xpath": ["//h1", "//h2", "//h3", "//h4", "//h5", "//h6"],
            "types": [SKOS_CONCEPT]
        },
        "IGNORE": {
            "xpath": ["//pre"]                
        },
        "@context" : {  "@vocab": "https://semantic-html.org/vocab#",
                        "structure": {
                                "@id": SKOS_BROADER,
                                "@type": "@id"
                        },
                        "sameAs": {
                                "@id": OWL_SAME_AS,
                                "@type": "@id"
                        },
                        "text": {
                                "@id": RDFS_LABEL,
                                "@type": "http://www.w3.org/2001/XMLSchema#string"
                        },
                    }
    } if not mapping else mapping

    plugin = ParseNotePlugin(mapping=mapping,metadata=metadata)
    result = plugin.run(html_str=html, note_uri=note_uri)
    try:
        tmp_store = Store()
        tmp_store.load(json.dumps(result["JSON-LD"]), format=RdfFormat.JSON_LD)
        # logger.debug(tmp_store.dump(format=RdfFormat.TRIG).decode("utf-8"))
        logger.debug("JSON-LD parsed")                    
    except Exception as e:
        logger.error(f"Error when parsing HTML: {e}")
    return tmp_store

def taxononmy_to_store(
    *,
    tax_store: Store, # source store for taxonomy from HTML
    kb_store: Store, # target store containing the knowledge base
    kb_graph: str,
    map: Optional[Dict[str, str]] = None,    
    label_language: Optional[str] = None   # e.g., "la", "de"; None -> untagged literal
) -> None:
    
    def _strip_modifier(text: str, prefixes: Dict[str, str]) -> tuple[str, Optional[str]]:
        """
        Returns (clean_text, modifier or None), e.g. ("Term", "delete") for "!Term".
        Only checks prefix at the very start of the string (after leading whitespace).
        """
        if text is None:
            return "", None
        t = text.lstrip()
        for name, sym in prefixes.items():
            if t.startswith(sym):
                return t[len(sym):].lstrip(), name
        return text, None

    def _merge_props(cfg: Optional[Mapping[str, Any]]) -> dict[str, Any]: # TODO not well tested
        defaults = {
            "source_props":       [RDF_TYPE],
            "source_objects":      [SKOS_CONCEPT],
            "source_sameAs":    OWL_SAME_AS,
            "source_text":      RDFS_LABEL,
            "source_broader":   SKOS_BROADER,
            "props": [RDF_TYPE],
            "objects": [SKOS_CONCEPT],
            "broaders":          [SKOS_BROADER],
            "labels":            [RDFS_LABEL],
            "sameAs": OWL_SAME_AS,
            "update_broader":   True,
            "update_label":     True,
            "update_modifiers": False,
            "modifier_prefixes": {
                    "add": "+",
                    "delete": "!",
                    "softmerge": "<",
                    "merge": "="
            }
        }
        if not cfg:
            return defaults
        out = defaults.copy()
        out.update({k: v for k, v in cfg.items() if isinstance(k, str) and v and (isinstance(v, (str, list, dict, bool)))})
        return out

    p = _merge_props(map)
    KB_GRAPH = safeNamedNode(kb_graph)
    update_broader   = bool(p.get("update_broader", True))
    update_label     = bool(p.get("update_label", True))
    update_modifiers = bool(p.get("update_modifiers", False))

    # Collect all Structure nodes and their sameAs/text
    # structures: node_id -> {"sameAs": <IRI or None>, "text": <str or None>}
    structures: Dict[str, Dict[str, Optional[str]]] = {}
    for t in p["source_props"]:
        for o in p["source_objects"]:
            for q in tax_store.quads_for_pattern(None, safeNamedNode(t), safeNamedNode(o), None):
                node = q.subject
                node_id = str(getattr(node, "value", node))
                entry = structures.setdefault(node_id, {"sameAs": None, "text": None, "modifier": None})

                # sameAs
                for qq in tax_store.quads_for_pattern(node, safeNamedNode(p["source_sameAs"]), None, None):
                    if getattr(qq.object, "value", None):
                        entry["sameAs"] = entry["sameAs"] or str(qq.object.value)

                # text (parse modifier if enabled)
                for qq in tax_store.quads_for_pattern(node, safeNamedNode(p["source_text"]), None, None):
                    if getattr(qq.object, "value", None):
                        raw = str(qq.object.value)
                        if update_modifiers:
                            clean, mod = _strip_modifier(raw, p["modifier_prefixes"])
                            # store first non-empty; first modifier wins
                            if entry["text"] is None and clean:
                                entry["text"] = clean
                            if entry["modifier"] is None and mod:
                                entry["modifier"] = mod
                            if entry["modifier"] == "add" and not entry.get("sameAs"):
                                entry["sameAs"] = f"{kb_graph}/{uuid.uuid4()}"
                        else:
                            if entry["text"] is None and raw:
                                entry["text"] = raw

    # Build child->parents mapping via inStructure, but resolved through sameAs
    child_to_parents: Dict[str, Set[str]] = {}
    touched_subjects: Set[str] = set()
    for rel in tax_store.quads_for_pattern(None, safeNamedNode(p["source_broader"]), None, None):
        child_node  = rel.subject
        parent_node = rel.object
        child_id  = str(getattr(child_node,  "value", child_node))
        parent_id = str(getattr(parent_node, "value", parent_node))
        child_same  = structures.get(child_id,  {}).get("sameAs")
        parent_same = structures.get(parent_id, {}).get("sameAs")
        if child_same and parent_same:
            child_to_parents.setdefault(child_same, set()).add(parent_same)
            touched_subjects.add(child_same)

    # Subjects that only need labels (no parents)
    if update_label:
        for node_id, data in structures.items():
            if data.get("sameAs") and data.get("text"):
                touched_subjects.add(data["sameAs"])

    # Apply updates in 'store'
    for subj_iri in sorted(touched_subjects):
        subj = safeNamedNode(subj_iri,enforce=True)
        # find structure entry (first one mapping to subj)
        # note: multiple nodes may map to same sameAs; first modifier wins by design
        mod = None
        text_val: Optional[str] = None
        for node_id, data in structures.items():
            if data.get("sameAs") == subj_iri:
                if mod is None:
                    mod = data.get("modifier")
                if text_val is None and data.get("text"):
                    text_val = data["text"]
                if mod and text_val:
                    break

        parents = sorted(child_to_parents.get(subj_iri, set()))

        if update_modifiers and mod == "delete":
            # Delete ALL triples where subject == this resource
            store_remove_all(kb_store, subj, None, g=KB_GRAPH)
            continue

        if update_modifiers and mod == "merge":            # 
            # move all (child, p, o) -> (parent, p, o)
            # delete any remaining child-subject triples
            for par in parents:
                store_move_subject(kb_store, subj, safeNamedNode(par), KB_GRAPH)
            store_remove_all(kb_store, subj, g=KB_GRAPH)
            continue

        if update_modifiers and mod == "softmerge":
            # Only link via owl:sameAs; no broader/label modifications for the child
            for par in parents:
                kb_store.add(Quad(subj, safeNamedNode(p["sameAs"]), safeNamedNode(par), KB_GRAPH))
            continue

        if update_modifiers and mod == "add":
            for p in p["props"]:
                for o in p["objects"]:
                    kb_store.add(Quad(subj, safeNamedNode(p), safeNamedNode(o), KB_GRAPH))

        # default or 'add' (or modifiers disabled)
        if update_broader:
            for b in p["broaders"]:
                store_remove_all(kb_store, subj, safeNamedNode(b), g=KB_GRAPH)
                for parent_iri in parents:
                    kb_store.add(Quad(subj, safeNamedNode(b), safeNamedNode(parent_iri), KB_GRAPH))

        if update_label and text_val:
            for l in p["labels"]:
                store_remove_all(kb_store, subj, safeNamedNode(l), g=KB_GRAPH)
                if label_language:
                    kb_store.add(Quad(subj, safeNamedNode(l), Literal(text_val, language=label_language), KB_GRAPH))
                else:
                    kb_store.add(Quad(subj, safeNamedNode(l), Literal(text_val), KB_GRAPH))


def pipeline(lib:ZoteroLibrary | dict, source_store:Store, job:Literal["writeNote", "writeStore"] = "writeNote", note_key: str = None, file:str = None):
    try:
        if isinstance(lib, ZoteroLibrary):
            BASE = lib.base_url
            lib_cfg = lib.sync
            sync_base_uri = lib_cfg.pop("base_uri", lib.base_url)
            tax_map = load_dict_like(lib.taxonomy.get("mapping", None), label="Taxonomy mapping")
            note_key = note_key or lib.taxonomy.get("note_key") or uuid.uuid4()
            logger.info("Loaded taxonomy config from ZoteroLibrary object.")

        elif isinstance(lib, dict):
            BASE = lib.get("graph")
            sync_base_uri = BASE
            tax_map = load_dict_like(lib.get("mapping", None), label="Taxonomy mapping")
            note_key = note_key or uuid.uuid4()
            logger.info("Loaded taxonomy config from dictionary.")

        else:
            logger.error("No valid taxonomy config: invalid 'lib' type.")
            return "no valid taxonomy config"

    except Exception as e:
        logger.error(f"No valid taxonomy config: {e}")
        return "no valid taxonomy config", e  
    
    if job == "writeNote":
        html_in = taxonomy_to_html(source_store, kb_graph = BASE, map=tax_map)
        if not file:
            res = html_to_note(html=html_in, note_key=note_key, **lib_cfg)
        else:
            EXPORT_DIRECTORY.mkdir(parents=True,exist_ok=True)
            html_file = EXPORT_DIRECTORY / file
            res = html_in
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(res)
        return res

    if job == "writeStore":        
        if file:
            html_file = IMPORT_DIRECTORY / file
            html_out = ""
            if html_file.is_file():
                with open(html_file, encoding="utf-8") as f:
                    html_out = f.read()
        else:
            lib_cfg.pop("collection_id")
            html_out = note_to_html(note_key=note_key,**lib_cfg)
 
        tax_store = html_to_taxonomy(html=html_out, note_uri=f"{sync_base_uri}/{note_key}", mapping=tax_map)
        taxononmy_to_store(tax_store=tax_store,kb_store=source_store, kb_graph= BASE, map=tax_map)
        return len(tax_store)