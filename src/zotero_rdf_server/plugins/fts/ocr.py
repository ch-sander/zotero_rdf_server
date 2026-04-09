from dataclasses import dataclass, field
from typing import Iterator, Optional, Dict, Any, List, Literal, Union, Tuple, Mapping, Iterable, Callable, Set, Sequence, Protocol
import io, json, os, tempfile, time, re, math, requests
from functools import lru_cache
from pathlib import Path
from .helpers import ensure_import, _hash_file,  resolve_config_path, _download, plugin_logger, detect_url_kind, detect_file_kind, resolve_source
from io import BytesIO
from urllib.parse import urlparse

from .helpers import plugin_logger, safe_doc_id
logger=plugin_logger()

ensure_import("PIL")
ensure_import("lxml")
from PIL import Image
from lxml import etree # TODO import


try:
    from zotero_rdf_server.config import APP_USER
except Exception:
    APP_USER = None

_KRAKEN_NET: dict[tuple[str, str], object] = {}
_KRAKEN_SEG: dict[tuple[str, str], object] = {}
_NO_UPSCALE_HOSTS: set[str] = set()

@dataclass(frozen=True)
class PdfTextPolicy:
    enabled: bool = True
    min_chars: int = 80
    min_alpha_ratio: float = 0.6

    @classmethod # policy = PdfTextPolicy.from_json(request.json.get("pdf_text_policy"))
    def from_json(cls, data: Mapping[str, Any]) -> "PdfTextPolicy":
        try:
            return cls(
                enabled=bool(data.get("enabled", True)),
                min_chars=int(data.get("min_chars", 80)),
                min_alpha_ratio=float(data.get("min_alpha_ratio", 0.6)),
            )
        except (TypeError, ValueError):
            return cls()
        
@dataclass(frozen=True)
class IiifOcrRule:
    """
    Two rule kinds:

    1) kind="link":
       - key: where to look in the canvas JSON (e.g. "seeAlso", "rendering", "otherContent")
       - profile/profile_match: optional profile string matching
       - xpath/namespaces: XPath expression used on the fetched hOCR (parsed as XML/HTML)

    2) kind="derive":
       - derive_from: where to take the source string from (default: canvas "@id")
       - id_regex: regex with one capturing group for the page id
       - url_template: where to plug the extracted id (default points to e-rara plaintext endpoint)
       - xpath/namespaces are ignored for kind="derive"
    """
    kind: str = "link"  # "link" | "derive"

    # existing "link" fields
    key: str = "seeAlso"
    profile: Optional[str] = None
    profile_match: str = "equals"  # "equals" | "contains"
    xpath: str = r"//x:span[contains(concat(' ', normalize-space(@class), ' '), ' ocr_line ')]"
    namespaces: Mapping[str, str] = field(default_factory=lambda: {"x": "http://www.w3.org/1999/xhtml"})

    derive_from: str = "@id"  # currently: only canvas-level fields (default: canvas["@id"])
    id_regex: str = r"/(\d+)$"
    url_template: str = "https://www.e-rara.ch/download/fulltext/plain/{id}"

    def matches_profile(self, p: Optional[str]) -> bool:
        # Only meaningful for kind="link"; for kind="derive" we treat as "match".
        if self.kind != "link":
            return True

        if self.profile is None:
            return True
        if not p:
            return False
        if self.profile_match == "contains":
            return self.profile in p
        return p == self.profile

    @staticmethod
    def default_rules() -> tuple["IiifOcrRule", ...]:
        return (
            IiifOcrRule(),
            # e-rara Plaintext
            IiifOcrRule(
                kind="derive",
                derive_from="@id",
                id_regex=r"^https?://www\.e-rara\.ch/.*/canvas/(\d+)$",
                url_template="https://www.e-rara.ch/download/fulltext/plain/{id}",               
                key="@id",
                xpath="",
            ),
        )


@dataclass(frozen=True)
class IiifOcrPolicy:
    enabled: bool = False
    min_chars: int = 20
    min_alpha_ratio: float = 0.5
    rules: Sequence[IiifOcrRule] = field(default_factory=tuple)
    timeout: int = 30

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "IiifOcrPolicy":
        try:
            enabled = bool(data.get("enabled", False))
            min_chars = int(data.get("min_chars", 20))
            min_alpha_ratio = float(data.get("min_alpha_ratio", 0.5))
            timeout = int(data.get("timeout", 30))

            rules_in = data.get("rules", None)
            rules: list[IiifOcrRule] = []

            if enabled and rules_in is None:
                rules = list(IiifOcrRule.default_rules())
            else:
                for r in (rules_in or []):
                    if not isinstance(r, Mapping):
                        continue

                    kind = str(r.get("kind", "link"))

                    # start with defaults, then override only what is supplied
                    base = IiifOcrRule(kind=kind) if kind != "link" else IiifOcrRule()

                    rules.append(
                        IiifOcrRule(
                            kind=kind,
                            key=str(r.get("key", base.key)),
                            profile=(None if r.get("profile") in (None, "") else str(r.get("profile"))),
                            profile_match=str(r.get("profile_match", base.profile_match)),
                            xpath=str(r.get("xpath") or base.xpath),
                            namespaces=dict(r.get("namespaces") or base.namespaces),
                            derive_from=str(r.get("derive_from", base.derive_from)),
                            id_regex=str(r.get("id_regex", base.id_regex)),
                            url_template=str(r.get("url_template", base.url_template)),
                        )
                    )

                if enabled and not rules:
                    rules = list(IiifOcrRule.default_rules())

            return cls(
                enabled=enabled,
                min_chars=min_chars,
                min_alpha_ratio=min_alpha_ratio,
                rules=tuple(rules),
                timeout=timeout,
            )
        except (TypeError, ValueError):
            return cls()

@dataclass(frozen=True)
class KrakenModelSpec:
    file: str
    url: Optional[str] = None
    checksum_algo: Optional[str] = None   # "md5" or "sha256"
    checksum: Optional[str] = None

@lru_cache(maxsize=8)
def get_ocr_cfg(config_path: Path) -> dict[str, Any]:
    from zotero_rdf_server.utils import load_dict_like
    return load_dict_like(config_path, label="OCR Config", verbose=True) or {}  
 
@lru_cache(maxsize=8)
def get_kraken_cfg(config_path: Path) -> dict[str, Any]:
    cfg = get_ocr_cfg(config_path)
    return cfg.get("kraken") or cfg

@lru_cache(maxsize=8)
def get_tesseract_cfg(config_path: Path) -> dict[str, Any]:
    cfg = get_ocr_cfg(config_path)
    return cfg.get("tesseract") or cfg

def resolve_domain(*, config_path: Path, domain: Optional[str]) -> str:
    if domain:
        return domain
    kcfg = get_kraken_cfg(config_path)
    active = kcfg.get("active") or {}
    if active.get("domain"):
        return active["domain"]
    if kcfg.get("default_domain"):
        return kcfg["default_domain"]
    return "print"  # hard fallback

