from __future__ import annotations
import json
import re
from typing import Literal, Optional
from urllib.parse import urlparse
import requests
import subprocess, importlib, sys, os
from typing import Optional
from pathlib import Path
import hashlib

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

def resolve_config_path(config_path: Optional[str] = None) -> Path:
    if config_path:
        return Path(config_path).expanduser().resolve()

    env = os.getenv("FTS_CONFIG")
    if env:
        plugin_logger().info(f"Loading config from ENV: {env}")
        return Path(env).expanduser().resolve()
    fallback = Path(__file__).resolve().parent / "config.yml"
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


def _is_probably_iiif_json(snippet: bytes) -> bool:
    """
    IIIF Presentation v2/v3 manifests are JSON(-LD).
    We sniff by looking for canonical keys in the first chunk.
    Avoid full parse if not needed, but be willing to parse a bit of JSON.
    """
    # Fast, cheap string sniff
    try:
        s = snippet.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False

    # Common IIIF hints (v2/v3)
    if '"type":"manifest"' in s or '"@type":"sc:manifest"' in s:
        return True
    if '"iiif.io/api/presentation"' in s:
        return True
    if '"@context"' in s and ("iiif" in s and "presentation" in s):
        return True
    if '"items"' in s and '"id"' in s and ("manifest" in s):
        # weak hint; try JSON parse for confirmation
        pass

    # Stronger check: parse small JSON if it looks like JSON
    p = _strip_bom_and_ws(snippet)
    if not (p.startswith(b"{") or p.startswith(b"[")):
        return False
    try:
        obj = json.loads(p.decode("utf-8", errors="strict"))
    except Exception:
        return False

    # v3: {"type":"Manifest", "@context": "...presentation/3/context.json", ...}
    # v2: {"@type":"sc:Manifest", "@context": "...presentation/2/context.json", ...}
    def get_context(o):
        return o.get("@context") if isinstance(o, dict) else None

    def context_mentions_iiif(ctx) -> bool:
        if isinstance(ctx, str):
            return "iiif.io/api/presentation" in ctx.lower()
        if isinstance(ctx, list):
            return any(isinstance(x, str) and "iiif.io/api/presentation" in x.lower() for x in ctx)
        return False

    if isinstance(obj, dict):
        t = (obj.get("type") or obj.get("@type") or "").lower()
        if t in ("manifest", "sc:manifest"):
            return True
        ctx = get_context(obj)
        if context_mentions_iiif(ctx):
            return True

    return False


def detect_url_kind(
    url: str,
    timeout: int = 30,
    sniff_bytes: int = 16384,  # enough to include JSON-LD @context etc.
    session: Optional[requests.Session] = None,
) -> Kind:
    s = session or requests.Session()

    # 1) HEAD best-effort (don’t trust it fully)
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
            # JSON could be IIIF; defer to sniff
    except requests.RequestException:
        pass

    # 2) GET with Range (if supported) to avoid downloading everything
    headers = {"Range": f"bytes=0-{sniff_bytes-1}"}
    try:
        with s.get(url, stream=True, allow_redirects=True, headers=headers, timeout=timeout) as r:
            r.raise_for_status()
            ctype_get = _norm_ctype(r.headers.get("Content-Type", "")) or ctype

            # Read small prefix
            r.raw.decode_content = True
            prefix = r.raw.read(sniff_bytes) or b""

            # a) Magic bytes
            if _looks_like_pdf(prefix):
                return "pdf"

            # b) Markup sniff
            mk = _sniff_markup(prefix)
            if mk:
                # If content-type says xhtml/xml/html, respect it, otherwise trust sniff
                if ctype_get in ("text/html", "application/xhtml+xml"):
                    return "html"
                if ctype_get in ("application/xml", "text/xml"):
                    return "xml"
                return mk

            # c) JSON vs text sniff
            jt = _sniff_text_vs_json(prefix)
            if jt == "json":
                # IIIF check
                if _is_probably_iiif_json(prefix):
                    return "iiif"
                return "json"

            # d) Header-based text fallback
            if ctype_get.startswith("text/plain"):
                return "text"

            # e) If it's labeled JSON, keep it JSON
            if ctype_get in ("application/json", "application/ld+json", "application/jsonld+json"):
                return "iiif" if _is_probably_iiif_json(prefix) else "json"

            # f) Otherwise default to text (safer than “json” for unknown octet-stream)
            return "text"

    except requests.RequestException:
        # Conservative fallback
        return "text"

def safe_doc_id(doc_id: str) -> str:
    s = doc_id.strip()
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE)
    return s[:200] or "doc"

def clean_ocr(text: str) -> str:
    text = text.replace("\x0c", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()