
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
import urllib.request
from pyoxigraph import Store, Quad, NamedNode, Literal, RdfFormat, DefaultGraph, BlankNode
from rapidfuzz import fuzz, process
import re, json, requests, yaml, unicodedata, subprocess, importlib, sys, hashlib, tempfile
from copy import deepcopy
from pathlib import Path
from .logging_config import logger
# from .config import *
from uuid import uuid4, uuid5, UUID
from typing import Any, Iterator

from .config import MAP_TYPE_HINT, MAP_ENTRY_TYPE, RDF_TYPE, LANG_MAP, PROV_TIMESTAMP, XSD_NS, MAP_LABEL, MAP_TARGET, MAP_REGEX, RDFS_LABEL, APP_USER


CT_TO_EXT = {
    "application/ld+json": "jsonld",
    "application/json": "json",
    "application/n-triples": "nt",
    "application/n-quads": "nq",
    "text/turtle": "ttl",
    "application/rdf+xml": "rdf",
    "application/xml": "rdf",
}
URL_SCHEMES = {"http", "https"}

def canonicalize_label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.casefold().split())


def canonicalize_types(my_types) -> tuple[str, ...]:
    return tuple(sorted(str(type_) for type_ in my_types))



def stable_entity_uuid(item: str, my_types, ENTITY_UUID:UUID) -> str:
    identity = {
        "version": 1,
        "label": canonicalize_label(item),
        "types": canonicalize_types(my_types),
    }

    canonical_identity = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return str(uuid5(ENTITY_UUID, canonical_identity))


def is_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in URL_SCHEMES and bool(u.netloc)
    except Exception:
        return False

def guess_ext_from_headers(headers: dict, fallback_url: str) -> str:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            name = m.group(1)
            ext = Path(name).suffix.lstrip(".").lower()
            if ext:
                return ext

    ct = headers.get("Content-Type") or headers.get("content-type") or ""
    ct = ct.split(";")[0].strip().lower()
    if ct in CT_TO_EXT:
        return CT_TO_EXT[ct]

    ext = Path(urlparse(fallback_url).path).suffix.lstrip(".").lower()
    return ext

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


def normalize_iri_scheme(iri: str) -> str:
    from .config import ZOT_BASE_URI
    BASE = urlparse(ZOT_BASE_URI)
    iri = iri.strip()
    logger.debug(f"HTTP --> HTTPS for {BASE} in {iri}")
    try:
        parsed = urlparse(iri)

        if parsed.netloc.replace("www.", "") == BASE.netloc.replace("www.", ""):
            parsed = parsed._replace(scheme=BASE.scheme)
            return parsed.geturl()

    except Exception:
        pass

    return iri

def safeNamedNode(uri: str | NamedNode, enforce: bool = True, allow_None: bool = False) -> NamedNode | Literal:

    if not isinstance(uri, (str, NamedNode)):
        raise TypeError(f"invalid type {type(uri)} for {uri}")
    
    INTERNAL_IRI_PREFIX = "http://internal.invalid/"
    if uri == None and allow_None: # TODO not tested
        return None
    if isinstance(uri, NamedNode):
        return uri
    if not isinstance(uri, str):
        logger.info(f"Invalid IRI input (not a string), converting to Literal or synthetic IRI: {uri} of type {type(uri)}")
        if enforce:
            fallback = quote(str(uri), safe="")
            return NamedNode(f"{INTERNAL_IRI_PREFIX}{fallback}")
        return safeLiteral(uri)
    uri = uri.strip("<>")
    parsed = urlparse(uri)
    if not parsed.scheme:

        # import inspect # TODO DEBUG
        # caller = inspect.stack()[1]
        # filename = caller.filename
        # lineno = caller.lineno
        # funcname = caller.function
        # logger.warning(f"Called from {filename}:{lineno} in {funcname}")

        logger.warning(f"Invalid IRI input (missing scheme), prepending 'http://': {uri}")
        uri = "http://" + uri
        parsed = urlparse(uri)

    if parsed.scheme and parsed.netloc:
        try:
            safe_iri = quote(uri, safe=':/#?&=%')
            return NamedNode(safe_iri)
        except ValueError as e:
            logger.warning(f"Invalid IRI converted to Literal or synthetic IRI: {uri} – {e}")
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