def resolve_recognition_model_name(
    *,
    config_path: Path,
    domain: str,
    model_name: Optional[str],
) -> str:
    if model_name:
        return model_name
    kcfg = get_kraken_cfg(config_path)
    active = kcfg.get("active") or {}
    if active.get("model"):
        return active["model"]
    defaults = kcfg.get("default_models") or {}
    name = defaults.get(domain)
    if not name:
        raise KeyError(f"No default_models entry for domain={domain!r} in YAML.")
    return name

def resolve_segmentation_name(
    *,
    config_path: Path,
    segmenter: Optional[str],
) -> str:
    if segmenter:
        return segmenter
    kcfg = get_kraken_cfg(config_path)
    active = kcfg.get("active") or {}
    return active.get("segmentation") or "BLLA"

def load_segmentation_model(*, config_path: str, segmenter: Optional[str]):
    from kraken.lib import vgsl
    from importlib import resources

    seg_name = resolve_segmentation_name(config_path=config_path, segmenter=segmenter)

    if str(seg_name).upper() == "BLLA":
        default_seg_model = resources.files("kraken").joinpath("blla.mlmodel")
        return vgsl.TorchVGSLModel.load_model(default_seg_model)

    seg_path = resolve_kraken_model_path(config_path=config_path, model_name=seg_name)
    return vgsl.TorchVGSLModel.load_model(seg_path)

def _get_model_spec(kcfg: dict[str, Any], model_name: str) -> KrakenModelSpec:
    models = kcfg.get("models") or {}
    spec = models.get(model_name)
    if not spec:
        raise KeyError(f"Unknown Kraken model: {model_name!r}")
    return KrakenModelSpec(
        file=spec["file"],
        url=spec.get("url"),
        checksum_algo=spec.get("checksum_algo"),
        checksum=spec.get("checksum"),
    )

def resolve_kraken_model_path(
    *,
    config_path: Path | Path,
    model_name: str,
) -> Path:
    kcfg = get_kraken_cfg(config_path) or {}
    models_dir = Path(kcfg.get("models_dir", Path(__file__).resolve().parent / "models")).expanduser()
    
    spec = _get_model_spec(kcfg, model_name)

    path = models_dir / spec.file
    if path.exists():
        # optional verify
        if spec.checksum_algo and spec.checksum:
            got = _hash_file(path, spec.checksum_algo)
            if got.lower() != spec.checksum.lower():
                raise ValueError(
                    f"Checksum mismatch for {model_name}: expected {spec.checksum}, got {got}"
                )
        return path

    if not spec.url:
        raise FileNotFoundError(f"{path} missing and no url for {model_name} set.")

    _download(spec.url, path)

    if spec.checksum_algo and spec.checksum:
        got = _hash_file(path, spec.checksum_algo)
        if got.lower() != spec.checksum.lower():
            raise ValueError(
                f"Checksum mismatch for {model_name}: expected {spec.checksum}, got {got}"
            )

    return path

## TESSERARCT

def resolve_tesseract_lang(
    *,
    config_path: Path,
    domain: Optional[str] = None,
    lang: Optional[str] = None,
) -> str:
    if lang:
        return lang

    tcfg = get_tesseract_cfg(config_path)
    active = tcfg.get("active") or {}
    if active.get("lang"):
        return active["lang"]

    defaults = tcfg.get("defaults") or {}
    dom_cfg = defaults.get(domain or "", {})
    if isinstance(dom_cfg, dict) and dom_cfg.get("lang"):
        return dom_cfg["lang"]

    if tcfg.get("default_lang"):
        return tcfg["default_lang"]

    return "lat"

def resolve_tesseract_config(
    *,
    config_path: Path,
    domain: Optional[str] = None,
    config: Optional[str] = None,
) -> str:
    if config:
        return config

    tcfg = get_tesseract_cfg(config_path)
    active = tcfg.get("active") or {}
    if active.get("config"):
        return active["config"]

    defaults = tcfg.get("defaults") or {}
    dom_cfg = defaults.get(domain or "", {})
    if isinstance(dom_cfg, dict) and dom_cfg.get("config"):
        return dom_cfg["config"]

    return tcfg.get("default_config") or ""

def configure_tesseract_binary(*, config_path: Path) -> None:
    tcfg = get_tesseract_cfg(config_path)
    exe = tcfg.get("executable", "/usr/bin/tesseract")
    if exe:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = exe


def tesseract_image_to_text(
    im: Image.Image,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    lang: str | None = None,
    config: str | None = None,
    binarize: bool = False,
    ink_ratio_range: list | None = None,
) -> str:
    try:
        ensure_import("pytesseract")
        import pytesseract

        cfg_path = resolve_config_path(config_path)
        configure_tesseract_binary(config_path=cfg_path)

        lang_x = resolve_tesseract_lang(
            config_path=cfg_path,
            domain=domain,
            lang=lang,
        )
        config_x = resolve_tesseract_config(
            config_path=cfg_path,
            domain=domain,
            config=config,
        )

        work = im

        if ink_ratio_range:
            r, bg, thr = ink_ratio(work)
            logger.debug(f"Found page for tesseract, r={r:.5f}, bg={bg:.1f}, thr={thr:.1f}")
            if r < ink_ratio_range[0]:
                logger.warning(
                    f"Skipping page (blank), r={r:.5f}, bg={bg:.1f}, thr={thr:.1f}"
                )
                return ""
            if r > ink_ratio_range[1]:
                logger.warning(
                    f"Skipping page (too dark/ornament), r={r:.3f}, bg={bg:.1f}"
                )
                return ""

        if binarize:
            g = work.convert("L")
            a = np.asarray(g)
            thr = max(1, int(np.median(a) - 25))
            bw = (a > thr).astype(np.uint8) * 255
            work = Image.fromarray(bw)

        txt = pytesseract.image_to_string(work, lang=lang_x, config=config_x or "")
        logger.debug(txt)
        return txt or ""

    except Exception:
        logger.exception("Tesseract OCR failed")
        return ""
    

## OCR        

@dataclass(frozen=True)
class PageItem:
    index: int
    kind: Literal["image", "text"]
    data: Union[Image.Image, str]
    source: str
    meta: Optional[dict] = None
    total: int = 1

class _TextPolicyLike(Protocol):
    enabled: bool
    min_chars: int
    min_alpha_ratio: float

def is_usable_text(text: str, policy: _TextPolicyLike, *, log_label: str = "text") -> bool:
    if not getattr(policy, "enabled", False):
        return False
    if not text:
        return False

    t = text.strip()
    if len(t) < getattr(policy, "min_chars", 0):
        return False

    alpha = sum(c.isalpha() for c in t)
    ratio = alpha / max(len(t), 1)
    logger.debug(f"Found usable {log_label} (alpha_ratio={ratio:.3f}, len={len(t)})")
    return ratio >= getattr(policy, "min_alpha_ratio", 0.0)

def _iter_link_objs(canvas: dict, key: str):
    obj = canvas.get(key)
    if obj is None:
        return
    if isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict):
                yield x
    elif isinstance(obj, dict):
        yield obj

def _rule_matches_profile(rule, profile: Optional[str]) -> bool:
    if rule.profile is None:
        return True
    if not profile:
        return False
    if getattr(rule, "profile_match", "equals") == "contains":
        return rule.profile in profile
    return profile == rule.profile

