
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, DefaultGraph, BlankNode
from rapidfuzz import fuzz, process
import re, json
from copy import deepcopy
from pathlib import Path
from .logging_config import logger
from .config import *

def iri_to_filename(iri: str) -> str:
    parsed = urlparse(iri)
    parts = [parsed.netloc] + parsed.path.strip("/").split("/")
    safe = "_".join(parts)
    return re.sub(r"[^\w\-\.]", "_", safe)

def make_iri(val: str | list[str], pref: str, enforce_list: bool = False) -> str | list[str]:
    is_str_input = isinstance(val, str)
    vals = [val] if is_str_input else val

    pref = pref.strip()
    result = []

    for v in vals:
        v = v.strip()
        if not v.startswith("http"):
            result.append(f"{pref}{v}")
        else:
            result.append(v)

    if enforce_list:
        return result
    return result[0] if is_str_input else result

def store_remove_all(store: Store, s: NamedNode = None, p: NamedNode = None, o: NamedNode | BlankNode | Literal = None, g: NamedNode | DefaultGraph = None):
    for q in list(store.quads_for_pattern(s, p, o, g)):
        store.remove(q)

def store_move_subject(store: Store, src: NamedNode, dst: NamedNode, g: NamedNode | DefaultGraph = None):
    """
    Re-subject all triples from src to dst: (src, p, o) -> (dst, p, o).
    If multiple identical (dst, p, o) exist, add() is idempotent in PyOxigraph.
    """
    to_move = list(store.quads_for_pattern(src, None, None, g))
    for q in to_move:
        # remove original
        store.remove(q)
        # add with new subject
        store.add(Quad(dst, q.predicate, q.object, g))

def safeNamedNode(uri: str | NamedNode, enforce: bool = True, allow_None: bool = False) -> NamedNode | Literal:
    INTERNAL_IRI_PREFIX = "http://internal.invalid/"
    if uri == None and allow_None: #TODO not tested
        return None
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
        logger.info(f"Invalid IRI input (missing scheme), prepending 'http://': {uri}")
        uri = "http://" + uri
        parsed = urlparse(uri)

    if parsed.scheme and parsed.netloc:
        try:
            safe_iri = quote(uri, safe=':/#?&=%')
            return NamedNode(safe_iri)
        except ValueError as e:
            logger.info(f"Invalid IRI converted to Literal or synthetic IRI: {uri} – {e}")
            if enforce:
                fallback = quote(uri, safe="")
                return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
            return safeLiteral(uri)

    logger.warning(f"IRI still invalid after normalization: {uri}")
    if enforce:
        fallback = quote(uri, safe="")
        logger.warning(f"Replaced {uri} with {INTERNAL_IRI_PREFIX}{fallback}")
        return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
    logger.warning(f"Stores {uri} as Literal")
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
  
def _ensure_dict(obj, label: str) -> dict:
    if isinstance(obj, dict):
        return obj
    raise ValueError(f"{label}: parsed content is not a mapping (got {type(obj).__name__})")

def load_dict_like(
    raw: str | dict | Path | None,
    default: dict | None = None,
    label: str = "config",
    timeout: float = 10.0,
    required: bool = False
) -> dict:
    def _fallback(reason: str) -> dict:
        if required:
            raise
        if default is not None:
            logger.warning(f"{label}: {reason}; using fallback default")
            return deepcopy(dict(default))
        logger.warning(f"{label}: {reason}; using empty mapping")
        return {}

    try:
        if isinstance(raw, dict):
            return deepcopy(dict(raw))

        if raw is None:
            return deepcopy(dict(default)) if default is not None else {}

        if isinstance(raw, Path):
            path = raw.resolve()
            if not path.exists():
                return _fallback(f"file not found: {path}")
            content = path.read_text(encoding="utf-8")
            suffix = path.suffix.lower()

        elif isinstance(raw, str):
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https"):
                try:
                    resp = requests.get(raw, timeout=timeout)
                    resp.raise_for_status()
                except requests.RequestException as e:
                    return _fallback(f"failed to fetch URL {raw}: {e}")
                content = resp.text
                suffix = Path(parsed.path).suffix.lower()
                logger.info(f"{label}: loaded from URL {raw}")
            else:
                path = Path(raw).expanduser().resolve()
                if path.exists():
                    content = path.read_text(encoding="utf-8")
                    suffix = path.suffix.lower()
                    logger.info(f"{label}: loaded from file {path}")
                else:
                    try:
                        data = json.loads(raw)
                        logger.info(f"{label}: loaded from JSON string")
                        return _ensure_dict(data, label)
                    except json.JSONDecodeError:
                        try:
                            data = yaml.safe_load(raw)
                            logger.info(f"{label}: loaded from YAML string")
                            return _ensure_dict(data, label)
                        except yaml.YAMLError as e:
                            return _fallback(f"string is neither valid JSON nor YAML: {e}")

        if suffix in (".yaml", ".yml"):
            data = yaml.safe_load(content)
            logger.info(f"{label}: parsed YAML")
            return _ensure_dict(data, label)
        if suffix == ".json" or not suffix:
            try:
                data = json.loads(content)
                logger.info(f"{label}: parsed JSON")
                return _ensure_dict(data, label)
            except json.JSONDecodeError:
                try:
                    data = yaml.safe_load(content)
                    logger.info(f"{label}: parsed YAML (no/unknown suffix)")
                    return _ensure_dict(data, label)
                except yaml.YAMLError as e:
                    return _fallback(f"failed to parse content as JSON/YAML: {e}")

        try:
            data = json.loads(content)
            logger.info(f"{label}: parsed JSON despite suffix {suffix}")
            return _ensure_dict(data, label)
        except json.JSONDecodeError:
            try:
                data = yaml.safe_load(content)
                logger.info(f"{label}: parsed YAML despite suffix {suffix}")
                return _ensure_dict(data, label)
            except yaml.YAMLError:
                return _fallback(f"unsupported file type: {suffix}")

    except Exception as _:
        return _fallback("unexpected error during load")
        
