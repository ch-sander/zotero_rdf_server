
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from .store import Store, Quad, NamedNode, Literal, RdfFormat, DefaultGraph
from rapidfuzz import fuzz
import re
from .logging_config import logger
from .config import *


def iri_to_filename(iri: str) -> str: #TODO move to utils
    parsed = urlparse(iri)
    parts = [parsed.netloc] + parsed.path.strip("/").split("/")
    safe = "_".join(parts)
    return re.sub(r"[^\w\-\.]", "_", safe)

def safeNamedNode(uri: str | NamedNode, enforce: bool = True) -> NamedNode | Literal:
    INTERNAL_IRI_PREFIX = "http://internal.invalid/"
    if isinstance(uri, NamedNode):
        return uri
    if not isinstance(uri, str):
        logger.info(f"Invalid IRI input (not a string), converting to Literal or synthetic IRI: {uri} of type {type(uri)}")
        if enforce:
            fallback = quote(str(uri), safe="")
            return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
        return safeLiteral(uri)

    parsed = urlparse(uri)
    if not parsed.scheme:
        logger.info(f"Invalid IRI input (missing scheme), converting to Literal or synthetic IRI: {uri}")
        if enforce:
            fallback = quote(uri, safe="")
            logger.warning(f"Replaced {uri} with {INTERNAL_IRI_PREFIX}{fallback}")
            return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
        logger.warning(f"Stores {uri} as Literal")
        return safeLiteral(uri)

    try:
        safe_iri = quote(uri, safe=':/#?&=%')
        return NamedNode(safe_iri)
    except ValueError as e:
        logger.info(f"Invalid IRI converted to Literal or synthetic IRI: {uri} – {e}")
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
    
def ensure_rdf_format(format=None, fallback=RdfFormat.TRIG):
    if not format:
        return fallback
    else:
        if not isinstance(format, RdfFormat): 
            return RdfFormat.from_media_type(format) or RdfFormat.from_extension(format) or fallback
        else:
            return format

def ensure_alt_label(store: Store, node: NamedNode, lit_value: str, alt_label_prop: NamedNode = NamedNode(SKOS_ALT), graph: NamedNode = DefaultGraph()):
    existing_labels = {
        q.object.value.lower()
        for q in store.quads_for_pattern(node, alt_label_prop, None, graph)
    }
    if lit_value.lower() not in existing_labels:
        store.add(Quad(node, alt_label_prop, Literal(lit_value), graph))
        logger.debug(f"[ALT] Added altLabel '{lit_value}' to {node}")


def fuzzy_match_label(pool_store:Store, label:str, threshold=90, graph_name:NamedNode = None, predicates:list = [SKOS_ALT], test:bool=False, regex:bool=False):
    best_score = 0
    best_match = None
    best_label = None
    logger.debug(f"Fuzzy matching '{label}' against existing pool of {len(pool_store)} quads (threshold: {threshold})")

    for quad in pool_store: # TODO use candidates!
        subject = quad.subject
        for pred in predicates: # [SKOS_ALT, RDFS_LABEL] Not really needed as every label should also be a altLabel

            for label_quad in pool_store.quads_for_pattern(
                subject, 
                safeNamedNode(pred), 
                None, 
                graph_name=graph_name
                ):
                existing_label = str(label_quad.object.value)
                score = fuzz.ratio(existing_label.lower(), label.lower())
                logger.debug(f"Compared '{label}' with '{existing_label}' → score: {score}")
                if score == 100 and threshold <=100:
                    return subject, 100, existing_label
                if score > best_score:
                    best_score = score
                    best_match = subject
                    best_label = existing_label

            # Regex matching
            if regex and any(c in label for c in ".^$*+?{}[]\\|()"):
                for pattern_quad in pool_store:
                    pattern_str = pattern_quad.object.value
                    try:
                        if pattern_str and re.search(pattern_str, label, re.IGNORECASE):
                            regex_match = subject
                            logger.debug(f"Regex '{pattern_str}' matched '{label}'")
                            return regex_match, 100, pattern_str
                    except re.error as e:
                        logger.warning(f"Invalid regex pattern '{pattern_str}' on {subject}: {e}")
   
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

def add_timestamp(store: Store, node: NamedNode, graph: NamedNode):
    store.add(Quad(node, NamedNode("http://www.w3.org/ns/prov#generatedAtTime"), Literal(datetime.now(timezone.utc).isoformat(),datatype=NamedNode(f"{XSD_NS}dateTime")), graph_name=graph))

def library_href(library_meta: dict):
    return (
        library_meta.get("library", {})
        .get("links", {})
        .get("alternate", {})
        .get("href")
    )