def _derive_url_from_canvas(canvas: dict, rule) -> Optional[str]:
    # Default: canvas["@id"] -> .../plain/{id}
    src_key = getattr(rule, "derive_from", "@id")
    src = canvas.get(src_key) if src_key != "@id" else (canvas.get("@id") or canvas.get("id"))
    if not isinstance(src, str) or not src:
        return None

    pattern = getattr(rule, "id_regex", r"/(\d+)$")
    m = re.search(pattern, src)
    if not m:
        return None

    page_id = m.group(1)
    tmpl = getattr(rule, "url_template", "https://www.e-rara.ch/download/fulltext/plain/{id}")
    try:
        return tmpl.format(id=page_id)
    except Exception:
        return None


def find_ocr(canvas: dict, policy) -> Optional[tuple[str, Any, Optional[str]]]:
    if not policy or not getattr(policy, "enabled", False):
        return None

    for rule in getattr(policy, "rules", ()):
        kind = getattr(rule, "kind", "link")

        if kind == "derive":
            url = _derive_url_from_canvas(canvas, rule)
            if url:
                return url, rule, None
            continue

        for obj in _iter_link_objs(canvas, rule.key):
            url = obj.get("@id") or obj.get("id")
            profile = obj.get("profile")
            if url and _rule_matches_profile(rule, profile):
                return str(url), rule, (None if profile is None else str(profile))

    return None

def ocr_bytes_to_text(ocr_bytes: bytes, rule) -> str:

    kind = getattr(rule, "kind", "link")
    xpath = getattr(rule, "xpath", "") or ""

    if kind == "derive" or not xpath.strip():
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                txt = ocr_bytes.decode(enc)
                break
            except Exception:
                txt = ""
        return txt.strip()
    
    namespaces = dict(getattr(rule, "namespaces", None) or {})
    dehyphenate = bool(getattr(rule, "dehyphenate", True))    
    parts = []
    used_html_fallback = False

    # 1) XML first (keeps namespaces)
    try:
        root = etree.fromstring(ocr_bytes, parser=etree.XMLParser(recover=True, encoding="utf-8"))
        parts = root.xpath(xpath, namespaces=namespaces) if xpath else []
    except Exception:
        parts = []

    # 2) HTML fallback (namespace-less): retry without prefixes
    if not parts:
        used_html_fallback = True
        root = etree.fromstring(ocr_bytes, parser=etree.HTMLParser(recover=True, encoding="utf-8"))
        xpath_no_ns = xpath.replace("x:", "") if xpath else ""
        parts = root.xpath(xpath_no_ns) if xpath_no_ns else []

    if not parts:
        return ""

    # If XPath returns nodes (e.g., ocr_line containers), preserve structure with newlines.
    if not isinstance(parts[0], str):
        lines: list[str] = []
        for node in parts:
            # node may be Element, AttributeResult, etc. -> itertext() handles Elements
            txt = " ".join(t.strip() for t in node.itertext() if t and t.strip())
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt:
                lines.append(txt)

        if not lines:
            return ""

        if dehyphenate:
            merged: list[str] = []
            for line in lines:
                if merged and merged[-1].endswith("-"):
                    merged[-1] = merged[-1][:-1] + line.lstrip()
                else:
                    merged.append(line)
            return "\n".join(merged).strip()

        return "\n".join(lines).strip()

    # Otherwise it's a list of strings (often ocrx_word/text()) -> flatten with spaces.
    out = []
    for p in parts:
        if isinstance(p, str):
            s = re.sub(r"\s+", " ", p).strip()
            if s:
                out.append(s)

    txt = " ".join(out)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt

# -----------------------------
# Small helpers
# -----------------------------

def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]

def _get_id(o: Any) -> Optional[str]:
    if isinstance(o, dict):
        return o.get("id") or o.get("@id")
    return None

def _iter_services(node: Any) -> Iterable[Dict[str, Any]]:
    """
    Yield service dicts from a node that can be:
    - dict with "service" being dict or list
    - list of dicts
    - directly a service dict
    """
    if node is None:
        return
    if isinstance(node, dict) and "service" in node:
        for s in _as_list(node.get("service")):
            if isinstance(s, dict):
                yield s
    elif isinstance(node, list):
        for s in node:
            if isinstance(s, dict):
                yield s
    elif isinstance(node, dict):
        # sometimes the service dict is given directly
        yield node

def _service_profiles(service: Dict[str, Any]) -> List[str]:
    prof = service.get("profile")
    if isinstance(prof, str):
        return [prof]
    if isinstance(prof, list):
        return [p for p in prof if isinstance(p, str)]
    return []

def _looks_like_image_service(service: Dict[str, Any]) -> bool:
    """
    Best-effort filter: prefer IIIF Image API services.
    Many manifests omit 'profile' though, so we fall back to heuristics.
    """
    sid = (_get_id(service) or "").rstrip("/")
    if not sid:
        return False

    # Strong signal: explicit Image API profile
    for p in _service_profiles(service):
        if "iiif.io/api/image" in p:
            return True

    # Sometimes type hints exist (v2: "ImageService2", v3: "ImageService3", etc.)
    t = (service.get("type") or service.get("@type") or "")
    if isinstance(t, str) and "imageservice" in t.lower():
        return True

    # Heuristic fallback: many image services include /iiif/ in base URL
    if "/iiif" in sid.lower():
        return True

    # If no evidence, still allow — but prefer ones that look plausible
    return True

def _extract_image_service_ids(*candidates: Any) -> List[str]:
    """
    Extract plausible Image API service ids from various candidate nodes.
    Keeps order, de-duplicates.
    """
    out: List[str] = []
    seen: Set[str] = set()

    def add(sid: Optional[str]) -> None:
        if not sid:
            return
        sid = sid.rstrip("/")
        if sid not in seen:
            seen.add(sid)
            out.append(sid)

    for c in candidates:
        for s in _iter_services(c):
            if _looks_like_image_service(s):
                add(_get_id(s))
            # v3 may nest again (rare but exists)
            for ss in _iter_services(s):
                if _looks_like_image_service(ss):
                    add(_get_id(ss))

    return out

# -----------------------------
# Robust IIIF Image URL builder (stable resizing)
# -----------------------------

def mk_iiif_image_url(service_id: str, max_width: Optional[int] = 2000, fmt: str = "jpg", quality: str = "default") -> str:
    sid = service_id.rstrip("/")
    fmt = fmt.lstrip(".")
    quality = quality or "default"
    if max_width is None:
        return f"{sid}/full/full/0/{quality}.{fmt}"
    return f"{sid}/full/{int(max_width)},/0/{quality}.{fmt}"

