
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from .store import Store, Quad, NamedNode, Literal
from rapidfuzz import fuzz

from .logging_config import logger
from .config import *

def safeNamedNode(uri: str, enforce: bool = True) -> NamedNode | Literal:
    INTERNAL_IRI_PREFIX = "http://internal.invalid/"
    if not isinstance(uri, str):
        logger.info(f"Invalid IRI input (not a string), converting to Literal or synthetic IRI: {uri}")
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

def add_timestamp(store: Store, node: NamedNode, graph: NamedNode):
    store.add(Quad(node, NamedNode("http://www.w3.org/ns/prov#generatedAtTime"), Literal(datetime.now(timezone.utc).isoformat(),datatype=NamedNode(f"{XSD_NS}dateTime")), graph_name=graph))

def library_href(library_meta: dict):
    return (
        library_meta.get("library", {})
        .get("links", {})
        .get("alternate", {})
        .get("href")
    )