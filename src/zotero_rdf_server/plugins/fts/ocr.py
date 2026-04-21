from dataclasses import dataclass, field
from typing import Iterator, Optional, Dict, Any, List, Literal, Union, Tuple, Mapping, Iterable, Callable, Set, Sequence, Protocol
import io, json, os, tempfile, time, re, math, requests, csv
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
class ReplaceRule:
    pattern: str
    repl: str = ""

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ReplaceRule":
        return cls(
            pattern=str(data.get("pattern", "")),
            repl=str(data.get("repl", "")),
        )

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class JsonPolicy:
    enabled: bool = True
    encoding: str = "utf-8"

    page_path: str | None = None   # "items"
    text_path: str | None = None   # "content" or "body.text"

    fields: tuple[str, ...] = ()

    replace_rules: tuple[ReplaceRule, ...] = ()
    skip_empty: bool = True

    fallback_to_full_document: bool = True

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "JsonPolicy":
        if not data:
            return cls()

        try:
            raw_fields = data.get("fields", ())
            if isinstance(raw_fields, str):
                fields = (raw_fields,)
            elif isinstance(raw_fields, Sequence):
                fields = tuple(str(f) for f in raw_fields if f is not None)
            else:
                fields = ()

            raw_rules = data.get("replace_rules", ())
            if isinstance(raw_rules, Sequence) and not isinstance(raw_rules, (str, bytes)):
                replace_rules = tuple(
                    ReplaceRule.from_json(r) for r in raw_rules if isinstance(r, Mapping)
                )
            else:
                replace_rules = ()

            return cls(
                enabled=bool(data.get("enabled", True)),
                encoding=str(data.get("encoding", "utf-8") or "utf-8"),
                page_path=str(data.get("page_path")) if data.get("page_path") else None,
                text_path=str(data.get("text_path")) if data.get("text_path") else None,
                fields=fields,
                replace_rules=replace_rules,
                skip_empty=bool(data.get("skip_empty", True)),
                fallback_to_full_document=bool(data.get("fallback_to_full_document", True)),
            )
        except (TypeError, ValueError):
            return cls()



def _json_get_path(obj: Any, path: str | None) -> Any:
    if not path:
        return obj

    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                idx = int(part)
                cur = cur[idx]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur

def _json_extract_text(item: Any, policy: JsonPolicy) -> str:
    parts = []

    if policy.fields:
        for f in policy.fields:
            val = _json_get_path(item, f)
            if val is not None:
                parts.append(str(val))
    elif policy.text_path:
        val = _json_get_path(item, policy.text_path)
        if val is not None:
            parts.append(str(val))
    else:
        parts.append(str(item))

    txt = "\n".join(parts)
    txt = _apply_replace_rules(txt, policy.replace_rules)
    return _normalize_text(txt)


@dataclass(frozen=True)
class TextPolicy:
    enabled: bool = True
    encoding: str = "utf-8"
    split_regex: str | None = None
    keep_delimiters: bool = False
    flags: int = 0
    replace_rules: tuple[ReplaceRule, ...] = ()
    skip_empty: bool = True

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "TextPolicy":
        if not data:
            return cls()
        try:
            raw_rules = data.get("replace_rules", ())
            if isinstance(raw_rules, Sequence) and not isinstance(raw_rules, (str, bytes)):
                replace_rules = tuple(
                    ReplaceRule.from_json(r) for r in raw_rules if isinstance(r, Mapping)
                )
            else:
                replace_rules = ()

            split_regex = data.get("split_regex")
            if split_regex is not None:
                split_regex = str(split_regex)

            return cls(
                enabled=bool(data.get("enabled", True)),
                encoding=str(data.get("encoding", "utf-8") or "utf-8"),
                split_regex=split_regex,
                keep_delimiters=bool(data.get("keep_delimiters", False)),
                flags=int(data.get("flags", 0)),
                replace_rules=replace_rules,
                skip_empty=bool(data.get("skip_empty", True)),
            )
        except (TypeError, ValueError):
            return cls()
        
def _apply_replace_rules(text: str, rules: tuple[ReplaceRule, ...]) -> str:
    for rule in rules:
        if rule.pattern:
            text = re.sub(rule.pattern, rule.repl, text)
    return text


