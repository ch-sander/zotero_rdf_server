import json
import re
from typing import Literal, Optional, Any
from urllib.parse import urlparse
import requests
import subprocess, importlib, sys, os
from pathlib import Path
import hashlib
import datetime
from functools import lru_cache
from typing import Any
from uuid import uuid4

here = Path(__file__).resolve().parent
requirements = here / "requirements.txt"

def plugin_logger(new:bool=False):
    if new:
        import logging
        logger = logging.getLogger("fts_plugin")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            h = logging.StreamHandler()
            h.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                "[%(levelname)s] %(name)s: %(message)s"
            )
            h.setFormatter(formatter)
            logger.addHandler(h)

        return logger
    else:
        from zotero_rdf_server.logging_config import logger as logger_z
        logger_z.propagate = False
        return logger_z

@lru_cache()
def ensure_import(module, attr=None, requirements=requirements):
    from zotero_rdf_server import utils
    return utils.ensure_import(module=module,attr=attr,requirements=requirements)


@lru_cache(maxsize=1)
def resolve_config_path(config_path: Optional[str] = None) -> Path:
    # def is_url(s: str) -> bool:
    #     u = urlparse(s)        
    #     return u.scheme in ("http", "https") and bool(u.netloc)

    raw = config_path or os.getenv("FTS_CONFIG")

    if raw:
        plugin_logger().info(f"Loading config from ENV: {raw}")
        if is_url(raw):
            cache_dir = Path("./tmp/fts_config") #  TODO improve caching
            cache_dir.mkdir(parents=True, exist_ok=True)

            fname = hashlib.sha256(raw.encode()).hexdigest()[:16] + ".yml"
            target = cache_dir / fname

            if not target.exists():
                r = requests.get(raw, timeout=15) # TODO set header
                r.raise_for_status()
                target.write_text(r.text, encoding="utf-8")

            return target.resolve()

        return Path(raw).expanduser().resolve()

    fallback = Path(__file__).resolve().parent / "fts_config.yml"
    plugin_logger().info(f"Loading config from fallback: {str(fallback)}")
    return fallback
  
def write_data_to_file(data, filename):
    with open(filename, "w", encoding="utf-8") as f:
        if isinstance(data, (dict, list)):
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            f.write(str(data))

def ISO_ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def make_json_safe(obj):
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [make_json_safe(v) for v in obj]
    return str(obj)  # fallback

def get_worker_slice(items, worker_id, total_workers):
    n = len(items)

    start = (n * worker_id) // total_workers
    end = (n * (worker_id + 1)) // total_workers

    return items[start:end]

def convert_bindings(bindings, reverse: bool = False):
    def parse_number_if_exact(val):
        try:
            i = int(val)
            if str(i) == val:
                return i
        except (ValueError, TypeError):
            pass

        try:
            f = float(val)
            if str(f) == val:
                return f
        except (ValueError, TypeError):
            pass

        return None

    if isinstance(bindings, dict):
        plugin_logger().info("Found SPARQL JSON as bindings!")
        # SPARQL-JSON
        var_names = bindings["head"]["vars"]
        rows = bindings["results"]["bindings"]

        def get_value(sol, name):
            entry = sol.get(name)
            return None if entry is None else entry.get("value")

    elif isinstance(bindings, list):
        plugin_logger().info("Found JSON list as bindings!")
        rows = bindings
        # var_names = list({k for row in rows for k in row.keys()})
        var_names = list(dict.fromkeys(k for row in rows for k in row.keys()))

        def get_value(sol, name):
            return sol.get(name)

    else:
        plugin_logger().info("Found QueryResult as bindings!")
        var_names = [v.value for v in bindings.variables]
        rows = list(bindings)

        def get_value(sol, name):
            entry = sol[name]
            return None if entry is None else entry.value

    if reverse:
        rows = list(rows)[::-1]
        plugin_logger().warning("Reverse = True")

    numeric_fields = set()
    for name in var_names:
        values = [
            get_value(sol, name)
            for sol in rows
            if get_value(sol, name) is not None
        ]

        if values and all(parse_number_if_exact(v) is not None for v in values):
            numeric_fields.add(name)

    items = []
    for sol in rows:
        items.append({
            name: (
                None if get_value(sol, name) is None
                else parse_number_if_exact(get_value(sol, name)) if name in numeric_fields
                else get_value(sol, name)
            )
            for name in var_names
        })

    return items, var_names