# -----------------------------
# Manifest -> image URLs (v2 + v3)
# -----------------------------
def iiif_manifest_to_pages(
    manifest: Dict[str, Any],
    max_width: Optional[int] = 2000,
    fmt: str = "jpg",
    quality: str = "default",
    include_direct_ids_as_fallback: bool = True,
) -> List[Tuple[str, Dict[str, Any]]]:
    pages: List[Tuple[str, Dict[str, Any]]] = []
    seen: Set[str] = set()

    def add(u: Optional[str], canvas: Dict[str, Any]) -> None:
        if not u or u in seen:
            return
        seen.add(u)
        pages.append((u, canvas))

    # v3
    if isinstance(manifest.get("items"), list):
        for canvas in manifest.get("items", []):
            if not isinstance(canvas, dict):
                continue
            # typical v3: canvas.items -> annotation pages -> items -> body
            for anno_page in canvas.get("items", []):
                if not isinstance(anno_page, dict):
                    continue
                for anno in anno_page.get("items", []):
                    if not isinstance(anno, dict):
                        continue
                    for body in _as_list(anno.get("body")):
                        sids = _extract_image_service_ids(body)
                        if sids:
                            for sid in sids:
                                add(mk_iiif_image_url(sid, max_width=max_width, fmt=fmt, quality=quality), canvas)
                        elif include_direct_ids_as_fallback and isinstance(body, dict):
                            add(body.get("id") or body.get("@id"), canvas)
        return pages

    # v2
    seqs = manifest.get("sequences", [])
    if isinstance(seqs, list) and seqs:
        seq0 = seqs[0] if isinstance(seqs[0], dict) else {}
        for canvas in _as_list(seq0.get("canvases")):
            if not isinstance(canvas, dict):
                continue
            for img in _as_list(canvas.get("images")):
                if not isinstance(img, dict):
                    continue
                res = img.get("resource") or {}
                if not isinstance(res, dict):
                    res = {}
                sids = _extract_image_service_ids(res)
                if sids:
                    for sid in sids:
                        add(mk_iiif_image_url(sid, max_width=max_width, fmt=fmt, quality=quality), canvas)
                elif include_direct_ids_as_fallback:
                    add(res.get("@id") or res.get("id"), canvas)

    return pages

def iiif_manifest_to_image_urls(
    manifest: Dict[str, Any],
    max_width: Optional[int] = 2000,
    fmt: str = "jpg",
    quality: str = "default",
    include_direct_ids_as_fallback: bool = True,
) -> List[str]:
    return [
        url for (url, _canvas) in iiif_manifest_to_pages(
            manifest,
            max_width=max_width,
            fmt=fmt,
            quality=quality,
            include_direct_ids_as_fallback=include_direct_ids_as_fallback,
        )
    ]

def _force_full_url(iiif_url: str, fmt: str, quality: str) -> str:
    if "/full/" not in iiif_url:
        return iiif_url
    base = iiif_url.split("/full/")[0].rstrip("/")
    return f"{base}/full/full/0/{quality}.{fmt}"

def fetch_pil_image(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    backoff: float = 1.5,
    iiif_format: str = "jpg",
    iiif_quality: str = "default",
):
    last_exc = None
    strict_tried = False
    iiif_format = iiif_format.lstrip(".")
    iiif_quality = iiif_quality or "default"

    host = urlparse(url).netloc
    if host in _NO_UPSCALE_HOSTS and "/full/" in url:
        url = _force_full_url(url, fmt=iiif_format, quality=iiif_quality)

    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers=APP_USER)
            r.raise_for_status()
            # return Image.open(BytesIO(r.content))
            with Image.open(BytesIO(r.content)) as img:
                return img.convert("RGB").copy()
            
        except Exception as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)

            if status == 403 and (not strict_tried) and "/full/" in url:
                _NO_UPSCALE_HOSTS.add(host)                
                url = _force_full_url(url, fmt=iiif_format, quality=iiif_quality)
                strict_tried = True
                logger.warning(f"{host} does not support scaling, using full images for this host")
                continue

            if status in (502, 503, 504) and attempt < retries:
                time.sleep(backoff ** attempt)
                continue

            logger.error(f"Fetching image failed for {url}: {e}")
            return None

    logger.error(f"Fetching image failed for {url}: {last_exc}")
    return None

def stream_download_to_tempfile(
    url: str,
    suffix: str,
    timeout: int = 120,
    retries: int = 3,
    backoff: float = 1.5,
    chunk_size: int = 1024 * 1024,
) -> str:
    logger.info(f"Downloading {url}")

    last_exc = None
    for attempt in range(retries + 1):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name

            with requests.get(url, stream=True, timeout=timeout, headers=APP_USER) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)

            return tmp_path

        except Exception as e:
            last_exc = e

            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

            status = getattr(getattr(e, "response", None), "status_code", None)

            transient_status = status in (429, 502, 503, 504)
            transient_exc = isinstance(e, (requests.Timeout, requests.ConnectionError))

            if (transient_status or transient_exc) and attempt < retries:
                sleep_s = backoff ** attempt
                logger.warning(f"Download failed ({e}); retrying in {sleep_s:.1f}s: {url}")
                time.sleep(sleep_s)
                continue

            logger.error(f"Download failed permanently: {url}: {e}")
            raise

    raise last_exc

@lru_cache(maxsize=1)
def _get_pdf_libs():
    PdfReader = ensure_import("pypdf", attr="PdfReader")
    pdfium = ensure_import("pypdfium2")
    return PdfReader, pdfium

@lru_cache(maxsize=1)
def _get_PyMuPDF():
    fitz = ensure_import("pymupdf")
    return fitz

import math

def downscale_if_needed(im: Image.Image, max_pixels: int= 10_000_000) -> Image.Image:
    w, h = im.size
    px = w * h
    if px <= max_pixels:
        return im
    factor = math.sqrt(max_pixels / px)
    new_size = (max(1, int(w * factor)), max(1, int(h * factor)))
    return im.resize(new_size)

