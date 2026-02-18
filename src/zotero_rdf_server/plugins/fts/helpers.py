import json
import re
from typing import Literal, Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse
import requests
import subprocess, importlib, sys, os
from pathlib import Path
import hashlib
from functools import lru_cache


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
        return logger_z

def ensure_import(module, attr=None, requirements=requirements):
    modname = re.split(r"(?:==|!=|<=|>=|<|>|~=)", module, 1)[0]

    try:
        mod = importlib.import_module(modname)

    except ImportError:
        try:
            plugin_logger().warning(
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

            mod = importlib.import_module(modname)

        except Exception as e:
            plugin_logger().error(e, exc_info=True)
            raise

    return getattr(mod, attr) if attr else mod

@lru_cache(maxsize=1)
def resolve_config_path(config_path: Optional[str] = None) -> Path:
    def is_url(s: str) -> bool:
        u = urlparse(s)        
        return u.scheme in ("http", "https") and bool(u.netloc)

    raw = config_path or os.getenv("FTS_CONFIG")

    if raw:
        plugin_logger().info(f"Loading config from ENV: {raw}")
        if is_url(raw):
            cache_dir = Path("./tmp/fts_config") #  TODO improve caching
            cache_dir.mkdir(parents=True, exist_ok=True)

            fname = hashlib.sha256(raw.encode()).hexdigest()[:16] + ".yml"
            target = cache_dir / fname

            if not target.exists():
                r = requests.get(raw, timeout=15)
                r.raise_for_status()
                target.write_text(r.text, encoding="utf-8")

            return target.resolve()

        return Path(raw).expanduser().resolve()

    fallback = Path(__file__).resolve().parent / "fts_config.yml"
    plugin_logger().info(f"Loading config from fallback: {str(fallback)}")
    return fallback

def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

# def _download(url: str, dest: Path) -> None:
#     import requests
#     dest.parent.mkdir(parents=True, exist_ok=True)
#     with requests.get(url, stream=True, timeout=60) as r:
#         r.raise_for_status()
#         tmp = dest.with_suffix(dest.suffix + ".part")
#         with open(tmp, "wb") as f:
#             for chunk in r.iter_content(chunk_size=1024 * 1024):
#                 if chunk:
#                     f.write(chunk)
#         tmp.replace(dest)


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

Kind = Literal["pdf", "iiif", "xml", "html", "text", "json"]

def detect_file_kind(path: Path) -> Optional[Kind]:
    ext = path.suffix.lower()
    return {
        ".json": "json",
        ".pdf": "pdf",
        ".txt": "text",
        ".html": "html",
        ".htm": "html",
        ".xml": "xml",
    }.get(ext, None)

def is_url(s: str) -> bool:
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


_IIIF_CTX_RE = re.compile(r"iiif\.io/api/presentation/[23]/context\.json", re.I)
_IIIF_TYPE_RE = re.compile(r'"(@type|type)"\s*:\s*"(sc:Manifest|Manifest)"', re.I)

def _is_probably_iiif_json_bytes(b: bytes) -> bool:
    s = b.decode("utf-8", errors="ignore")
    if _IIIF_CTX_RE.search(s):
        return True
    if _IIIF_TYPE_RE.search(s):
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

def safe_doc_id(doc_id: str) -> str:
    s = doc_id.strip()
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE)
    return s[:200] or "doc"

def clean_ocr(text: str) -> str:
    text = text.replace("\x0c", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

# TODO

RegexRule = Tuple[Union[str, re.Pattern], str]

CLEAN_CFG = {  "clean": {
    "replace_formfeed": True,
    "normalize_whitespace": True,
    "rules": [
      {"pattern": r"[\u00AD]", "repl": ""},
      {"pattern": r"-\s+", "repl": ""},
      {"pattern": r"\s*\n\s*", "repl": " ", "flags": ["M"]}
    ],
    "vignette": {"left": 200, "right": 200, "mode": "chars"}
  }}

def _compile_rules(rules_json: Optional[Sequence[Dict[str, Any]]]) -> List[RegexRule]:
    """
    JSON-Format pro Regel:
      {"pattern": "...", "repl": "...", "flags": ["I","M","S","U","A","X"]}  # flags optional
    """
    if not rules_json:
        return []

    flag_map = {
        "I": re.IGNORECASE,
        "M": re.MULTILINE,
        "S": re.DOTALL,
        "U": re.UNICODE,
        "A": re.ASCII,
        "X": re.VERBOSE,
    }

    compiled: List[RegexRule] = []
    for r in rules_json:
        pat = r.get("pattern")
        repl = r.get("repl", "")
        if not isinstance(pat, str):
            raise ValueError(f"Rule missing string 'pattern': {r!r}")

        flags_val = 0
        for f in (r.get("flags") or []):
            if f not in flag_map:
                raise ValueError(f"Unknown regex flag {f!r} in rule {r!r}")
            flags_val |= flag_map[f]

        compiled.append((re.compile(pat, flags_val), str(repl)))
    return compiled

def _apply_vignette(text: str, vignette: Optional[Dict[str, Any]]) -> str:
    """
    JSON-Format:
      {"left": 0, "right": 0, "mode": "chars"|"words"|"lines"}
    """
    if not vignette:
        return text

    left = int(vignette.get("left", 0) or 0)
    right = int(vignette.get("right", 0) or 0)
    mode = str(vignette.get("mode", "chars"))

    if left <= 0 and right <= 0:
        return text

    if mode == "chars":
        start = left
        end = None if right <= 0 else -right
        return text[start:end]

    if mode == "words":
        parts = text.split()
        start = left
        end = None if right <= 0 else max(0, len(parts) - right)
        return " ".join(parts[start:end])

    if mode == "lines":
        lines = text.splitlines()
        start = left
        end = None if right <= 0 else max(0, len(lines) - right)
        return "\n".join(lines[start:end])

    raise ValueError(f"Unknown vignette mode: {mode!r}")

def clean_ocr_new(
    text: str,
    *,
    rules: Optional[Sequence[Dict[str, Any]]] = None,
    normalize_whitespace: bool = True,
    replace_formfeed: bool = True,
    vignette: Optional[Dict[str, Any]] = None,
    vignette_before_strip: bool = False,
) -> str:
    if replace_formfeed:
        text = text.replace("\x0c", " ")

    compiled = _compile_rules(rules)

    for pattern, repl in compiled:
        text = pattern.sub(repl, text)

    if normalize_whitespace:
        text = re.sub(r"\s+", " ", text)

    if not vignette_before_strip:
        text = text.strip()

    text = _apply_vignette(text, vignette)

    return text.strip()