def _ensure_mapping_or_list(data, label: str) -> dict | list:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            pass

    if isinstance(data, dict):
        return data

    if isinstance(data, list):
        return data

    raise ValueError(f"{label}: expected dict or list")

def _parse_csv_to_dict(content: str, label: str) -> dict:
    import csv
    from io import StringIO
    try:
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        return {"rows": rows}
    except Exception as e:
        raise ValueError(f"{label}: failed to parse CSV: {e}")
    
def _sources_from_sparql(
    store: Store,
    query: str,
    *,
    url_variable: str = "url",
    document_uri_variable: str = "document_uri",
) -> Iterator[tuple[str, str]]:
    logger.info("Using citation source query:\n%s", query)

    bindings = store.query(
        query,
        use_default_graph_as_union=True,
    )

    count = 0

    for row in bindings:
        url_value = row[url_variable]
        document_uri_value = row[document_uri_variable]

        if url_value is None:
            logger.warning(
                "SPARQL result does not contain binding ?%s",
                url_variable,
            )
            continue

        if document_uri_value is None:
            logger.warning(
                "SPARQL result does not contain binding ?%s",
                document_uri_variable,
            )
            continue

        if not isinstance(url_value, (NamedNode, Literal)):
            logger.warning(
                "Ignoring unsupported SPARQL value for ?%s: %r",
                url_variable,
                url_value,
            )
            continue

        if not isinstance(document_uri_value, (NamedNode, Literal)):
            logger.warning(
                "Ignoring unsupported SPARQL value for ?%s: %r",
                document_uri_variable,
                document_uri_value,
            )
            continue

        count += 1
        yield url_value.value, document_uri_value.value

    logger.info("Number of citation sources: %s", count)
    