def ensure_alt_label(store: Store, node: NamedNode, lit_value: str, alt_label_prop: NamedNode = NamedNode(SKOS_ALT), graph: NamedNode = DefaultGraph()):
    existing_labels = {
        q.object.value.lower()
        for q in store.quads_for_pattern(node, alt_label_prop, None, graph)
    }
    if lit_value.lower() not in existing_labels:
        store.add(Quad(node, alt_label_prop, Literal(lit_value), graph))
        logger.debug(f"[ALT] Added altLabel '{lit_value}' to {node}")

def quads_by_type(store:Store,type_nodes:list, graph:NamedNode):
    result_store = Store()
    for t in type_nodes:
        for quad in store.quads_for_pattern(
            None,
            NamedNode(RDF_TYPE),
            safeNamedNode(t),
            graph
        ):
            result_store.bulk_extend(store.quads_for_pattern(quad.subject, None, None, graph))
    return result_store

def fuzzy_match_label(pool_store:Store, label:str, threshold=90, graph_name:NamedNode = None, predicates:list = [SKOS_ALT], regex:bool=False, max_matches:int = 1):

    logger.debug(
        f"Fuzzy matching '{label}' against existing pool of {len(pool_store)} quads "
        f"(threshold: {threshold}, max_matches={max_matches})"
    )

    label_map = {}  # label:str -> list of subjects

    for pred in predicates:
        for label_quad in pool_store.quads_for_pattern(
                None,
                safeNamedNode(pred),
                None,
                graph_name=graph_name
            ):
            lbl = str(label_quad.object.value)
            label_map.setdefault(lbl, []).append(label_quad.subject)

    def lower_processor(label:str):
        return label.lower()
    
    # Fuzzy Matching
    if label_map:
        if max_matches > 1:
            results = process.extract(
                label,
                label_map.keys(),
                scorer=fuzz.ratio,
                processor=lower_processor,
                score_cutoff=threshold,
                limit=max_matches
            )
            matches = []
            for best_match_label, score, _ in results:
                for subj in label_map[best_match_label]:
                    matches.append((subj, score, best_match_label))

            if matches:
                logger.debug(
                    f"Fuzzy matches for '{label}': {[(m[1], m[2]) for m in matches]}"
                )
                return matches
        else:
            result = process.extractOne(
                label,
                label_map.keys(),
                processor=lower_processor,
                scorer=fuzz.ratio,
                score_cutoff=threshold
            )
            if result:
                best_match_label, score, _ = result
                subjects = label_map[best_match_label]
                best_subject = subjects[0]
                logger.debug(
                    f"Best fuzzy match for '{label}' → '{best_match_label}' (score={score})"
                )
                if score == 100 and threshold <= 100:
                    return best_subject, 100, best_match_label
                return best_subject, score, best_match_label


    # Regex matching
    if regex and any(c in label for c in ".^$*+?{}[]\\|()"):
        regex_matches = []
        for pattern_quad in pool_store:
            pattern_str = pattern_quad.object.value
            subject = pattern_quad.subject
            try:
                if pattern_str and re.search(pattern_str, label, re.IGNORECASE):
                    logger.debug(f"Regex '{pattern_str}' matched '{label}'")
                    regex_matches.append((subject, 100, pattern_str))
            except re.error as e:
                logger.warning(
                    f"Invalid regex pattern '{pattern_str}' on {subject}: {e}"
                )
        if regex_matches:
            return regex_matches[:max_matches] if max_matches > 1 else regex_matches[0]
        
    # if regex and any(c in label for c in ".^$*+?{}[]\\|()"):
    #     for pattern_quad in pool_store:
    #         pattern_str = pattern_quad.object.value
    #         try:
    #             if pattern_str and re.search(pattern_str, label, re.IGNORECASE):
    #                 regex_match = subject
    #                 logger.debug(f"Regex '{pattern_str}' matched '{label}'")
    #                 return regex_match, 100, pattern_str
    #         except re.error as e:
    #             logger.warning(f"Invalid regex pattern '{pattern_str}' on {subject}: {e}")
   
    # if best_score >= threshold: # TODO return dict for all matches above threshold in descending order to match source on multiple KB items
    #     logger.debug(f"Best match: {best_match} with label '{best_label}' (score: {best_score})")
    #     return best_match, best_score, best_label
    # else:
    #     logger.debug("No fuzzy match found above threshold.")
    #     return None, 0, None

    logger.debug("No fuzzy match found above threshold.")
    return [] if max_matches > 1 else (None, 0, None)

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
    store.add(Quad(node, NamedNode(PROV_TIMESTAMP), Literal(datetime.now(timezone.utc).isoformat(),datatype=NamedNode(f"{XSD_NS}dateTime")), graph_name=graph))

def library_href(library_meta: dict):
    return (
        library_meta.get("library", {})
        .get("links", {})
        .get("alternate", {})
        .get("href")
    )