def iter_pages(
    input: str,
    *,
    iiif_max_width: Optional[int] = 2000,
    iiif_format: str = "jpg",
    pdf_dpi: int = 200,
    pdf_text_policy: PdfTextPolicy = PdfTextPolicy(),
    iiif_ocr_policy: IiifOcrPolicy = IiifOcrPolicy(),
    timeout: int = 30,
    file_formats: list | None = None,
    start_page: int = 1,
    doc_id: str = "n/a"
) -> Iterator[PageItem]:
    if not start_page or int(start_page)<=0:
        start_page = 1
    src_kind, src_path = resolve_source(input)

    if src_kind == "file":
        kind = detect_file_kind(src_path)
    else:
        kind = detect_url_kind(input, timeout=timeout)

    def _log_and_yield(page: PageItem, total:int=1):
        if page.kind=="text":
            preview = " ".join((page.data or "").split())[:60]
            logger.debug(f"\n{doc_id}: {page.index}/{total}: {preview}")
        return page

    if file_formats and not kind in file_formats:
        logger.warning(f"{doc_id}: File {input} skipped as {kind} not in {file_formats}")
        return
    else:
        logger.info(f"{doc_id}: Processing {str(kind).upper()} {src_kind} --> {input}")

    if kind in ("json", "iiif"):    
        if src_kind == "file":
            manifest = json.loads(src_path.read_text(encoding="utf-8"))
        else:
            manifest = requests.get(input, timeout=timeout, headers=APP_USER).json()

        pages = iiif_manifest_to_pages(
            manifest,
            max_width=iiif_max_width,
            fmt=iiif_format,
        )

        if not pages:
            logger.warning(f"IIIF manifest <{manifest}> has no image canvases")
            return
        
        logger.info(f"{doc_id}: Found {len(pages)} pages in IIIF, starting at {start_page}")
        pages = pages[(start_page - 1):]
        logger.debug(f"IIIF Policy: {iiif_ocr_policy}")
        try:
            for i, (img_url, canvas) in enumerate(pages, start=start_page):
                hit = find_ocr(canvas, iiif_ocr_policy)
                if hit:                
                    ocr_url, rule, profile = hit
                    logger.debug(f"{doc_id}: Found IIIF OCR text in {ocr_url}")
                    try:
                        r = requests.get(ocr_url, timeout=getattr(iiif_ocr_policy, "timeout", timeout), headers=APP_USER)
                        r.raise_for_status()
                        txt = ocr_bytes_to_text(r.content, rule)
                        logger.debug(f"{doc_id}: Seeing IIIF OCR text in {ocr_url}: {txt}")
                        if is_usable_text(txt, iiif_ocr_policy, log_label="OCR text"):
                            logger.info(f"{doc_id}: [{i}/{len(pages)}]: Using IIIF OCR text from {ocr_url}")
                            aPage = PageItem(i, "text", txt, source=f"ocr:{ocr_url}", meta={
                                "canvas": canvas.get("@id") or canvas.get("id"),
                                "profile": profile,
                                "key": rule.key,
                                "xpath": rule.xpath,
                            }, total=len(pages))
                            yield _log_and_yield(aPage,len(pages))
                            continue
                    except Exception as e:
                        logger.warning(f"{doc_id}: OCR fetch/parse failed for page {i} ({ocr_url}): {e}")

                img = fetch_pil_image(img_url, timeout=timeout, iiif_format=iiif_format, iiif_quality="default")
                if img is None:
                    logger.warning(f"{doc_id}: Skipping page {i}: could not fetch image {img_url}")
                    continue
                aPage = PageItem(i, "image", img, source=f"iiif:{img_url}", meta={
                    "canvas": canvas.get("@id") or canvas.get("id"),
                }, total=len(pages))
                yield _log_and_yield(aPage,len(pages))
        except Exception as e:
            logger.error(f"{doc_id}: Error reading IIIF {input}: {e}")
        return

    if kind == "pdf":
        pdf_path = None
        doc = None
        try:
            if src_kind == "file":
                pdf_path = str(src_path)
            else:
                try:
                    pdf_path = stream_download_to_tempfile(input, suffix=".pdf")
                except Exception as e:
                    logger.error(f"{doc_id}: Error downloading PDF {input}: {e}")
                    return
            # from pypdf import PdfReader
            # import pypdfium2 as pdfium
            # PdfReader, pdfium = _get_pdf_libs()
            pymupdf = _get_PyMuPDF()
            doc = pymupdf.open(pdf_path)
            scale = pdf_dpi / 72
            mat = pymupdf.Matrix(scale, scale)
            # reader = PdfReader(pdf_path)
            # doc = pdfium.PdfDocument(pdf_path)
            # pages=reader.pages
            total = doc.page_count
            logger.info(f"{doc_id}: Found {total} pages in PDF, starting at {start_page}")
            # pages = pages[(start_page - 1):]
            for i in range(start_page, total + 1):
                page = doc.load_page(i-1)
                # txt = page.extract_text() or ""
                txt = page.get_text("text") or ""
                if is_usable_text(txt, pdf_text_policy, log_label="PDF text"):
                    logger.info(f"{doc_id}: [{i}/{total}]: Using PDF text {input}")
                    aPage = PageItem(i, "text", txt, source=f"pdf-text:{input}#page={i}", total=total)
                    yield _log_and_yield(aPage,total)
                else:
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    # pil = doc[i-1].render(scale=pdf_dpi/72).to_pil()
                    pil = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    # pil = downscale_if_needed(pil)
                    aPage = PageItem(i, "image", pil, source=f"pdf-image:{input}#page={i}", total=total)
                    yield _log_and_yield(aPage,total)
                    del pil, pix, page
            
        except Exception as e:
            logger.error(f"{doc_id}: Error reading PDF {input}: {e}")

        finally:
            if doc:
                doc.close()
            if pdf_path and src_kind != "file":
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
        return
    
    if kind in ("text", "html", "xml"): # TODO XML parsing
        try:
            if src_kind == "file":
                raw = src_path.read_text(encoding="utf-8")
            else:
                r = requests.get(input, timeout=timeout, headers=APP_USER)
                r.raise_for_status()
                if not r.encoding:
                    r.encoding = "utf-8"
                raw = r.text
            aPage = PageItem(1, "text", raw, source=f"{kind}:{input}", total=1)
            yield _log_and_yield(aPage,1)
        except Exception as e:
            logger.error(f"{doc_id}: Reading {kind.upper()} {input}: {e}")
        return
    
    logger.error(f"{doc_id}: Unknown URL type: {kind.upper()} {input}")
    # raise ValueError("Unknown URL type.")


import numpy as np

def ink_ratio(pil_img):
    g = pil_img.convert("L")
    a = np.asarray(g)
    bg = np.median(a)
    thr = bg - 25
    ink = (a < thr).mean()
    return float(ink), float(bg), float(thr)

def kraken_image_to_text_legacy(
    im: Image.Image,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    model_name: str | None = None,
    segmenter: str | None = None,
    binarize: bool = False,
    ink_ratio_range: list | None = None # [0, 1]
) -> str:
    try:
        if ink_ratio_range:
            r, bg, thr = ink_ratio(im)
            logger.debug(f"Found page (blank), r={r:.5f}, bg={bg:.1f}, thr={thr:.1f}")
            if r < ink_ratio_range[0]:
                logger.warning(
                    f"Skipping page (blank), r={r:.5f}, bg={bg:.1f}, thr={thr:.1f}"
                )
                return ""

            if r > ink_ratio_range[1]:
                logger.warning(
                    f"Skipping page (too dark/ornament), r={r:.3f}, bg={bg:.1f}"
                )
                return ""
            
        ensure_import("kraken")
        try:
            from kraken import binarization, blla, rpred #, pageseg
            from kraken.lib import models
            import warnings

            warnings.filterwarnings(
                "ignore",
                message="Using legacy polygon extractor",
                module="kraken.rpred",
            )
        except Exception:
            logger.exception("Kraken import failed")
            return ""
        

        cfg_path = resolve_config_path(config_path)
        dom = resolve_domain(config_path=cfg_path, domain=domain)
        recog_name = resolve_recognition_model_name(
            config_path=cfg_path,
            domain=dom,
            model_name=model_name,
        )
        seg_name = resolve_segmentation_name(config_path=cfg_path, segmenter=segmenter)
        seg_key = (str(cfg_path), str(seg_name))
        seg_model = _KRAKEN_SEG.get(seg_key)
        if seg_model is None:
            seg_model = load_segmentation_model(config_path=cfg_path, segmenter=segmenter)
            _KRAKEN_SEG[seg_key] = seg_model

        # seg_model = load_segmentation_model(config_path=cfg_path, segmenter=segmenter)
        logger.debug(f"Kraken page recognition with {recog_name}...")
        work = binarization.nlbin(im) if binarize else im
        bounds = blla.segment(work, model=seg_model)
        # seg = pageseg.segment(work)
        model_path = str(resolve_kraken_model_path(config_path=cfg_path, model_name=recog_name))
        net_key = (str(cfg_path), model_path)
        net = _KRAKEN_NET.get(net_key)
        if net is None:
            net = models.load_any(model_path)
            _KRAKEN_NET[net_key] = net
        # net = models.load_any(model_path)

        preds = rpred.rpred(network=net, im=work, bounds=bounds)
        ocr_page = "\n".join(p.prediction for p in preds)

        logger.debug(ocr_page)

        return ocr_page
    except Exception:
        logger.exception("Kraken OCR failed")
        return ""