def _download(url: str, dest: Path) -> None:
    import requests
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    dest.parent.mkdir(parents=True, exist_ok=True)

    def _strip_download_param(u: str) -> str:
        parts = urlsplit(u)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "download"]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) KrakenModelFetcher/1.0",
        "Accept": "*/*",
    }

    urls_to_try = [url, _strip_download_param(url)]

    last_exc = None
    for u in urls_to_try:
        try:
            with requests.get(u, stream=True, timeout=180, headers=headers, allow_redirects=True) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                tmp.replace(dest)
                return
        except Exception as e:
            last_exc = e

    raise RuntimeError(f"Download failed for all variants of {url!r}") from last_exc

Kind = Literal["pdf", "iiif", "xml", "html", "text", "json", "csv"]

def detect_file_kind(
    path: Path,
    sniff_bytes: int = 16384,
) -> Optional[Kind]:
    try:
        with path.open("rb") as f:
            prefix = f.read(sniff_bytes)

            if _looks_like_pdf(prefix):
                return "pdf"

            mk = _sniff_markup(prefix)
            if mk:
                return mk

            jt = _sniff_text_vs_json(prefix)

            if jt == "json" or path.suffix.lower() == ".json":
                def fetch_more(step: int, offset: int) -> bytes:
                    f.seek(offset)
                    return f.read(step) or b""

                return (
                    "iiif"
                    if _is_probably_iiif_json(prefix, fetch_more_cb=fetch_more)
                    else "json"
                )

    except OSError:
        return None

    ext = path.suffix.lower()
    return {
        ".txt": "text",
        ".csv": "csv",
        ".html": "html",
        ".htm": "html",
        ".xml": "xml",
    }.get(ext, None)

def is_url(s: str) -> bool:
    try:
        from zotero_rdf_server.utils import is_url
        return is_url(s)
    except Exception:
        try:
            u = urlparse(s)
            return u.scheme in ("http", "https")
        except Exception:
            return False
    
def resolve_source(src: str) -> tuple[str, Path | None]:
    if is_url(src):
        return "url", None
    p = Path(src)
    if p.exists():
        return "file", p.resolve()
    raise FileNotFoundError(f"Source not found: {src}")

def _norm_ctype(ctype: str) -> str:
    return (ctype or "").split(";")[0].strip().lower()

def _looks_like_pdf(prefix: bytes) -> bool:
    return prefix.startswith(b"%PDF-")

def _strip_bom_and_ws(b: bytes) -> bytes:
    # UTF-8 BOM + leading whitespace/newlines
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    return b.lstrip()

def _sniff_markup(prefix: bytes) -> Optional[Kind]:
    p = _strip_bom_and_ws(prefix)
    # HTML often starts with <!doctype html> or <html ...>
    if p[:64].lower().startswith(b"<!doctype html") or p[:16].lower().startswith(b"<html"):
        return "html"
    # XML starts with <?xml ...?> or <tag ...> where tag is not html (best-effort)
    if p.startswith(b"<?xml"):
        return "xml"
    if p.startswith(b"<"):
        # Could be xml or html; try a quick heuristic
        head = p[:256].lower()
        if b"<html" in head or b"<!doctype html" in head:
            return "html"
        return "xml"
    return None

def _sniff_text_vs_json(prefix: bytes) -> Optional[Kind]:
    p = _strip_bom_and_ws(prefix)
    if not p:
        return "text"  # empty-ish content -> treat as text
    if p[:1] in (b"{", b"["):
        return "json"
    return "text"


