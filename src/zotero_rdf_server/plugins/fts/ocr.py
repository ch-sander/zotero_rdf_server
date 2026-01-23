from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Iterator, Optional, Dict, Any, List, Literal, Union, Tuple, Mapping
import io, json, os, tempfile
import requests
from PIL import Image
from functools import lru_cache
from pathlib import Path
from .helpers import ensure_import, _hash_file,  resolve_config_path, _download, plugin_logger, detect_url_kind

from .helpers import plugin_logger
logger=plugin_logger()


@dataclass(frozen=True)
class KrakenModelSpec:
    file: str
    url: Optional[str] = None
    checksum_algo: Optional[str] = None   # "md5" or "sha256"
    checksum: Optional[str] = None

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
    
@lru_cache(maxsize=8)
def get_kraken_cfg(config_path: Path) -> dict[str, Any]:
    # import yaml
    from zotero_rdf_server.utils import load_dict_like
    
    # path = Path(config_path).expanduser().resolve()
    # with path.open("r", encoding="utf-8") as f:
    #     cfg = yaml.safe_load(f) or {}
    cfg = load_dict_like(config_path, "Kraken Config")
    return cfg.get("kraken") or cfg

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


@dataclass(frozen=True)
class PageItem:
    index: int
    kind: Literal["image", "text"]
    data: Union[Image.Image, str]
    source: str
    meta: Optional[dict] = None

def is_usable_pdf_text(text: str, policy: PdfTextPolicy) -> bool:
    if not policy.enabled:
        return False
    if not text:
        return False
    t = text.strip()
    if len(t) < policy.min_chars:
        return False
    alpha = sum(c.isalpha() for c in t)
    return (alpha / max(len(t), 1)) >= policy.min_alpha_ratio

def detect_url_kind_deprecated(url: str, timeout: int = 30) -> str:
    # "pdf" | "json"
    try:
        h = requests.head(url, allow_redirects=True, timeout=timeout)
        ctype = (h.headers.get("Content-Type") or "").lower()
        if "application/pdf" in ctype:
            return "pdf"
    except requests.RequestException:
        pass

    r = requests.get(url, stream=True, timeout=timeout)
    r.raise_for_status()
    first = r.raw.read(5)
    if first == b"%PDF-":
        return "pdf"
    return "json" # TODO Better test

def iiif_manifest_to_image_urls(manifest: Dict[str, Any], max_width: Optional[int]=2000, fmt: str="jpg") -> List[str]:
    def mk(service_id: str) -> str:
        size = f"{max_width}," if max_width else "full"
        return f"{service_id.rstrip('/')}/full/{size}/0/default.{fmt}"

    urls: List[str] = []

    # v3
    if "items" in manifest:
        for canvas in manifest.get("items", []):
            for anno_page in canvas.get("items", []):
                for anno in anno_page.get("items", []):
                    body = anno.get("body", {})
                    service = body.get("service")
                    sid = None
                    if isinstance(service, list) and service:
                        sid = service[0].get("id") or service[0].get("@id")
                    elif isinstance(service, dict):
                        sid = service.get("id") or service.get("@id")

                    if sid:
                        urls.append(mk(sid))
                    elif isinstance(body, dict) and body.get("id"):
                        urls.append(body["id"])
        return urls

    # v2
    seqs = manifest.get("sequences", [])
    if seqs:
        canvases = seqs[0].get("canvases", [])
        for canvas in canvases:
            for img in canvas.get("images", []):
                res = img.get("resource", {})
                service = res.get("service", {})
                sid = service.get("@id") or service.get("id")
                if sid:
                    urls.append(mk(sid))
                elif res.get("@id"):
                    urls.append(res["@id"])

    return urls

def fetch_pil_image(url: str, timeout: int = 60) -> Image.Image:
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        im.load()
        return im
    except Exception as e:
        logger.error(f"Fetching image {url}: {e}")

def stream_download_to_tempfile(url: str, suffix: str, timeout: int = 120) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(chunk)
        return tmp.name

@lru_cache(maxsize=1)
def _get_pdf_libs():
    PdfReader = ensure_import("pypdf", attr="PdfReader")
    pdfium = ensure_import("pypdfium2")
    return PdfReader, pdfium