def _split_text_pages(text: str, split_regex: str | None, keep_delimiters: bool, flags: int) -> list[str]:
    if not split_regex:
        return [text]

    if keep_delimiters:
        parts = re.split(f"({split_regex})", text, flags=flags)
        out: list[str] = []
        buf = ""
        for part in parts:
            if not part:
                continue
            if re.fullmatch(split_regex, part, flags=flags):
                if buf.strip():
                    out.append(buf)
                buf = part
            else:
                buf += part
        if buf.strip():
            out.append(buf)
        return out

    return [p for p in re.split(split_regex, text, flags=flags) if p is not None]


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()

def _get_lxml_html():
    import lxml.html
    return lxml.html


def _get_lxml_etree():
    from lxml import etree
    return etree


def _extract_xpath_text(node, text_xpath: str | None) -> str:
    if not text_xpath:
        return "".join(node.itertext()).strip()

    parts = node.xpath(text_xpath)
    out: list[str] = []

    for p in parts:
        if isinstance(p, str):
            out.append(p)
        else:
            try:
                out.append("".join(p.itertext()))
            except AttributeError:
                out.append(str(p))

    return _normalize_text(" ".join(x for x in out if x))

# TextPolicy
#   "enabled": true,
#   "split_regex": "^##\\s+",
#   "flags": 8,
#   "keep_delimiters": true,
#   "replace_rules": [
#     { "pattern": "\\r\\n?", "repl": "\n" },
#     { "pattern": "[ \\t]+", "repl": " " }
#   ],
#   "skip_empty": true
# }

# HtmlPolicy
#   "enabled": true,
#   "page_xpath": "//article",
#   "text_xpath": ".//text()",
#   "replace_rules": [
#     { "pattern": "\\s+", "repl": " " }
#   ],
#   "skip_empty": true,
#   "fallback_to_full_document": true
# }

# XmlPolicy
#   "enabled": true,
#   "page_xpath": "//record",
#   "text_xpath": ".//title/text() | .//body/text()",
#   "replace_rules": [
#     { "pattern": "\\s+", "repl": " " }
#   ],
#   "skip_empty": true

# CsvPolicy
# "enabled": True,
# "columns": ["content"],
# "delimiter": ";",
# "has_header": True,
# "encoding": "utf-8",
# "quotechar": "\"",
# "skip_empty": True

@dataclass(frozen=True)
class HtmlPolicy:
    enabled: bool = True
    encoding: str = "utf-8"
    page_xpath: str | None = None
    text_xpath: str | None = ".//text()"
    replace_rules: tuple[ReplaceRule, ...] = ()
    skip_empty: bool = True
    fallback_to_full_document: bool = True

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "HtmlPolicy":
        if not data:
            return cls()
        try:
            raw_rules = data.get("replace_rules", ())
            if isinstance(raw_rules, Sequence) and not isinstance(raw_rules, (str, bytes)):
                replace_rules = tuple(
                    ReplaceRule.from_json(r) for r in raw_rules if isinstance(r, Mapping)
                )
            else:
                replace_rules = ()

            page_xpath = data.get("page_xpath")
            text_xpath = data.get("text_xpath", ".//text()")

            return cls(
                enabled=bool(data.get("enabled", True)),
                encoding=str(data.get("encoding", "utf-8") or "utf-8"),
                page_xpath=str(page_xpath) if page_xpath else None,
                text_xpath=str(text_xpath) if text_xpath else ".//text()",
                replace_rules=replace_rules,
                skip_empty=bool(data.get("skip_empty", True)),
                fallback_to_full_document=bool(data.get("fallback_to_full_document", True)),
            )
        except (TypeError, ValueError):
            return cls()

@dataclass(frozen=True)
class XmlPolicy:
    enabled: bool = True
    encoding: str = "utf-8"
    page_xpath: str | None = None
    text_xpath: str | None = ".//text()"
    replace_rules: tuple[ReplaceRule, ...] = ()
    skip_empty: bool = True
    fallback_to_full_document: bool = True

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "XmlPolicy":
        if not data:
            return cls()
        try:
            raw_rules = data.get("replace_rules", ())
            if isinstance(raw_rules, Sequence) and not isinstance(raw_rules, (str, bytes)):
                replace_rules = tuple(
                    ReplaceRule.from_json(r) for r in raw_rules if isinstance(r, Mapping)
                )
            else:
                replace_rules = ()

            page_xpath = data.get("page_xpath")
            text_xpath = data.get("text_xpath", ".//text()")

            return cls(
                enabled=bool(data.get("enabled", True)),
                encoding=str(data.get("encoding", "utf-8") or "utf-8"),
                page_xpath=str(page_xpath) if page_xpath else None,
                text_xpath=str(text_xpath) if text_xpath else ".//text()",
                replace_rules=replace_rules,
                skip_empty=bool(data.get("skip_empty", True)),
                fallback_to_full_document=bool(data.get("fallback_to_full_document", True)),
            )
        except (TypeError, ValueError):
            return cls()