_IIIF_CTX_RE = re.compile(r"iiif\.io/api/presentation/[23]/", re.I)
_IIIF_MANIFEST_TYPE_RE = re.compile(r'"(@type|type)"\s*:\s*"(sc:Manifest|Manifest)"', re.I)
_IIIF_SC_TYPE_RE = re.compile(r'"@type"\s*:\s*"sc:[A-Za-z]+"\s*', re.I)
_IIIF_URL_HINT_RE = re.compile(r'(iiif|i3f|/iiif/)', re.I)

def _is_probably_iiif_json_bytes(b: bytes) -> bool:
    s = b.decode("utf-8", errors="ignore")

    if _IIIF_CTX_RE.search(s):
        return True
    if _IIIF_MANIFEST_TYPE_RE.search(s):
        return True

    if _IIIF_SC_TYPE_RE.search(s) and _IIIF_URL_HINT_RE.search(s):
        return True

    if '"sequences"' in s and '"canvases"' in s:
        return True
    if '"items"' in s and ('"@context"' in s or '"type"' in s):
        return True

    return False

def _is_probably_iiif_json(
    initial: bytes,
    fetch_more_cb=None,
    max_total: int = 512_000,   # 512 KB cap
    step: int = 64_000,
) -> bool:
    if _is_probably_iiif_json_bytes(initial):
        return True

    if fetch_more_cb is None:
        return False

    buf = bytearray(initial)
    while len(buf) < max_total:
        more = fetch_more_cb(step, len(buf))
        if not more:
            break
        buf.extend(more)
        if _is_probably_iiif_json_bytes(buf):
            return True

    try:
        txt = bytes(buf).decode("utf-8", errors="strict")
        obj = json.loads(txt)
    except Exception:
        return False

    if isinstance(obj, dict):
        t = (obj.get("type") or obj.get("@type") or "")
        if str(t).lower() in ("manifest", "sc:manifest"):
            return True
        ctx = obj.get("@context")
        if isinstance(ctx, str) and "iiif.io/api/presentation" in ctx.lower():
            return True
        if isinstance(ctx, list) and any(isinstance(x, str) and "iiif.io/api/presentation" in x.lower() for x in ctx):
            return True

    return False


def detect_url_kind(
    url: str,
    timeout: int = 30,
    sniff_bytes: int = 16384,
    session: Optional[requests.Session] = None,
) -> Kind:
    s = session or requests.Session()

    # 1) HEAD best-effort
    ctype = ""
    try:
        with s.head(url, allow_redirects=True, timeout=timeout) as h:
            ctype = _norm_ctype(h.headers.get("Content-Type", ""))
            if ctype == "application/pdf":
                return "pdf"
            if ctype in ("text/html", "application/xhtml+xml"):
                return "html"
            if ctype in ("application/xml", "text/xml"):
                return "xml"
            if ctype.startswith("text/plain"):
                return "text"
    except requests.RequestException:
        pass

    headers = {
        "Range": f"bytes=0-{sniff_bytes-1}",
        "Accept": "application/ld+json, application/json;q=0.9, */*;q=0.1",
    }

    try:
        with s.get(url, stream=True, allow_redirects=True, headers=headers, timeout=timeout) as r:
            r.raise_for_status()
            ctype_get = _norm_ctype(r.headers.get("Content-Type", "")) or ctype

            r.raw.decode_content = True
            prefix = r.raw.read(sniff_bytes) or b""

            # a) Magic bytes
            if _looks_like_pdf(prefix):
                return "pdf"

            # b) Markup sniff
            mk = _sniff_markup(prefix)
            if mk:
                if ctype_get in ("text/html", "application/xhtml+xml"):
                    return "html"
                if ctype_get in ("application/xml", "text/xml"):
                    return "xml"
                return mk

            # c) JSON vs text sniff
            jt = _sniff_text_vs_json(prefix)

            def fetch_more(step: int, offset: int) -> bytes:
                h2 = {
                    "Range": f"bytes={offset}-{offset+step-1}",
                    "Accept": headers["Accept"],
                }
                rr = s.get(r.url, stream=True, allow_redirects=True, headers=h2, timeout=timeout)
                rr.raise_for_status()
                rr.raw.decode_content = True
                return rr.raw.read(step) or b""

            # If it looks like JSON OR server labels it JSON: try IIIF detection with fallback fetch_more
            if jt == "json" or ctype_get in ("application/json", "application/ld+json", "application/jsonld+json"):
                is_iiif = _is_probably_iiif_json(
                    prefix,
                    fetch_more_cb=fetch_more,
                )
                return "iiif" if is_iiif else "json"

            # d) Header-based text fallback
            if ctype_get.startswith("text/plain"):
                return "text"

            # e) Otherwise default to text
            return "text"

    except requests.RequestException:
        return "text"
    