def iter_pages(
    url: str,
    *,
    iiif_max_width: Optional[int] = 2000,
    iiif_format: str = "jpg",
    pdf_dpi: int = 200,
    pdf_text_policy: PdfTextPolicy = PdfTextPolicy(),
    timeout: int = 30,
    file_formats: list | None = None
) -> Iterator[PageItem]:

    kind = detect_url_kind(url, timeout=timeout)

    if file_formats and not kind in file_formats:
        logger.warning(f"File {url} skipped as not in {file_formats}")
        return

    if kind in ("json", "iiif"):
        manifest = requests.get(url, timeout=timeout).json()
        img_urls = iiif_manifest_to_image_urls(manifest, max_width=iiif_max_width, fmt=iiif_format)
        if not img_urls:
            logger.warning(f"IIIF manifest {url} has no image canvases")
            return
        for i, img_url in enumerate(img_urls, start=1):
            logger.debug(f"iter_pages yielding page={i}")
            yield PageItem(i, "image", fetch_pil_image(img_url), source=f"iiif:{img_url}")
        return

    if kind == "pdf":
        pdf_path = stream_download_to_tempfile(url, suffix=".pdf")
        try:
            # from pypdf import PdfReader
            # import pypdfium2 as pdfium
            PdfReader, pdfium = _get_pdf_libs()

            reader = PdfReader(pdf_path)
            doc = pdfium.PdfDocument(pdf_path)

            for i, page in enumerate(reader.pages, start=1):
                txt = page.extract_text() or ""
                if is_usable_pdf_text(txt, pdf_text_policy):
                    yield PageItem(i, "text", txt, source=f"pdf-text:{url}#page={i}")
                else:
                    pil = doc[i-1].render(scale=pdf_dpi/72).to_pil()
                    logger.debug(f"iter_pages yielding page={i}")
                    yield PageItem(i, "image", pil, source=f"pdf-image:{url}#page={i}")
        except Exception as e:
            logger.error(f"Reading PDF {url}: {e}")

        finally:
            try:
                os.remove(pdf_path)
            except OSError:
                pass
        return
    
    if kind in ("text", "html", "xml"): # TODO XML parsing
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            if not r.encoding:
                r.encoding = "utf-8"
            raw = r.text
            logger.debug(f"iter_pages yielding page={i}")
            yield PageItem(1, "text", raw, source=f"{kind}:{url}")
        except Exception as e:
            logger.error(f"Reading {kind.upper()} {url}: {e}")
        return
    raise ValueError("Unknown URL type.")


def kraken_image_to_text(
    im: Image.Image,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    model_name: str | None = None,
    segmenter: str | None = None,
    binarize: bool = False,
) -> str:
    try:
        ensure_import("kraken")
        from kraken import binarization, blla, rpred #, pageseg
        from kraken.lib import models

        

        cfg_path = resolve_config_path(config_path)
        dom = resolve_domain(config_path=cfg_path, domain=domain)
        recog_name = resolve_recognition_model_name(
            config_path=cfg_path,
            domain=dom,
            model_name=model_name,
        )

        seg_model = load_segmentation_model(config_path=cfg_path, segmenter=segmenter)
        logger.debug(f"Kraken page recognition with {recog_name}...")
        work = binarization.nlbin(im) if binarize else im
        bounds = blla.segment(work, model=seg_model)
        # seg = pageseg.segment(work)
        model_path = str(resolve_kraken_model_path(config_path=cfg_path, model_name=recog_name))
        net = models.load_any(model_path)

        preds = rpred.rpred(network=net, im=work, bounds=bounds)
        ocr_page = "\n".join(p.prediction for p in preds)

        logger.debug(ocr_page)

        return ocr_page
    except Exception as e:
        logger.error(f"Kraken OCR failed: {e}")

def page_to_text(
    item: PageItem,
    *,
    config_path: str | None = None,
    domain: str | None = None,
    model_name: str | None = None,
    segmenter: str | None = None,
    binarize: bool = True,
) -> str:
    if item.kind == "text":
        return item.data  # type: ignore[return-value]
    logger.debug(f"processing image {item.index} of {item.source}")
    return kraken_image_to_text(
        item.data,
        config_path=config_path,
        domain=domain,
        model_name=model_name,
        segmenter=segmenter,
        binarize=binarize,
    )