def kraken_image_to_text(
    im: Image.Image,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    model_name: str | None = None,
    segmenter: str | None = None,
    binarize: bool = False,
    ink_ratio_range: list | None = None,  # [0, 1]
    seg_blur_radius: float | None = None,
) -> str:
    try:
        if ink_ratio_range:
            r, bg, thr = ink_ratio(im)
            logger.debug(f"Found page (blank), r={r:.5f}, bg={bg:.1f}, thr={thr:.1f}")
            if r < ink_ratio_range[0]:
                logger.warning(
                    f"Skipping page (blank), r={r:.5f}, bg={bg:.1f}, thr={thr:.1f}"
                )
                return ""

            if r > ink_ratio_range[1]:
                logger.warning(
                    f"Skipping page (too dark/ornament), r={r:.3f}, bg={bg:.1f}"
                )
                return ""

        ensure_import("kraken")
        try:
            from kraken import binarization, blla, rpred
            from kraken.lib import models
            from PIL import ImageFilter
            import warnings

            warnings.filterwarnings(
                "ignore",
                message="Using legacy polygon extractor",
                module="kraken.rpred",
            )
        except Exception:
            logger.exception("Kraken import failed")
            return ""

        cfg_path = resolve_config_path(config_path)
        dom = resolve_domain(config_path=cfg_path, domain=domain)
        recog_name = resolve_recognition_model_name(
            config_path=cfg_path,
            domain=dom,
            model_name=model_name,
        )
        seg_name = resolve_segmentation_name(config_path=cfg_path, segmenter=segmenter)

        seg_key = (str(cfg_path), str(seg_name))
        seg_model = _KRAKEN_SEG.get(seg_key)
        if seg_model is None:
            seg_model = load_segmentation_model(config_path=cfg_path, segmenter=segmenter)
            _KRAKEN_SEG[seg_key] = seg_model

        logger.debug(f"Kraken page recognition with {recog_name}...")

        work = binarization.nlbin(im) if binarize else im

        seg_work = work.copy()

        if seg_blur_radius and seg_blur_radius > 0:
            seg_work = seg_work.filter(ImageFilter.GaussianBlur(radius=seg_blur_radius))
            logger.info(f"Applied Gaussian blur to segmentation image: radius={seg_blur_radius}")

        bounds = blla.segment(seg_work, model=seg_model)

        model_path = str(resolve_kraken_model_path(config_path=cfg_path, model_name=recog_name))
        net_key = (str(cfg_path), model_path)
        net = _KRAKEN_NET.get(net_key)
        if net is None:
            net = models.load_any(model_path)
            _KRAKEN_NET[net_key] = net

        preds = rpred.rpred(network=net, im=work, bounds=bounds)
        ocr_page = "\n".join(p.prediction for p in preds)

        logger.debug(ocr_page)
        return ocr_page

    except Exception:
        logger.exception("Kraken OCR failed")
        return ""
    

def page_to_text(
    item: PageItem,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    model_name: str | None = None,
    segmenter: str | None = None,
    binarize: bool = True,
    framework: Literal["kraken", "tesseract"] = "kraken",
    tesseract_lang: str | None = None,
    tesseract_config: str | None = None,
    seg_blur_radius: float | None = None,
) -> str:
    if item.kind == "text":
        return item.data or ""

    logger.debug(f"processing image {item.index} of {item.source} in framework {framework.upper()}")
    if item.data is None:
        logger.error(f"page_to_text got None image: page={item.index} source={item.source}")
        return ""

    if framework == "tesseract":
        return tesseract_image_to_text(
            item.data,
            config_path=config_path,
            domain=domain,
            lang=tesseract_lang,
            config=tesseract_config,
            binarize=binarize,
        )
    
    elif framework == "kraken":
        return kraken_image_to_text(
            item.data,
            config_path=config_path,
            domain=domain,
            model_name=model_name,
            segmenter=segmenter,
            binarize=binarize,
            seg_blur_radius=seg_blur_radius
        )
      
    elif framework == "transformer":
        from .medieval_ocr_pipeline.complete_ocr_pipeline import process_complete_image, setup_models  
        MODELS = setup_models()
        res = process_complete_image(item.data, verbose=False, cleanup_temp=True, models=MODELS)
        return "" if res is None else res[0]
        
    else:
        return ""