def format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _save_pil(im, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)

    width, height = im.size
    file_size = path.stat().st_size

    logger.info(
        "Stored image file: %s (%dx%d px, %s)",
        path,
        width,
        height,
        _format_size(file_size),
    )


def safe_doc_id(doc_id: str) -> str:
    s = doc_id.strip()
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE)
    return s[:200] or "doc"

def clean_ocr(text: str) -> str:
    text = text.replace("\x0c", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def le_item_to_wadm_annotation(
    item: dict[str, Any],
    *,
    source_uri: str,
    annotation_id_base: str | None = None,
    creator: str | None = "zotero-rdf-server",
) -> dict[str, Any]:
    """
    Translate one grounded LangExtract item to a Web Annotation Data Model annotation.

    Expected item:
    {
        "concept": "...",
        "normalized": "...",
        "translation": "...",
        "start": 123,
        "end": 145,
        "evidence": "..."
    }
    """

    start = item.get("start")
    end = item.get("end")

    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError("WADM annotation requires integer start/end offsets.")

    concept = item.get("concept")
    if not isinstance(concept, str) or not concept.strip():
        raise ValueError("WADM annotation requires a non-empty concept.")

    annotation_id = (
        f"{annotation_id_base.rstrip('/')}/{uuid4()}"
        if annotation_id_base
        else f"urn:uuid:{uuid4()}"
    )

    bodies: list[dict[str, Any]] = [
        {
            "type": "TextualBody",
            "purpose": "tagging",
            "value": concept,
            "language": "la",
        }
    ]

    normalized = item.get("normalized")
    if isinstance(normalized, str) and normalized.strip():
        bodies.append({
            "type": "TextualBody",
            "purpose": "normalizing",
            "value": normalized,
            "language": "la",
        })

    translation = item.get("translation")
    if isinstance(translation, str) and translation.strip():
        bodies.append({
            "type": "TextualBody",
            "purpose": "describing",
            "value": translation,
            "language": "en",
        })

    evidence = item.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        bodies.append({
            "type": "TextualBody",
            "purpose": "evidencing",
            "value": evidence,
            "language": "la",
        })

    annotation: dict[str, Any] = {
        "@context": "http://www.w3.org/ns/anno.jsonld",
        "id": annotation_id,
        "type": "Annotation",
        "motivation": "tagging",
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "body": bodies,
        "target": {
            "source": source_uri,
            "selector": {
                "type": "TextPositionSelector",
                "start": start,
                "end": end,
            },
        },
    }

    if creator:
        annotation["creator"] = {
            "type": "Software",
            "name": creator,
        }

    return annotation


def le_output_to_wadm(
    items: list[dict[str, Any]],
    *,
    source_uri: str,
    annotation_id_base: str | None = None,
    creator: str | None = "zotero-rdf-server",
) -> list[dict[str, Any]]:
    return [
        le_item_to_wadm_annotation(
            item,
            source_uri=source_uri,
            annotation_id_base=annotation_id_base,
            creator=creator,
        )
        for item in items
        if isinstance(item.get("start"), int)
        and isinstance(item.get("end"), int)
    ]