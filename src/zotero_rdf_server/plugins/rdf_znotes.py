import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional, Dict, Literal, Set
from pyoxigraph import Store, RdfFormat, NamedNode, BlankNode, DefaultGraph, Quad
from zotero_rdf_server.utils import ensure_rdf_format, safeNamedNode, _remove_all
from zotero_rdf_server.logging_config import logger
import time, json
from zotero_rdf_server.config import *
from collections import defaultdict
from datetime import datetime

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

def describe_resources(source, input_format: str = "trig", output_format: str = "ttl", prefixes: dict = None, label_predicate: Optional[str] = "http://www.w3.org/2000/01/rdf-schema#label", type_predicate: Optional[str] = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", graph: str | NamedNode = None) -> List[Tuple[str, str, str]]:
    
    if graph:
        graph = safeNamedNode(graph) if not isinstance(graph, NamedNode) else graph

    if isinstance(source, Store):
        store = source        
    else:
        store = Store()
        input_format=ensure_rdf_format(input_format)
        store.bulk_load(input=source, format=input_format)

    label_pred_uri = safeNamedNode(label_predicate) if label_predicate else None
    type_pred_uri = safeNamedNode(type_predicate) if type_predicate else NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")

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
    
def taxonomy_to_html(store: Store, map: dict = {}, query:str = None, add_header: bool = True) -> str:
    if query and "SELECT" in query.upper():
        sparql = query
    else:
        LABEL_PROPS = map.get("labels", ["skos:prefLabel", RDFS_LABEL])
        PREFERRED_LANGS = ["en", "la", "de", ""] # ordered
        TYPES = map.get("types", [SKOS_CONCEPT])
        BROADER = map.get("broaders", [SKOS_BROADER])

        def fmt_term(term: str) -> str:
            t = term.strip()
            if t.startswith("<") and t.endswith(">"):
                return t
            if t.startswith(("http://", "https://", "urn:")):
                return f"<{t}>"
            return t

        values_types  = " ".join(fmt_term(t) for t in TYPES)
        label_path = " | ".join(fmt_term(p) for p in LABEL_PROPS)
        broader_path = " | ".join(fmt_term(b) for b in BROADER)
        prefix_block = "\n".join(f"PREFIX {k}: <{v}>" for k, v in PREFIXES.items())

        sparql = f"""
        {prefix_block}

        SELECT ?c ?parent ?lp ?lv
        WHERE {{
        ?c rdf:type ?t .
        VALUES ?t {{ {values_types} }}
        OPTIONAL {{ ?c ({broader_path}) ?parent . }}
        OPTIONAL {{ ?c ({label_path}) ?lv . FILTER(isLiteral(?lv)) }}
        }}
        """        

    try:
        rows = list(store.query(query=sparql)) # TODO graph default_graph=DefaultGraph()
        print(f"{len(rows)} rows found for taxonomy") # TODO logger.info
    except Exception as e:
        print(f"Error for query {sparql}: {e}") # TODO logger.error

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
    note_key: str | None = None,
    mode: Literal["overwrite", "append", "prepend"] = "overwrite",
    separator: str = "\n\n"
) -> str:
    """
    Create or update a Zotero note with given HTML. 
    Returns the web URI of the note.
    """
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
            "collections": [],
        }
        resp = zot.create_items([new_item])[0]
        note_key = resp["key"]

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
    # TODO
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
                                "@type": "@value"
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
    tax_store: Store, # parsed HTML note as RDF
    kb_store: Store, # knowledge base
    map: Optional[Dict[str, str]] = None,
    update_broader: bool = True,
    update_label: bool = True,
    label_language: Optional[str] = None   # e.g., "la", "de"; None -> untagged literal
) -> None:
    
    def _merge_props(cfg: Optional[Dict[str, str]]) -> Dict[str, str]:
        defaults = {
            "type_props":       [RDF_TYPE],
            "source_type":      SKOS_CONCEPT,
            "source_sameAs":    OWL_SAME_AS,
            "source_text":      RDFS_LABEL,
            "source_broader":   SKOS_BROADER,
            "broaders":          [SKOS_BROADER],
            "labels":            [RDFS_LABEL],
            "update_broader":   True,
            "update_label":     True
        }
        if not cfg:
            return defaults
        out = defaults.copy()
        out.update({k: v for k, v in cfg.items() if isinstance(k, str) and v and (isinstance(v, str) or isinstance(v, list))})
        return out
    
    p = _merge_props(map)
    update_broader = p.pop("update_broader", update_broader)
    update_label = p.pop("update_label", update_label)

    # 1) Collect all Structure nodes and their sameAs/text
    #    structures: node_id -> {"sameAs": <IRI or None>, "text": <str or None>}
    structures: Dict[str, Dict[str, Optional[str]]] = {}
    for t in p["type_props"]:
        for q in tax_store.quads_for_pattern(None, safeNamedNode(t), safeNamedNode(p["source_type"]), None):
            node = q.subject
            node_id = str(getattr(node, "value", node))  # NamedNode/BlankNode -> stable string id
            entry = structures.setdefault(node_id, {"sameAs": None, "text": None})

            # sameAs (allow multiple; pick the first non-empty)
            for qq in tax_store.quads_for_pattern(node, safeNamedNode(p["source_sameAs"]), None, None):
                if getattr(qq.object, "value", None):
                    entry["sameAs"] = entry["sameAs"] or str(qq.object.value)

            # text (label candidate)
            for qq in tax_store.quads_for_pattern(node, safeNamedNode(p["source_text"]), None, None):
                if getattr(qq.object, "value", None):
                    # keep the first non-empty
                    entry["text"] = entry["text"] or str(qq.object.value)

    # 2) Build child->parents mapping via inStructure, but resolved through sameAs
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

    # 3) Subjects that only need labels (no parents)
    if update_label:
        for node_id, data in structures.items():
            if data.get("sameAs") and data.get("text"):
                touched_subjects.add(data["sameAs"])

    # 4) Apply updates in 'store'
    for subj_iri in sorted(touched_subjects):
        subj = safeNamedNode(subj_iri)

        if update_broader:
            for b in p["broaders"]:
                # remove all existing broader
                _remove_all(kb_store, subj, safeNamedNode(b)) # TODO DefaultGraph()
                # add each parent from mapping
                for parent_iri in sorted(child_to_parents.get(subj_iri, set())):
                    kb_store.add(Quad(subj, safeNamedNode(b), safeNamedNode(parent_iri), None)) # TODO DefaultGraph()

        if update_label:
            # find a text for this subject (first non-empty)
            for l in p["labels"]:
                text_val: Optional[str] = None
                for node_id, data in structures.items():
                    if data.get("sameAs") == subj_iri and data.get("text"):
                        text_val = data["text"]
                        break
                if text_val is not None:
                    _remove_all(kb_store, subj, safeNamedNode(l)) # TODO DefaultGraph()
                    if label_language:
                        kb_store.add(Quad(subj, safeNamedNode(l), Literal(text_val, language=label_language), None)) # TODO DefaultGraph()
                    else:
                        kb_store.add(Quad(subj, safeNamedNode(l), Literal(text_val), None)) # TODO DefaultGraph()

def pipeline(): # TODO in API
    BASE = ""
    lib_cfg ={
    "api_key" : "",
    "library_id": "",
    "library_type" : "group",
    "note_key":""}
    tax_map = None
    source_store = Store()
    html_in = taxonomy_to_html(source_store, map=tax_map)
    note_key = html_to_note(html_in, **lib_cfg)
    # Editing
    lib_cfg.update("note_key", note_key)
    html_out = note_to_html(**lib_cfg)
    tax_store = html_to_taxonomy(html_out, note_uri=f"{BASE}{note_key}")
    taxononmy_to_store(tax_store=tax_store,kb_store=source_store, map=tax_map)