def _index_url(
    url: str,
    *,
    index_document,
    document_uri: str,
    graph_uri: str | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Download and index a document URL."""
    with tempfile.TemporaryDirectory(
        prefix="zotero-citation-download-",
    ) as temp_dir:
        path = Path(temp_dir) / "document"

        logger.info("Downloading citation source %s", url)

        urllib.request.urlretrieve(
            url,
            path,
        )

        return index_document(
            path,
            document_uri=document_uri,
            graph_uri=graph_uri,
            context=context,
        )
    

def load_dict_like(
    raw: str | dict | list | Path | None,
    default: dict | list[dict] | None = None,
    label: str = "config",
    timeout: float = 10.0,
    required: bool = False,
    verbose:bool = False
) -> dict | list[dict]:

    def _return(data: dict | list[dict]) -> dict | list[dict]:
        if verbose:
            logger.info(f"Finished: {label}")
            logger.info(json.dumps(data,indent=4))
        else:
            logger.debug(json.dumps(data,indent=4))
        return data
    
    def _fallback(reason: str) -> dict | list[dict]:
        logger.info(f"got raw to load: {raw}")
        if required:
            raise ValueError(f"{label}: {reason}")
        if default is not None:
            logger.warning(f"{label}: {reason}; using fallback default")
            return _return(deepcopy(default))
        logger.warning(f"{label}: {reason}; using empty mapping")        
        return _return({})

    try:
        if isinstance(raw, (dict, list)):
            return _return(deepcopy(_ensure_mapping_or_list(raw, label)))

        if isinstance(raw, str):
            try:
                data = json.loads(raw)
                return _return(_ensure_mapping_or_list(data, label))
            except Exception:
                pass

        if raw is None:
            return _return(deepcopy(default) if default is not None else {})

        if isinstance(raw, Path):
            path = raw.resolve()
            if not path.exists():
                return _return(_fallback(f"file not found: {path}"))
            content = path.read_text(encoding="utf-8")
            suffix = path.suffix.lower()

        elif isinstance(raw, str):
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https"):
                try:
                    resp = requests.get(raw, timeout=timeout, headers=APP_USER)
                    resp.raise_for_status()
                except requests.RequestException as e:
                    return _return(_fallback(f"failed to fetch URL {raw}: {e}"))
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
                        return _return(_ensure_mapping_or_list(data, label))
                    except json.JSONDecodeError:
                        try:
                            data = yaml.safe_load(raw)
                            logger.info(f"{label}: loaded from YAML string")
                            return _return(_ensure_mapping_or_list(data, label))
                        except yaml.YAMLError as e:
                            if "," in raw.splitlines()[0]:
                                try:
                                    data = _parse_csv_to_dict(raw, label)
                                    logger.info(f"{label}: parsed CSV (sniffed)")
                                    return _return(data)
                                except Exception:
                                    pass
                            return _fallback(f"string is not valid: {e}")

        if suffix in (".yaml", ".yml"):
            data = yaml.safe_load(content)
            logger.info(f"{label}: parsed YAML")
            return _return(_ensure_mapping_or_list(data, label))
        if suffix == ".json" or not suffix:
            try:
                data = json.loads(content)
                logger.info(f"{label}: parsed JSON")
                return _return(_ensure_mapping_or_list(data, label))
            except json.JSONDecodeError:
                try:
                    data = yaml.safe_load(content)
                    logger.info(f"{label}: parsed YAML (no/unknown suffix)")
                    return _return(_ensure_mapping_or_list(data, label))
                except yaml.YAMLError as e:
                    return _return(_fallback(f"failed to parse content as JSON/YAML: {e}"))                
        if suffix == ".csv":
            try:
                data = _parse_csv_to_dict(content, label)
                logger.info(f"{label}: parsed CSV")
                return _return(data)
            except Exception as e:
                return _return(_fallback(str(e)))
            
        try:
            data = json.loads(content)
            logger.info(f"{label}: parsed JSON despite suffix {suffix}")
            return _return(_ensure_mapping_or_list(data, label))
        except json.JSONDecodeError:
            try:
                data = yaml.safe_load(content)
                logger.info(f"{label}: parsed YAML despite suffix {suffix}")
                return _return(_ensure_mapping_or_list(data, label))
            except yaml.YAMLError:                
                if "," in content.splitlines()[0]:
                    try:
                        data = _parse_csv_to_dict(content, label)
                        logger.info(f"{label}: parsed CSV (sniffed)")
                        return _return(data)
                    except Exception:
                        pass
                return _return(_fallback("failed to parse content"))
            
    except Exception as _:
        return _return(_fallback("unexpected error"))
    
def default_filename(prefix: str, ext: str) -> str:
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}.{ext}"

def html_to_string(text: str) -> str:
    if not text:
        return ""

    try:
        from lxml import html
        return html.fromstring(text).text_content().strip()
    except ImportError:
        pass
    except Exception:
        return ""

    try:
        from html.parser import HTMLParser
        from html import unescape

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.t = []

            def handle_data(self, d):
                self.t.append(d)

        p = P()
        p.feed(text)
        p.close()
        return unescape(''.join(p.t)).strip()
    except Exception:
        return ""

def load_text_like(
    raw: str | Path | None,
    default: str | None = None,
    label: str | None = "text",
    timeout: float = 10.0,
    required: bool = False,
) -> str:
    def _fallback(reason: str) -> str:
        if required:
            raise
        if default is not None:
            logger.warning(f"{label}: {reason}; using fallback default")
            return str(default)
        logger.warning(f"{label}: {reason}; using empty string")
        return ""

    try:
        if raw is None:
            return str(default) if default is not None else ""

        if isinstance(raw, Path):
            path = raw.expanduser().resolve()
            if not path.exists():
                return _fallback(f"file not found: {path}")
            if label is not None:
                logger.info(f"{label}: loaded from file {path}")
            return path.read_text(encoding="utf-8")

        if isinstance(raw, str):
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https"):
                try:
                    resp = requests.get(raw, timeout=timeout, headers=APP_USER)
                    resp.raise_for_status()
                    if label is not None:
                        logger.info(f"{label}: loaded from URL {raw}")
                    return resp.text
                except requests.RequestException as e:
                    return _fallback(f"failed to fetch URL {raw}: {e}")

            path = Path(raw).expanduser().resolve()
            if path.exists():
                if label is not None:
                    logger.info(f"{label}: loaded from file {path}")
                return path.read_text(encoding="utf-8")

            # plain string
            if label is not None:
                logger.info(f"{label}: using raw string")
            return raw

        return _fallback(f"unsupported input type: {type(raw)}")

    except Exception:
        if required:
            raise
        return _fallback("unexpected error")


        
def ensure_mapping_literal(
    store: Store,
    subject: NamedNode,
    lit_value: str,
    prop: NamedNode = NamedNode(MAP_LABEL),
    graph: NamedNode = DefaultGraph()
):
    existing = {
        q.object.value.lower()
        for q in store.quads_for_pattern(subject, prop, None, graph)
        if q.object and isinstance(q.object, Literal)
    }
    if lit_value.lower() not in existing:
        store.add(Quad(subject, prop, Literal(lit_value), graph))
        store.add(Quad(
            subject,
            NamedNode("http://purl.org/dc/terms/modified"),
            Literal(
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                datatype=NamedNode(f"{XSD_NS}dateTime"),
            ),
            graph,
        ))
        logger.debug(f"[MAP] Added {prop.value} '{lit_value}' to {subject}")
    # add rdfs
    if not any(store.quads_for_pattern(subject, NamedNode(RDFS_LABEL), None, graph)):
        store.add(Quad(subject, NamedNode(RDFS_LABEL), Literal(lit_value), graph))
        logger.debug(f"[MAP] Added '{lit_value}' as rdfs:label to {subject}")

def quads_by_type(store:Store,type_nodes:list, graph:NamedNode, type:NamedNode = NamedNode(RDF_TYPE)):
    result_store = Store()
    for t in type_nodes:
        for quad in store.quads_for_pattern(
            None,
            type,
            safeNamedNode(t),
            graph
        ):
            result_store.bulk_extend(store.quads_for_pattern(quad.subject, None, None, graph))
    return result_store

def fuzzy_match_label_deprecated(
    pool_store: Store,
    label: str,
    threshold=90,
    graph_name: NamedNode = None,
    predicates: list = [MAP_LABEL],
    regex: bool = False,
    max_matches: int = 1
): # TODO include typeHint? Done in pooling upstream
    logger.debug(
        f"Fuzzy matching '{label}' against existing pool of {len(pool_store)} quads "
        f"(threshold: {threshold}, max_matches={max_matches}, graph: {graph_name})"
    )

    label_map = {}  # lbl -> list of entry-subjects

    for pred in predicates:
        for q in pool_store.quads_for_pattern(
            None, safeNamedNode(pred), None, graph_name=graph_name
        ):
            lbl = str(q.object.value)
            label_map.setdefault(lbl, []).append(q.subject)  # subject = entry

    def lower_processor(x: str):
        return x.lower()

    # helper: entry -> target entity
    def entry_to_target(entry: NamedNode):
        for tq in pool_store.quads_for_pattern(
            entry, safeNamedNode(MAP_TARGET), None, graph_name=graph_name
        ):
            return tq.object
        return None

    # --- Fuzzy Matching ---
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
                for entry in label_map[best_match_label]:
                    target = entry_to_target(entry)
                    if target:
                        matches.append((target, score, best_match_label))
                    else:
                        logger.warning(f"Found mapping for {label}, but no target in {str(entry)}")
            if matches:
                logger.debug(f"Fuzzy matches for '{label}': {[(m[1], m[2]) for m in matches]}")
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
                entry = label_map[best_match_label][0]
                target = entry_to_target(entry)
                if target:
                    logger.debug(
                        f"Best fuzzy match for '{label}' → '{best_match_label}' (score={score}): {target}"
                    )
                    if score == 100 and threshold <= 100:
                        return target, 100, best_match_label
                    return target, score, best_match_label
                else:
                    logger.warning(f"Found mapping for {label}, but no target in {str(entry)}")

    # --- Regex matching über map:pattern ---
    if regex and any(c in label for c in ".^$*+?{}[]\\|()"):
        regex_matches = []
        for pq in pool_store.quads_for_pattern(None, safeNamedNode(MAP_REGEX), None, graph_name=graph_name):
            pattern_str = pq.object.value
            entry = pq.subject
            try:
                if pattern_str and re.search(pattern_str, label, re.IGNORECASE):
                    target = entry_to_target(entry)
                    if target:
                        logger.debug(f"Regex '{pattern_str}' matched '{label}'")
                        regex_matches.append((target, 100, pattern_str))
                    else:
                        logger.warning(f"Found mapping for {label}, but no target in {str(entry)}")
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern_str}' on {entry}: {e}")

        if regex_matches:
            return regex_matches[:max_matches] if max_matches > 1 else regex_matches[0]

    logger.debug("No fuzzy match found above threshold.")
    return [] if max_matches > 1 else (None, 0, None)

from typing import Optional

def fuzzy_match_label(
    pool_store: Store,
    label: str,
    threshold: int = 90,
    graph_name: Optional[NamedNode] = None,
    predicates: Optional[list] = None,
    regex: bool = False,
    max_matches: int = 1,
):
    """
    Match a label against mapping labels and optionally mapping regex patterns.

    Single-match return value:
        (target, score, matched_label, entry)

    Multi-match return value:
        [
            (target, score, matched_label, entry),
            ...
        ]

    A target may be None when a matching mapping entry exists but has no
    MAP_TARGET relation yet.

    No-match return value:
        Single match: (None, 0, None, None)
        Multi match:  []
    """
    if max_matches < 1:
        raise ValueError("max_matches must be at least 1")

    if predicates is None:
        predicates = [MAP_LABEL]

    logger.debug(
        f"Matching '{label}' against a pool of {len(pool_store)} quads "
        f"(threshold={threshold}, max_matches={max_matches}, "
        f"graph={graph_name}, regex={regex})"
    )

    label_map = {}
    target_map = {}

    # Build the label-to-entry index.
    for predicate in predicates:
        predicate_node = safeNamedNode(predicate)

        for quad in pool_store.quads_for_pattern(
            None,
            predicate_node,
            None,
            graph_name=graph_name,
        ):
            mapped_label = str(quad.object.value)
            label_map.setdefault(mapped_label, []).append(quad.subject)

    # Build the entry-to-target index once to avoid repeated store scans.
    for quad in pool_store.quads_for_pattern(
        None,
        safeNamedNode(MAP_TARGET),
        None,
        graph_name=graph_name,
    ):
        target_map.setdefault(quad.subject, quad.object)

    def normalize(value: str) -> str:
        return value.casefold()

    def make_match(
        entry: NamedNode,
        score: int,
        matched_value: str,
    ):
        target = target_map.get(entry)

        if target is None:
            logger.debug(
                f"Mapping entry {entry} matched '{label}' as "
                f"'{matched_value}', but has no target"
            )

        return target, score, matched_value, entry

    def order_entries(entries):
        # Prefer complete mappings over entries without a target.
        return sorted(
            entries,
            key=lambda entry: target_map.get(entry) is None,
        )

    # Try ordinary fuzzy matching first.
    if label_map:
        if max_matches == 1:
            result = process.extractOne(
                label,
                label_map.keys(),
                processor=normalize,
                scorer=fuzz.ratio,
                score_cutoff=threshold,
            )

            if result:
                matched_label, score, _ = result
                entries = order_entries(label_map[matched_label])
                entry = entries[0]

                match = make_match(
                    entry=entry,
                    score=score,
                    matched_value=matched_label,
                )

                logger.debug(
                    f"Best fuzzy match for '{label}': "
                    f"'{matched_label}' with score {score}"
                )

                return match

        else:
            # Request enough labels to fill the result after entries are expanded.
            results = process.extract(
                label,
                label_map.keys(),
                processor=normalize,
                scorer=fuzz.ratio,
                score_cutoff=threshold,
                limit=None,
            )

            matches = []

            for matched_label, score, _ in results:
                for entry in order_entries(label_map[matched_label]):
                    matches.append(
                        make_match(
                            entry=entry,
                            score=score,
                            matched_value=matched_label,
                        )
                    )

                    if len(matches) >= max_matches:
                        logger.debug(
                            f"Fuzzy matches for '{label}': "
                            f"{[(match[1], match[2]) for match in matches]}"
                        )
                        return matches

            if matches:
                logger.debug(
                    f"Fuzzy matches for '{label}': "
                    f"{[(match[1], match[2]) for match in matches]}"
                )
                return matches

    # Try mapping regex patterns when no fuzzy result was returned.
    if regex:
        regex_matches = []

        for quad in pool_store.quads_for_pattern(
            None,
            safeNamedNode(MAP_REGEX),
            None,
            graph_name=graph_name,
        ):
            pattern = str(quad.object.value)
            entry = quad.subject

            if not pattern:
                continue

            try:
                if re.search(pattern, label, flags=re.IGNORECASE):
                    match = make_match(
                        entry=entry,
                        score=100,
                        matched_value=pattern,
                    )

                    logger.debug(
                        f"Regex pattern '{pattern}' matched '{label}'"
                    )

                    if max_matches == 1:
                        return match

                    regex_matches.append(match)

                    if len(regex_matches) >= max_matches:
                        return regex_matches

            except re.error as exc:
                logger.warning(
                    f"Invalid regex pattern '{pattern}' "
                    f"on mapping entry {entry}: {exc}"
                )

        if regex_matches:
            return regex_matches

    logger.debug(
        f"No match found for '{label}' above threshold {threshold}"
    )

    if max_matches > 1:
        return []

    return None, 0, None, None

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

def add_timestamp(store: Store, node: NamedNode, graph: NamedNode, predicate:NamedNode=NamedNode(PROV_TIMESTAMP)):
    store.add(Quad(node, predicate, Literal(datetime.now(timezone.utc).isoformat(),datatype=NamedNode(f"{XSD_NS}dateTime")), graph_name=graph))

def dedupe_zotero_objects(
    items: list[dict],
    collection_keys: list[str] | None = None,
) -> list[dict]:
    if not collection_keys:
        return items

    allowed = set(collection_keys)
    by_key: dict[str, dict] = {}

    for item in items:
        if not isinstance(item, dict):
            continue

        key = item.get("key") or item.get("data", {}).get("key")
        if not key:
            continue

        data = item.setdefault("data", {})
        item_collections = set(data.get("collections") or [])

        filtered_collections = item_collections & allowed

        if key not in by_key:
            copied_item = dict(item)
            copied_data = dict(data)
            copied_data["collections"] = sorted(filtered_collections)
            copied_item["data"] = copied_data
            by_key[key] = copied_item
        else:
            existing_data = by_key[key].setdefault("data", {})
            existing_collections = set(existing_data.get("collections") or [])
            existing_data["collections"] = sorted(
                existing_collections | filtered_collections
            )

    return list(by_key.values())

def library_href(library_meta: dict):
    return (
        library_meta
        .get("links", {})
        .get("alternate", {})
        .get("href")
    )

def merge_with_prefix(base: dict, extra: dict, prefix: str) -> dict:
    return {
        **base,
        **{f"{prefix}{key}": value for key, value in extra.items()},
    }

def ucfirst(value: object) -> str:
    s = str(value)
    return s[:1].upper() + s[1:] if s else s

def ensure_import(module, attr=None, requirements=None):
    modname = re.split(r"(?:==|!=|<=|>=|<|>|~=)", module, 1)[0]

    try:
        mod = importlib.import_module(modname)

    except ImportError:
        try:
            logger.warning(
                f"{modname} not found. Installing dependencies ({module})..."
            )

            if requirements:
                subprocess.check_call([
                    sys.executable, "-m", "pip",
                    "install", "-r", str(requirements),
                ])
            else:
                subprocess.check_call([
                    sys.executable, "-m", "pip",
                    "install", module,
                ])
            try:
                mod = importlib.import_module(modname)
            except ImportError as e:
                logger.error(e)
                return

        except Exception as e:
            logger.error(e, exc_info=True)
            raise

    return getattr(mod, attr) if attr else mod


def require_symbol(module_name: str, symbol: str, *, hint:str = None):
    if importlib.util.find_spec(module_name) is None:
        msg = f"Required module not found: {module_name}"
        if hint:
            msg += f" ({hint})"
        logger.error(msg)
        raise ModuleNotFoundError(msg)

    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        msg = f"Module exists but failed to import: {module_name}"
        if hint:
            msg += f" ({hint})"
        logger.error(msg)
        raise ImportError(msg) from e

    try:
        return getattr(mod, symbol)
    except AttributeError as e:
        msg = f"Module '{module_name}' does not provide required symbol '{symbol}'"
        if hint:
            msg += f" ({hint})"
        logger.error(msg)
        raise AttributeError(msg) from e


MAP_TARGET_NODE = safeNamedNode(MAP_TARGET)
MAP_ENTRY_TYPE_NODE = safeNamedNode(MAP_ENTRY_TYPE)
MAP_TYPE_HINT_NODE = safeNamedNode(MAP_TYPE_HINT)
MAP_LABEL_NODE = safeNamedNode(MAP_LABEL)
RDF_TYPE_NODE = NamedNode(RDF_TYPE)
RDFS_LABEL_NODE = NamedNode(RDFS_LABEL)

def find_entries_for_type_hints(
    store: Store,
    type_hints,
    graph_name: NamedNode,
):
    entries = set()

    for type_hint in type_hints:
        for quad in store.quads_for_pattern(
            None,
            safeNamedNode(MAP_TYPE_HINT),
            safeNamedNode(type_hint),
            graph_name,
        ):
            entries.add(quad.subject)

    return entries

def find_entries_for_target(
    store: Store,
    target: NamedNode,
    map_graph: NamedNode
) -> set[NamedNode]:

    entries: set[NamedNode] = set()

    for q in store.quads_for_pattern(
        None,
        MAP_TARGET_NODE,
        target,
        graph_name=map_graph
    ):
        if isinstance(q.subject, NamedNode):
            entries.add(q.subject)

    return entries

def select_entity_types(
    rule_types: list,
    mapping_types: list,
    type_source: str = "mapping_or_rule",
) -> list:
    rule_types = list(rule_types or [])
    mapping_types = list(mapping_types or [])

    if type_source == "mapping":
        return mapping_types

    if type_source == "rule":
        return rule_types

    if type_source == "mapping_or_rule":
        return mapping_types or rule_types
    
    if type_source == "rule_or_mapping":
        return rule_types or mapping_types
    
    if type_source == "merge":
        result = []
        seen = set()

        for rdf_type in [*mapping_types, *rule_types]:
            key = getattr(rdf_type, "value", str(rdf_type))

            if key not in seen:
                seen.add(key)
                result.append(rdf_type)

        return result

    raise ValueError(
        f"Unsupported type_source: {type_source!r}"
    )

def ensure_entry(
    store: Store,
    target: NamedNode,
    map_graph: NamedNode,
    type_hints: list[str] | None = None,
    entry_spec: dict | None = None,
) -> NamedNode:

    entries = find_entries_for_target(store, target, map_graph)

    if entries:
        keeper = next(iter(entries))

        # optional: merge duplicates into keeper
        for e in list(entries):
            if e == keeper:
                continue
            migrate_facts(store, e, keeper, map_graph)
            delete_subject_facts(store, e, map_graph)

        if type_hints:
            for th in type_hints:
                store.add(Quad(keeper, MAP_TYPE_HINT_NODE, safeNamedNode(th), map_graph))
        return keeper

    # create new entry
    entry = safeNamedNode(f"{str(map_graph.value).strip('/')}/{uuid4()}")

    store.add(Quad(entry, NamedNode(RDF_TYPE), safeNamedNode(MAP_ENTRY_TYPE), map_graph))
    store.add(Quad(entry, MAP_TARGET_NODE, target, map_graph))

    if type_hints:
        for th in type_hints:
            store.add(Quad(entry, MAP_TYPE_HINT_NODE, safeNamedNode(th), map_graph))

    if entry_spec: # TODO
        from .rdf import load_rdf_from_spec
        load_rdf_from_spec(
            entry_spec,
            context=None,
            data={                
                "target": target.value,
                "types": type_hints or [],
            },
            node_value=entry.value,
            store=store,
            default_graph_uri=map_graph,
        )
    else:
        add_timestamp(store=store, node=entry, graph=map_graph)
    return entry

def iter_entities(store: Store, entity_graph: NamedNode):
    seen = set()
    for q in store.quads_for_pattern(None, None, None, graph_name=entity_graph):
        if isinstance(q.subject, NamedNode) and q.subject not in seen:
            seen.add(q.subject)
            yield q.subject

def is_mapping_target(store: Store, node: NamedNode, map_graph: NamedNode) -> bool:
    for _ in store.quads_for_pattern(None, MAP_TARGET_NODE, node, graph_name=map_graph):
        return True
    return False

def is_object_somewhere(
    store: Store,
    node: NamedNode,
    graphs: list[NamedNode] | None = None,
    ignore_predicates: set[NamedNode] | None = None,
) -> bool:

    ignore_predicates = ignore_predicates or set()

    if graphs is None:
        for q in store.quads_for_pattern(None, None, node, graph_name=None):
            if q.predicate not in ignore_predicates:
                return True
        return False

    for g in graphs:
        for q in store.quads_for_pattern(None, None, node, graph_name=g):
            if q.predicate not in ignore_predicates:
                return True
    return False

def delete_subject_facts(store: Store, node: NamedNode, graph: NamedNode):
    for q in store.quads_for_pattern(node, None, None, graph_name=graph):
        store.remove(q)

def replace_object_everywhere(store: Store, old: NamedNode, new: NamedNode, graph: NamedNode | None = None):
    for q in list(store.quads_for_pattern(None, None, old, graph_name=graph)):
        store.remove(q)
        store.add(Quad(q.subject, q.predicate, new, q.graph_name))

def migrate_facts(store: Store, old: NamedNode, new: NamedNode, graph: NamedNode, ignore_predicates: set[NamedNode] | None = None):
    ignore_predicates = ignore_predicates or {NamedNode(PROV_TIMESTAMP), NamedNode(RDFS_LABEL)}
    for q in list(store.quads_for_pattern(old, None, None, graph_name=graph)):
        if q.predicate not in ignore_predicates:
            store.add(Quad(new, q.predicate, q.object, graph))

def retarget_mapping_entries(
    store: Store,
    old: NamedNode,
    new: NamedNode,
    map_graph: NamedNode,
    *,
    dedup: bool = False,
    ignore_predicates_in_merge: set[NamedNode] | None = None,
):
    # 1) retarget all old -> new
    to_retarget = list(store.quads_for_pattern(None, MAP_TARGET_NODE, old, graph_name=map_graph))
    for q in to_retarget:
        store.remove(q)
        store.add(Quad(q.subject, q.predicate, new, map_graph))

    if not dedup:
        return

    # 2) dedup entries for new
    entries = list(find_entries_for_target(store, new, map_graph))  # set -> list
    if len(entries) <= 1:
        return

    keeper = entries[0]
    ignore = ignore_predicates_in_merge

    for e in entries:
        if e == keeper:
            continue
        migrate_facts(store, e, keeper, map_graph, ignore_predicates=ignore)
        delete_subject_facts(store, e, map_graph)

def first_literal(store: Store, subj: NamedNode, pred: NamedNode, graph: NamedNode) -> str | None:
    for q in store.quads_for_pattern(subj, pred, None, graph_name=graph):
        if isinstance(q.object, Literal) and q.object.value is not None:
            return str(q.object.value)
    return None

def has_any_facts(store: Store, node: NamedNode, graph: NamedNode) -> bool:
    for _ in store.quads_for_pattern(node, None, None, graph_name=graph):
        return True
    return False

def iter_mapping_entries(store: Store, map_graph: NamedNode):
    for q in store.quads_for_pattern(None, RDF_TYPE_NODE, MAP_ENTRY_TYPE_NODE, graph_name=map_graph):
        if isinstance(q.subject, NamedNode):
            yield q.subject

def get_target_of_entry(store: Store, entry: NamedNode, map_graph: NamedNode) -> NamedNode | None:
    for q in store.quads_for_pattern(entry, MAP_TARGET_NODE, None, graph_name=map_graph):
        if isinstance(q.object, NamedNode):
            return q.object
    return None

def get_type_hints_of_entry(store: Store, entry: NamedNode, map_graph: NamedNode) -> list[str]:
    out = []
    for q in store.quads_for_pattern(entry, MAP_TYPE_HINT_NODE, None, graph_name=map_graph):
        if isinstance(q.object, NamedNode):
            out.append(q.object.value)
    return out

def get_rdf_types_of_entity(store: Store, node: NamedNode, entity_graph: NamedNode) -> list[str]:
    out = []
    for q in store.quads_for_pattern(node, RDF_TYPE_NODE, None, graph_name=entity_graph):
        if isinstance(q.object, NamedNode):
            out.append(q.object.value)
    return out

def stable_int_id(value: str) -> int:
    """Return a deterministic positive 63-bit integer for a string."""
    digest = hashlib.blake2b(
        value.encode("utf-8"),
        digest_size=8,
        person=b"scigma-note-id",
    ).digest()

    return int.from_bytes(digest, byteorder="big") & ((1 << 63) - 1)