@dataclass(frozen=True)
class CsvPolicy:
    enabled: bool = True
    columns: tuple[str, ...] = ()
    delimiter: str = ","
    has_header: bool = True
    encoding: str = "utf-8"
    quotechar: str = '"'
    skip_empty: bool = True

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "CsvPolicy":
        if not data:
            return cls()

        try:
            raw_columns = data.get("columns", ())
            if isinstance(raw_columns, str):
                columns = (raw_columns,)
            elif isinstance(raw_columns, Sequence):
                columns = tuple(str(c) for c in raw_columns if c is not None)
            else:
                columns = ()

            delimiter = str(data.get("delimiter", ",") or ",")
            if len(delimiter) != 1:
                delimiter = ","

            quotechar = str(data.get("quotechar", '"') or '"')
            if len(quotechar) != 1:
                quotechar = '"'

            return cls(
                enabled=bool(data.get("enabled", True)),
                columns=columns,
                delimiter=delimiter,
                has_header=bool(data.get("has_header", True)),
                encoding=str(data.get("encoding", "utf-8") or "utf-8"),
                quotechar=quotechar,
                skip_empty=bool(data.get("skip_empty", True)),
            )
        except (TypeError, ValueError):
            return cls()

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
    return load_dict_like(config_path, label="OCR Config", verbose=False) or {}  
 
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
    ensure_import("pymupdf")
    import pymupdf
    return pymupdf

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
    csv_policy: CsvPolicy = CsvPolicy(),
    text_policy: TextPolicy = TextPolicy(),
    html_policy: HtmlPolicy = HtmlPolicy(),
    xml_policy: XmlPolicy = XmlPolicy(),
    json_policy: JsonPolicy = JsonPolicy(),
    timeout: int = 30,
    file_formats: list | None = None,
    start_page: int = 1,
    doc_id: str = "n/a",
    skip: bool = False,
    skip_pages: Optional[set[int]] = None,
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
        return page, total

    if file_formats and not kind in file_formats:
        logger.warning(f"{doc_id}: File {input} skipped as {kind} not in {file_formats}")
        return
    else:
        logger.info(f"{doc_id}: Processing {str(kind).upper()} {src_kind} --> {input}")
        
    if skip and kind in ("pdf"):
        logger.warning("Has not downloaded PDF to save traffic")
        yield _log_and_yield(PageItem(start_page, "sniff", "", source=f"url:{src_path}", total=-1),-1)

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
        
        if skip:
            yield _log_and_yield(PageItem(start_page, "sniff", "", source=f"url:{src_path}", total=len(pages)),len(pages))

        logger.info(f"{doc_id}: Found {len(pages)} pages in IIIF, starting at {start_page}")
        pages = pages[(start_page - 1):]
        logger.debug(f"IIIF Policy: {iiif_ocr_policy}")
        try:
            for i, (img_url, canvas) in enumerate(pages, start=start_page):
                if skip_pages is not None and i in skip_pages:
                    continue
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

            pymupdf = _get_PyMuPDF()
            doc = pymupdf.open(pdf_path)
            scale = pdf_dpi / 72
            mat = pymupdf.Matrix(scale, scale)
            total = doc.page_count
            logger.info(f"{doc_id}: Found {total} pages in PDF, starting at {start_page}")

            # if skip:
            #     yield _log_and_yield(PageItem(start_page, "sniff", "", source=f"pdf-file:{input}", total=total),total)

            # pages = pages[(start_page - 1):]
            for i in range(start_page, total + 1):
                if skip_pages is not None and i in skip_pages:
                    continue
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

    if kind in ("text", "html", "xml"):
        try:
            if kind == "text":
                policy = text_policy
                encoding = policy.encoding
            elif kind == "html":
                policy = html_policy
                encoding = policy.encoding
            else:
                policy = xml_policy
                encoding = policy.encoding

            if src_kind == "file":
                raw = src_path.read_text(encoding=encoding)
            else:
                r = requests.get(input, timeout=timeout, headers=APP_USER)
                r.raise_for_status()
                if not r.encoding:
                    r.encoding = encoding
                raw = r.text

            if kind == "text":
                if not policy.enabled:
                    aPage = PageItem(1, "text", raw, source=f"{kind}:{input}", total=1)
                    yield _log_and_yield(aPage, 1)
                    return

                raw = _apply_replace_rules(raw, policy.replace_rules)
                pages = [_normalize_text(p) for p in _split_text_pages(
                    raw,
                    split_regex=policy.split_regex,
                    keep_delimiters=policy.keep_delimiters,
                    flags=policy.flags,
                )]

                if policy.skip_empty:
                    pages = [p for p in pages if p]

                total = len(pages) or 1
                logger.info(f"{doc_id}: Found {total} text page(s), starting at {start_page}")

                if skip:
                    yield _log_and_yield(
                        PageItem(start_page, "sniff", "", source=f"text-file:{input}", total=total),
                        total,
                    )

                for i, txt in enumerate(pages, start=1):
                    if i < start_page:
                        continue
                    if skip_pages is not None and i in skip_pages:
                        continue

                    aPage = PageItem(i, "text", txt, source=f"text:{input}#page={i}", total=total)
                    yield _log_and_yield(aPage, total)

                return

            elif kind == "html":
                if not policy.enabled:
                    aPage = PageItem(1, "text", raw, source=f"{kind}:{input}", total=1)
                    yield _log_and_yield(aPage, 1)
                    return

                lxml_html = _get_lxml_html()
                doc = lxml_html.fromstring(raw)

                nodes = doc.xpath(policy.page_xpath) if policy.page_xpath else [doc]
                if not nodes and policy.fallback_to_full_document:
                    nodes = [doc]

                pages = []
                for node in nodes:
                    txt = _extract_xpath_text(node, policy.text_xpath)
                    txt = _apply_replace_rules(txt, policy.replace_rules)
                    txt = _normalize_text(txt)
                    if txt or not policy.skip_empty:
                        pages.append(txt)

                total = len(pages) or 1
                logger.info(f"{doc_id}: Found {total} HTML page(s), starting at {start_page}")

                if skip:
                    yield _log_and_yield(
                        PageItem(start_page, "sniff", "", source=f"html-file:{input}", total=total),
                        total,
                    )

                for i, txt in enumerate(pages, start=1):
                    if i < start_page:
                        continue
                    if skip_pages is not None and i in skip_pages:
                        continue

                    aPage = PageItem(i, "text", txt, source=f"html:{input}#page={i}", total=total)
                    yield _log_and_yield(aPage, total)

                return

            elif kind == "xml":
                if not policy.enabled:
                    aPage = PageItem(1, "text", raw, source=f"{kind}:{input}", total=1)
                    yield _log_and_yield(aPage, 1)
                    return

                etree = _get_lxml_etree()
                parser = etree.XMLParser(recover=True)
                doc = etree.fromstring(raw.encode(encoding), parser=parser)

                nodes = doc.xpath(policy.page_xpath) if policy.page_xpath else [doc]
                if not nodes and policy.fallback_to_full_document:
                    nodes = [doc]

                pages = []
                for node in nodes:
                    txt = _extract_xpath_text(node, policy.text_xpath)
                    txt = _apply_replace_rules(txt, policy.replace_rules)
                    txt = _normalize_text(txt)
                    if txt or not policy.skip_empty:
                        pages.append(txt)

                total = len(pages) or 1
                logger.info(f"{doc_id}: Found {total} XML page(s), starting at {start_page}")

                if skip:
                    yield _log_and_yield(
                        PageItem(start_page, "sniff", "", source=f"xml-file:{input}", total=total),
                        total,
                    )

                for i, txt in enumerate(pages, start=1):
                    if i < start_page:
                        continue
                    if skip_pages is not None and i in skip_pages:
                        continue

                    aPage = PageItem(i, "text", txt, source=f"xml:{input}#page={i}", total=total)
                    yield _log_and_yield(aPage, total)

                return

        except Exception as e:
            logger.error(f"{doc_id}: Reading {kind.upper()} {input}: {e}")
        return

    if kind == "json":
        try:
            if src_kind == "file":
                raw = src_path.read_text(encoding=json_policy.encoding)
            else:
                r = requests.get(input, timeout=timeout, headers=APP_USER)
                r.raise_for_status()
                if not r.encoding:
                    r.encoding = json_policy.encoding
                raw = r.text

            if not json_policy.enabled:
                aPage = PageItem(1, "text", raw, source=f"json:{input}", total=1)
                yield _log_and_yield(aPage, 1)
                return

            data = json.loads(raw)

            # --- Paginierung ---
            if json_policy.page_path:
                pages = _json_get_path(data, json_policy.page_path)
            else:
                pages = data

            if pages is None:
                pages = []

            # normalize: immer Liste
            if not isinstance(pages, list):
                pages = [pages]

            if not pages and json_policy.fallback_to_full_document:
                pages = [data]

            total = len(pages) or 1

            logger.info(f"{doc_id}: Found {total} JSON page(s), starting at {start_page}")

            if skip:
                yield _log_and_yield(
                    PageItem(start_page, "sniff", "", source=f"json-file:{input}", total=total),
                    total,
                )

            # --- Yield pro Element ---
            for i, item in enumerate(pages, start=1):
                if i < start_page:
                    continue
                if skip_pages is not None and i in skip_pages:
                    continue

                txt = _json_extract_text(item, json_policy)

                if not txt and json_policy.skip_empty:
                    continue

                aPage = PageItem(
                    i,
                    "text",
                    txt,
                    source=f"json:{input}#page={i}",
                    total=total,
                )
                yield _log_and_yield(aPage, total)

        except Exception as e:
            logger.error(f"{doc_id}: Reading JSON {input}: {e}")
        return

    # if kind in ("text", "html", "xml"): # TODO XML parsing
    #     try:
    #         if src_kind == "file":
    #             raw = src_path.read_text(encoding="utf-8")
    #         else:
    #             r = requests.get(input, timeout=timeout, headers=APP_USER)
    #             r.raise_for_status()
    #             if not r.encoding:
    #                 r.encoding = "utf-8"
    #             raw = r.text
    #         aPage = PageItem(1, "text", raw, source=f"{kind}:{input}", total=1)
    #         yield _log_and_yield(aPage,1)
    #     except Exception as e:
    #         logger.error(f"{doc_id}: Reading {kind.upper()} {input}: {e}")
    #     return
    
    if kind == "csv":
        try:
            if src_kind == "file":
                raw = src_path.read_text(encoding=csv_policy.encoding)
            else:
                r = requests.get(input, timeout=timeout, headers=APP_USER)
                r.raise_for_status()
                if not r.encoding:
                    r.encoding = csv_policy.encoding
                raw = r.text

            if not csv_policy.enabled:
                aPage = PageItem(1, "text", raw, source=f"csv:{input}", total=1)
                yield _log_and_yield(aPage, 1)
                return

            if csv_policy.has_header:
                reader = csv.DictReader(
                    io.StringIO(raw),
                    delimiter=csv_policy.delimiter,
                    quotechar=csv_policy.quotechar,
                )
                rows = list(reader)
                total = len(rows)

                logger.info(f"{doc_id}: Found {total} rows in CSV, starting at {start_page}")

                if skip:
                    yield _log_and_yield(
                        PageItem(start_page, "sniff", "", source=f"csv-file:{input}", total=total),
                        total,
                    )

                available_columns = tuple(reader.fieldnames or ())
                if csv_policy.columns:
                    missing = [c for c in csv_policy.columns if c not in available_columns]
                    if missing:
                        raise ValueError(
                            f"CSV columns not found: {missing}. Available columns: {available_columns}"
                        )
                    selected_columns = csv_policy.columns
                else:
                    selected_columns = available_columns

                for i, row in enumerate(rows, start=1):
                    if i < start_page:
                        continue
                    if skip_pages is not None and i in skip_pages:
                        continue

                    parts = []
                    for col in selected_columns:
                        value = row.get(col, "")
                        if value is None:
                            value = ""
                        value = str(value).strip()
                        if value or not csv_policy.skip_empty:
                            parts.append(f"{col}: {value}" if len(selected_columns) > 1 else value)

                    txt = "\n".join(parts).strip()

                    if not txt and csv_policy.skip_empty:
                        continue

                    aPage = PageItem(
                        i,
                        "text",
                        txt,
                        source=f"csv-text:{input}#row={i}",
                        total=total,
                    )
                    yield _log_and_yield(aPage, total)

            else:
                reader = csv.reader(
                    io.StringIO(raw),
                    delimiter=csv_policy.delimiter,
                    quotechar=csv_policy.quotechar,
                )
                rows = list(reader)
                total = len(rows)

                logger.info(f"{doc_id}: Found {total} rows in CSV, starting at {start_page}")

                if skip:
                    yield _log_and_yield(
                        PageItem(start_page, "sniff", "", source=f"csv-file:{input}", total=total),
                        total,
                    )

                if csv_policy.columns:
                    try:
                        selected_indices = tuple(int(c) for c in csv_policy.columns)
                    except ValueError as e:
                        raise ValueError(
                            "CSV policy columns must be numeric strings when has_header=False"
                        ) from e
                else:
                    max_cols = max((len(r) for r in rows), default=0)
                    selected_indices = tuple(range(max_cols))

                for i, row in enumerate(rows, start=1):
                    if i < start_page:
                        continue
                    if skip_pages is not None and i in skip_pages:
                        continue

                    parts = []
                    for idx in selected_indices:
                        value = row[idx] if idx < len(row) else ""
                        value = str(value).strip()
                        if value or not csv_policy.skip_empty:
                            parts.append(f"col{idx}: {value}" if len(selected_indices) > 1 else value)

                    txt = "\n".join(parts).strip()

                    if not txt and csv_policy.skip_empty:
                        continue

                    aPage = PageItem(
                        i,
                        "text",
                        txt,
                        source=f"csv-text:{input}#row={i}",
                        total=total,
                    )
                    yield _log_and_yield(aPage, total)

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
    framework: Literal["kraken", "tesseract", "source"] = "kraken",
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
    yield_result:bool = True,
) -> Iterator[Tuple[int, str]]:
    from .helpers import ISO_ts
    iter_kwargs = dict(iter_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    logger.debug(
        f"iter_text_pages received: {[iter_kwargs, page_to_text_kwargs, text_image_file_kwargs, framework]}"
    )
    
    cfg = text_image_file_kwargs or {}
    use_transformer = framework == "transformer"
    ocr = framework and framework not in {"none"}
    ts_in = ISO_ts()
    call_args = {
            "input": input,
            "doc_id": doc_id,
            "iter_kwargs": iter_kwargs,
            "page_to_text_kwargs": page_to_text_kwargs,
            "text_image_file_kwargs": text_image_file_kwargs,
            "framework": framework,
            "yield_result": yield_result
        }
    meta_dict = {
        'call':call_args,
        'ts_in': ts_in
        }
    if use_transformer:
        try:
            logger.info("### Using Transformer from medieval_ocr_pipeline ###")
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
            framework = "soruce" 

    try:
        from zotero_rdf_server.config import EXPORT_DIRECTORY
        EXPORT_DIRECTORY = Path(EXPORT_DIRECTORY)
    except Exception:
        EXPORT_DIRECTORY = Path().resolve()

    img_out: Optional[str] = cfg.get("img_out", "images")
    txt_out: Optional[str] = cfg.get("txt_out", "texts")
    meta_out = cfg.get("meta_out")
    img_ext: str = cfg.get("img_ext", "jpg")
    txt_ext: str = cfg.get("txt_ext", "txt")

    save_text: str = cfg.get("save_text", "skip")  # "skip" | "overwrite" | "active"
    save_image: str = cfg.get("save_image", "skip")  # "skip" | "overwrite" | "active"
    on_error: str = cfg.get("on_error", "log")  # "raise" | "skip" | "empty" | "log"

    if save_text not in {"skip", "overwrite", "active", "cache"}:
        raise ValueError(f"save_text must be 'active', 'skip' or 'overwrite', got {save_text}")
    if save_image not in {"skip", "cache", "overwrite", "active", "sniff", "smart"}:
        raise ValueError(
            f"save_image must be one of 'skip', 'cache', 'overwrite', 'active', 'sniff', 'smart', got {save_image}"
        )
    if on_error not in {"raise", "skip", "empty", "log"}:
        raise ValueError(f"on_error must be 'raise', 'skip', 'empty' or 'log', got {on_error}")

    _doc_id = safe_doc_id(doc_id or input)
    iter_kwargs['doc_id']=_doc_id
    iter_kwargs['skip'] = save_image in {"sniff"} # TODO


    def _meta_file(meta:dict):
        if meta_out:                 
            try:
                from .helpers import make_json_safe
                meta_safe = make_json_safe(meta)
                meta_file = _resolve_out(meta_out, None) / f"{_doc_id}.json"   
                logger.debug(f"Stored meta: {meta_file}")                
                meta_file.parent.mkdir(parents=True, exist_ok=True)
                meta_file.write_text(json.dumps(meta_safe,indent=4,default=str), encoding="utf-8")
            except Exception as e:
                logger.error(f"{_doc_id}: Failed to store {meta_file}: {e}")

    def _resolve_out(p: Optional[str], doc_dir:str|None = _doc_id) -> Optional[Path]:
        if not p:
            return None
        pp = Path(p)
        if pp.is_absolute():
            logger.error(f"Absolute paths are not allowed: {pp}")
            return (EXPORT_DIRECTORY / doc_dir).resolve()
        result_path = (EXPORT_DIRECTORY / pp / doc_dir).resolve() if doc_dir else (EXPORT_DIRECTORY / pp ).resolve()
        logger.info(f"Export path set: {result_path}")
        return result_path

    img_dir = _resolve_out(img_out)
    txt_dir = _resolve_out(txt_out)    

    def _save_pil(im, path: Path) -> None:
        try:
            logger.info(f"Stored file: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            im.save(path)
        except Exception as e:
            logger.error(f"{_doc_id}: Failed to store {path}: {e}")

    def _save_text(txt: str, path: Path) -> None:
        try:
            logger.debug(f"Stored file: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(txt, encoding="utf-8")
        except Exception as e:
            logger.error(f"{_doc_id}: Failed to store {path}: {e}")

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

    def _cache_discrepancy_report() -> dict[str, Any]:        
        pages = _cached_pages()
        text_pages = set(pages["text"])
        image_pages = set(pages["image"])        
        return {
            "call": call_args,
            "ts_in": ts_in,
            "ts_out": ISO_ts(),
            "text_pages": sorted(text_pages),
            "image_pages": sorted(image_pages),
            "text_count": len(text_pages),
            "image_count": len(image_pages),
            "text_only": sorted(text_pages - image_pages),
            "image_only": sorted(image_pages - text_pages),
            "shared": sorted(text_pages & image_pages),
        }
    
    def _log_discrepancy_report(total: int = 0) -> None:
        if save_text in {"cache"}:
            logger.info(
                "TEXT CACHE mode has not stored text files, but it reused cached text if at least one available; "
            )
        if save_text in {"skip"}:
            logger.info(
                "TEXT SKIP mode has not stored text files, but it reused cached text if at least one available; "
                "otherwise it processed pages (OCR) without persisting results."
            )

        if save_text in {"active"}:
            logger.info(
                "TEXT ACTIVE mode has stored missing text files, reusing cached text where available; "
                "OCR was only performed for pages without cached text."
            )

        if save_text in {"overwrite"}:
            logger.info(
                "TEXT OVERWRITE mode has regenerated and stored all text files, ignoring existing cache; "
                "OCR was performed for every page and existing text files were replaced."
            )
        if save_image in {"skip"}:
            logger.info("IMAGE SKIP mode has not stored images, but it downloaded from source, if no cached images were found.")
        if save_image in {"cache"}:
            logger.info("IMAGE CACHE mode only used cached images.")
        if save_image in {"active"}:
            logger.info("IMAGE ACTIVE has stored missing images, but only downloaded from source, if no cached images were found.")
        if save_image in {"smart"}:
            logger.info("IMAGE SMART mode has downloaded from source and stored missing image files.")
        if save_image in {"sniff"}:
            logger.info("IMAGE SNIFF mode has neither stored nor yielded any text data, but it downloaded from source for metadata sniffing.")

        report = _cache_discrepancy_report()
        report['total_source'] = total   
        if total > 0 and not report['image_count'] == total:
            logger.warning(f"DISCREPANCY REPORT FOR {_doc_id}: {report['image_count']} images cached, {total} found in source!")
        if total > 0 and not report['text_count'] == total:
            logger.warning(f"DISCREPANCY REPORT FOR {_doc_id}: {report['text_count']} texts cached, {total} found in source!")
        if report["text_only"]:
            logger.info(
                f"DISCREPANCY REPORT FOR {_doc_id}: {len(report['text_only'])} cached text pages without cached image file: "
                f"{report['text_only'][:10]}{' ...' if len(report['text_only']) > 10 else ''}"
            )

        if report["image_only"]:
            logger.info(
                f"DISCREPANCY REPORT FOR {_doc_id}: {len(report['image_only'])} cached image pages without cached text file: "
                f"{report['image_only'][:10]}{' ...' if len(report['image_only']) > 10 else ''}"
            )
      
        _meta_file(report)
        logger.info("Report completed!")

    def _maybe_store_text(page_no: int, txt: str) -> None:
        if save_text not in {"active", "overwrite"} or not ocr:
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
        return page_no, txt # TODO return LLM?
    
    _meta_file(meta_dict)
    cached_page_set = _cached_pages()

    logger.info(f"{_doc_id}: Found {len(set(cached_page_set['text']))} text files and {len(set(cached_page_set['image']))} image files")

    if not yield_result:
        logger.warning("Cached text files will not be logged and yielded!")

    # If text file found and not overwrite, use as result and skip download + OCR
    if (
        save_text not in {"overwrite"} # {"active", "skip"}
        and save_image in {"skip"} #  == "skip"
        and txt_dir is not None
        and any(txt_dir.glob(f"*.{txt_ext}"))        
    ):      
        logger.warning(f"{_doc_id}: Using {len(set(cached_page_set['text']))} text files in {txt_dir}")
        if yield_result:
            yield from _yield_from_cache()
        _log_discrepancy_report()
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
    if save_image in {"cache"} and img_dir is not None and img_dir.exists():
        cached_imgs = list(_iter_cached_image_pages(img_dir, img_ext))
        total = len(cached_imgs)
        if cached_imgs:
            logger.warning(f"{_doc_id}: Using {len(set(cached_page_set['image']))} image files in {img_dir}; no remote download")
            for page_no, img_path in cached_imgs:
                tp = _text_path(page_no)
                if save_text not in {"overwrite"} and tp and tp.exists(): # Cache
                    if yield_result:
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
                        # yield page_no, tp.read_text(encoding="utf-8")
                    continue

                if ocr: # OCR
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

                    _maybe_store_text(page_no, txt)
                    if yield_result: 
                        yield _log_and_yield(page_no, txt, total)
                    else:
                        _log_and_yield(page_no, txt, total)
            
            _log_discrepancy_report()
            return
    
    # Download
    total = 0
    _report = _cache_discrepancy_report()

    if save_text in {"overwrite"}:
        skip_pages = set()
    elif save_image in {"smart"}:        
        skip_pages = set(_report["image_pages"])
        _sneak_kwargs = {**iter_kwargs, "skip": True}
        try:
            _sneak, _total = next(iter_pages(input=input, **_sneak_kwargs))
            logger.info(f"SMART: Skipping {len(skip_pages)} pages when iterating all {_total} source pages.")
            if _total == len(skip_pages):
                logger.debug("Skip from here?")
        except Exception as e:
            logger.info(f"SMART: could not peep into file: {e}.")
    elif save_image in {"active", "overwrite"}:
        skip_pages = set(_report["shared"])
    else:
        skip_pages = set(_report["text_pages"])

    
    iter_kwargs["skip_pages"] = skip_pages

    for item, total in iter_pages(input=input, **iter_kwargs):
        page_no = getattr(item, "sequence", None) or getattr(item, "index", None)        

        if page_no is None:
            raise AttributeError("PageItem has neither .sequence nor .index")
        if save_image in {"sniff"}:
            break
        if "overwrite" not in {save_image, save_text}:
            if set(_report['shared']) == set(range(1, int(total + 1))):
                logger.warning(f"{total} images and text files found that match source --> skip this item!")
                if yield_result:
                    yield from _yield_from_cache()
                _log_discrepancy_report(total)
                return
        if save_image in {"smart"} and int(page_no) in _report['shared']:
            continue

        # if save_image == "active":
        #     ip = _image_path(page_no)
        #     if ip is not None and ip.exists():
        #         try:
        #             with Image.open(ip) as im:
        #                 item.data = im.copy()
        #             item.kind = getattr(item, "kind", "image")
        #         except Exception as e:
        #             logger.error(f"{_doc_id}: Failed to load cached image for page {page_no} from {str(ip)}: {e}")
        #             if on_error == "raise":
        #                 raise
        #             if on_error == "skip":
        #                 continue

        tp = _text_path(page_no)

        # Save Image
        if item.kind == "image" and save_image not in {"skip", "cache"} and img_dir is not None:
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
                else:
                    logger.debug(f"Image exists: {ip}")

        # If Cached Text --> return
        if save_image not in {"smart"} and save_text not in {"overwrite"} and tp is not None and tp.exists():
            if yield_result:
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
        if ocr:
            logger.info(f"Using {framework.upper()}")
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

            _maybe_store_text(page_no, txt)
            if yield_result: 
                yield _log_and_yield(page_no, txt, total)
            else:
                _log_and_yield(page_no, txt, total)

    _log_discrepancy_report(total)

# END