def iter_text_pages(
    input: str,
    *,
    doc_id: str | None = None,
    iter_kwargs: Dict[str, Any],
    page_to_text_kwargs: Dict[str, Any],
    text_image_file_kwargs: Optional[Dict[str, Any]] = None,
    framework: Literal["kraken", "tesseract", "transformer", "none"] = "kraken",
) -> Iterator[Tuple[int, str]]:
    
    iter_kwargs = dict(iter_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    logger.debug(
        f"iter_text_pages received: {[iter_kwargs, page_to_text_kwargs, text_image_file_kwargs, framework]}"
    )
    cfg = text_image_file_kwargs or {}
    use_transformer = framework == "transformer"
    no_ocr = framework == "none"

    
    if use_transformer:
        try:
            logger.info("### Using Tranformer from medieval_ocr_pipeline ###")
            here = Path(__file__).resolve().parent
            requirements = here / "medieval_ocr_pipeline" / "requirements.txt"
            ensure_import("transformers", requirements=requirements)
            try:
                from transformers import logging as hf_logging
                hf_logging.set_verbosity_error()
            except Exception:
                logger.warning("Could not deactivate transformer logging")
            ensure_import("torch", requirements=requirements)
            # from .medieval_ocr_pipeline.complete_ocr_pipeline import process_complete_image, setup_models  
            # MODELS = setup_models()
            # src\zotero_rdf_server\plugins\fts\medieval_ocr_pipeline\complete_ocr_pipeline.py
        except Exception as e:
            logger.exception("Transformer plugin import failed")
            use_transformer = False

    try:
        from zotero_rdf_server.config import EXPORT_DIRECTORY

        EXPORT_DIRECTORY = Path(EXPORT_DIRECTORY)
    except Exception:
        EXPORT_DIRECTORY = Path().resolve()

    img_out: Optional[str] = cfg.get("img_out", "images")
    txt_out: Optional[str] = cfg.get("txt_out", "texts")
    img_ext: str = cfg.get("img_ext", "jpg")
    txt_ext: str = cfg.get("txt_ext", "txt")

    save_text: str = cfg.get("save_text", "skip")  # "skip" | "overwrite" | "active"
    save_image: str = cfg.get("save_image", "skip")  # "skip" | "overwrite" | "active"
    on_error: str = cfg.get("on_error", "log")  # "raise" | "skip" | "empty" | "log"

    if save_text not in {"skip", "overwrite", "active"}:
        raise ValueError(f"save_text must be 'active', 'skip' or 'overwrite', got {save_text!r}")
    if save_image not in {"skip", "overwrite", "active"}:
        raise ValueError(f"save_image must be 'active', 'skip' or 'overwrite', got {save_image!r}")
    if on_error not in {"raise", "skip", "empty", "log"}:
        raise ValueError(f"on_error must be 'raise', 'skip', 'empty' or 'log', got {on_error!r}")

    _doc_id = safe_doc_id(doc_id or input)
    iter_kwargs['doc_id']=_doc_id

    def _resolve_out(p: Optional[str]) -> Optional[Path]:
        if not p:
            return None
        pp = Path(p)
        if pp.is_absolute():
            logger.error(f"Absolute paths are not allowed: {pp}")
            return (EXPORT_DIRECTORY / _doc_id).resolve()
        result_path = (EXPORT_DIRECTORY / pp / _doc_id).resolve()
        logger.info(f"Export path set: {result_path}")
        return result_path

    img_dir = _resolve_out(img_out)
    txt_dir = _resolve_out(txt_out)

    def _save_pil(im, path: Path) -> None:
        logger.debug(f"Stored file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)

    def _save_text(txt: str, path: Path) -> None:
        logger.debug(f"Stored file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(txt, encoding="utf-8")

    def _text_path(page_no: int) -> Optional[Path]:
        return None if txt_dir is None else (txt_dir / f"{page_no:04d}.{txt_ext}")

    def _image_path(page_no: int) -> Optional[Path]:
        return None if img_dir is None else (img_dir / f"{page_no:04d}.{img_ext}")

    def _parse_page_no(path: Path) -> Optional[int]:
        try:
            return int(path.stem)
        except Exception:
            return None

    def _cached_pages() -> dict[str, list[int]]:
        pages = {"text": [], "image": []}

        if txt_dir and txt_dir.exists():
            for p in txt_dir.glob(f"*.{txt_ext}"):
                n = _parse_page_no(p)
                if n is not None:
                    pages["text"].append(n)

        if img_dir and img_dir.exists():
            for p in img_dir.glob(f"*.{img_ext}"):
                n = _parse_page_no(p)
                if n is not None:
                    pages["image"].append(n)

        pages["text"].sort()
        pages["image"].sort()
        return pages

    def _iter_cached_image_pages(img_dir: Path, img_ext: str) -> Iterator[Tuple[int, Path]]:
        files = sorted(img_dir.glob(f"*.{img_ext}"))
        for f in files:
            n = _parse_page_no(f)
            if n is not None:
                yield n, f

    def _maybe_store_text(page_no: int, txt: str) -> None:
        if save_text not in {"active", "overwrite"}:
            return
        tp = _text_path(page_no)
        if tp is None:
            return
        if save_text == "overwrite" or not tp.exists():
            _save_text(txt, tp)

    def _yield_from_cache() -> Iterator[Tuple[int, str]]:
        if txt_dir is None or not txt_dir.exists():
            return iter(())
        page_nos = sorted(
            n for n in (_parse_page_no(p) for p in txt_dir.glob(f"*.{txt_ext}")) if n is not None
        )

        def _it() -> Iterator[Tuple[int, str]]:
            for page_no in page_nos:
                tp = _text_path(page_no)
                if tp is None or not tp.exists():
                    continue
                yield page_no, tp.read_text(encoding="utf-8")

        return _it()

    def _write_lock_dir() -> Optional[Path]:
        base = txt_dir or img_dir
        return None if base is None else (base / ".write.lock")

    def _write_lock_create() -> bool:
        ld = _write_lock_dir()
        if ld is None:
            return False
        ld.parent.mkdir(parents=True, exist_ok=True)
        try:
            ld.mkdir()
            logger.info(f"Lock acquired: {ld}")
            return True
        except FileExistsError:
            return False

    def _write_lock_remove_best_effort() -> None:
        ld = _write_lock_dir()
        if ld is None or not ld.exists():
            return
        try:
            for p in ld.glob("*"):
                try:
                    if p.is_file():
                        p.unlink()
                except Exception:
                    pass
            ld.rmdir()
            logger.info(f"Lock removed: {ld}")
        except Exception:
            pass

    def _log_and_yield(page_no: int, txt: str, total: int=1, cached:bool=False):
        preview = (
            (t[:60] + "..." if len(t) > 60 else t)
            if (t := " ".join((txt or "").split()))
            else "[no text]"
        )
        logger.info(f"\n{_doc_id} {page_no}/{total}: {'CACHED' if cached else framework.upper()} result: {preview}")
        return page_no, txt
    
    cached_page_set = _cached_pages()

    logger.info(f"{_doc_id}: Found {len(set(cached_page_set['text']))} text files and {len(set(cached_page_set['image']))} image files")

    if no_ocr:
        logger.info(f"{_doc_id}: framework='none' -> cache only")
        if txt_dir is not None and any(txt_dir.glob(f"*.{txt_ext}")):
            logger.info(f"{_doc_id}: Using {len(set(cached_page_set['text']))} cached text files in {txt_dir}")
            yield from _yield_from_cache()
        else:
            logger.info(f"{_doc_id}: No cached text files found")
        return

    # If text file found and not overwrite, use as result and skip download + OCR
    if (
        save_text == "active"
        and save_image == "skip"
        and txt_dir is not None
        and any(txt_dir.glob(f"*.{txt_ext}"))
    ):      
        logger.warning(f"{_doc_id}: Using {len(set(cached_page_set['text']))} text files in {txt_dir}")
        yield from _yield_from_cache()
        return    
    
    # writing_enabled = (
    #     (save_text in {"active", "overwrite"} and txt_dir is not None) or
    #     (save_image in {"active", "overwrite"} and img_dir is not None)
    # )

    # lock_acquired = False
    # if writing_enabled:
    #     lock_acquired = _write_lock_create()
    #     if lock_acquired:
    #         logger.debug(f"Write lock set: {_write_lock_dir()}")
    #     else:
    #         logger.info(f"{_doc_id}: Write lock already present: {_write_lock_dir()} (another run may be writing or crashed)")
    # TODO call _write_lock_remove_best_effort in finally

    # If image file found and not overwrite, use as result and skip download but proceed with OCR
    if save_image == "active" and img_dir is not None and img_dir.exists():
        cached_imgs = list(_iter_cached_image_pages(img_dir, img_ext))
        total = len(cached_imgs)
        if cached_imgs:
            logger.warning(f"{_doc_id}: Using {len(set(cached_page_set['image']))} image files in {img_dir}; no remote download")
            for page_no, img_path in cached_imgs:
                tp = _text_path(page_no)
                if save_text == "active" and save_text != "overwrite" and tp and tp.exists():
                    yield _log_and_yield(page_no, tp.read_text(encoding="utf-8"), total, True)
                    # yield page_no, tp.read_text(encoding="utf-8")
                    continue

                try:
                    with Image.open(img_path) as im:
                        pil = im.copy()
                    item = PageItem(page_no, "image", pil, source=f"cache-image:{img_path}",total=total) 
                    txt = page_to_text(
                        item,
                        framework=framework,
                        **page_to_text_kwargs
                    )
                except Exception as e:
                    logger.error(f"{_doc_id}: Failed to load cached image for page {page_no} from {img_path}: {e}")

                    if on_error == "raise":
                        raise
                    if on_error == "skip":
                        continue
                    txt = "" # DEBUG

                if save_text in {"active", "overwrite"}:
                    _maybe_store_text(page_no, txt)
                yield _log_and_yield(page_no, txt, total)
            return
        
    for item in iter_pages(input=input, **iter_kwargs):
        page_no = getattr(item, "sequence", None) or getattr(item, "index", None)
        total = getattr(item, "total", 1)
        if page_no is None:
            raise AttributeError("PageItem has neither .sequence nor .index")

        if save_image == "active":
            ip = _image_path(page_no)
            if ip is not None and ip.exists():
                try:
                    with Image.open(ip) as im:
                        item.data = im.copy()
                    item.kind = getattr(item, "kind", "image")
                except Exception as e:
                    logger.error(f"{_doc_id}: Failed to load cached image for page {page_no} from {str(ip)}: {e}")
                    if on_error == "raise":
                        raise
                    if on_error == "skip":
                        continue

        tp = _text_path(page_no)

        # Save Image
        if item.kind == "image" and save_image in {"active", "overwrite"} and img_dir is not None:
            ip = _image_path(page_no)
            if ip is not None and item.data is not None and hasattr(item.data, "save"):
                if save_image == "overwrite" or not ip.exists():
                    try:
                        _save_pil(item.data, ip)
                    except Exception as e:
                        logger.error(f"{_doc_id}: Failed to store image page {page_no} to {str(ip)}: {e}")
                        if on_error == "raise":
                            raise
                        if on_error == "skip":
                            continue

        # If Cached Text return
        if save_text == "active" and tp is not None and tp.exists():
            try:
                txt = tp.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"{_doc_id}: Failed to load cached text for page {page_no} from {str(tp)}: {e}")
                if on_error == "raise":
                    raise
                if on_error == "skip":
                    continue
                txt = ""
            yield _log_and_yield(page_no, txt, total, True)
            continue

        # OCR
        try:
            txt = page_to_text(
                item,
                framework=framework,
                **page_to_text_kwargs
            )
        except Exception as e:
            logger.error(f"{_doc_id}: iter_text_pages error on page {page_no}: {e}")
            if on_error == "raise":
                raise
            if on_error == "skip":
                continue
            txt = ""

        if save_text in {"active", "overwrite"} and tp is not None:
            if save_text == "overwrite" or not tp.exists():
                try:
                    _save_text(txt, tp)
                except Exception as e:
                    logger.error(f"{_doc_id}: Failed to store text page {page_no} to {str(tp)}: {e}")
                    if on_error == "raise":
                        raise
                    if on_error == "skip":
                        continue

        yield _log_and_yield(page_no, txt, total)    

    # for item in iter_pages(input=input, **iter_kwargs):
    #     page_no = getattr(item, "sequence", None) or getattr(item, "index", None)
    #     total = getattr(item, "total", 1)
    #     if page_no is None:
    #         raise AttributeError("PageItem has neither .sequence nor .index")

    #     # Image: if active and cached -> load from file and set in item.data
    #     if save_image == "active":
    #         ip = _image_path(page_no)
    #         if ip is not None and ip.exists():
    #             try:
    #                 with Image.open(ip) as im:
    #                     item.data = im.copy()
    #                 item.kind = getattr(item, "kind", "image")
    #             except Exception as e:
    #                 logger.error(f"{_doc_id}: Failed to load cached image for page {page_no} from {str(ip)}: {e}")

    #                 if on_error == "raise":
    #                     raise
    #                 if on_error == "skip":
    #                     continue
    #                 # empty/log -> weiter, dann ggf. OCR/remote

    #     # Text: if active and cached -> read directly, no OCR
    #     tp = _text_path(page_no)
    #     if save_text == "active" and tp is not None and tp.exists():
    #         if item.kind == "image" and save_image in {"active", "overwrite"} and img_dir is not None:
    #             ip = _image_path(page_no)
    #             if ip is not None and item.data is not None and hasattr(item.data, "save"):
    #                 if save_image == "overwrite" or not ip.exists():
    #                     try:
    #                         _save_pil(item.data, ip)
    #                     except Exception as e:
    #                         logger.error(
    #                             f"{_doc_id}: Failed to store image page {page_no} to {str(ip)}: {e}"
    #                         )
    #                         if on_error == "raise":
    #                             raise
    #                         if on_error == "skip":
    #                             continue

    #         try:
    #             txt = tp.read_text(encoding="utf-8")
    #         except Exception as e:
    #             logger.error(f"{_doc_id}: Failed to load cached text for page {page_no} from {str(tp)}: {e}")
    #             if on_error == "raise":
    #                 raise
    #             if on_error == "skip":
    #                 continue
    #             txt = ""

    #         yield _log_and_yield(page_no, txt, total)
    #         continue

    #     # Save image (active/overwrite)
    #     if item.kind == "image" and save_image in {"active", "overwrite"} and img_dir is not None:
    #         ip = _image_path(page_no)
    #         if ip is not None and item.data is not None and hasattr(item.data, "save"):
    #             if save_image == "overwrite" or not ip.exists():
    #                 try:
    #                     _save_pil(item.data, ip)
    #                 except Exception as e:
    #                     logger.error(f"{_doc_id}: Failed to store image page {page_no} to {str(ip)}: {e}")
    #                     if on_error == "raise":
    #                         raise
    #                     if on_error == "skip":
    #                         continue

    #     # OCR / page_to_text
    #     try:
    #         txt = page_to_text(
    #             item,
    #             framework=framework,
    #             **page_to_text_kwargs
    #         )

    #     except Exception as e:
    #         logger.error(f"{_doc_id}: iter_text_pages error on page {page_no}: {e}")
    #         if on_error == "raise":
    #             raise
    #         if on_error == "skip":
    #             continue
    #         txt = ""  # empty/log

    #     # Save text (active/overwrite)
    #     if save_text in {"active", "overwrite"} and tp is not None:
    #         if save_text == "overwrite" or not tp.exists():
    #             try:
    #                 _save_text(txt, tp)
    #             except Exception as e:
    #                 logger.error(f"{_doc_id}: Failed to store text page {page_no} to {str(tp)}: {e}")
    #                 if on_error == "raise":
    #                     raise
    #                 if on_error == "skip":
    #                     continue

    #     yield _log_and_yield(page_no, txt, total)

# end