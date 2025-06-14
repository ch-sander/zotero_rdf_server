import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional
from pyoxigraph import Store, RdfFormat, NamedNode, BlankNode, DefaultGraph
from zotero_rdf_server.utils import ensure_rdf_format, safeNamedNode
from zotero_rdf_server.logging_config import logger
import time, requests



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