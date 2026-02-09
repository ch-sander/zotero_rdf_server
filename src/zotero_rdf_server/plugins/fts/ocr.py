from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Iterator, Optional, Dict, Any, List, Literal, Union, Tuple, Mapping
import io, json, os, tempfile, time
import requests
from PIL import Image
from functools import lru_cache
from pathlib import Path
from .helpers import ensure_import, _hash_file,  resolve_config_path, _download, plugin_logger, detect_url_kind, detect_file_kind, resolve_source
from io import BytesIO

from .helpers import plugin_logger, safe_doc_id
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
    cfg = load_dict_like(config_path,label= "Kraken Config")
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
    logger.debug(f"Found PDF text")
    t = text.strip()
    if len(t) < policy.min_chars:
        return False
    alpha = sum(c.isalpha() for c in t)
    return (alpha / max(len(t), 1)) >= policy.min_alpha_ratio

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

def fetch_pil_image_old(url: str, timeout: int = 60) -> Image.Image:
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        im.load()
        return im
    except Exception as e:
        logger.error(f"Fetching image {url}: {e}")

def fetch_pil_image(url: str, *, timeout: int = 30, retries: int = 3, backoff: float = 1.5):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return Image.open(BytesIO(r.content))
        except Exception as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (502, 503, 504) and attempt < retries:
                sleep_s = backoff ** attempt
                time.sleep(sleep_s)
                continue
            logger.error(f"fetch_pil_image failed for {url}: {e}")
            return None
    logger.error(f"fetch_pil_image failed for {url}: {last_exc}")
    return None

def stream_download_to_tempfile_old(url: str, suffix: str, timeout: int = 120) -> str:
    logger.info(f"Downloading {url}")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(chunk)
        return tmp.name

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

            with requests.get(url, stream=True, timeout=timeout) as r:
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

def iter_pages(
    input: str,
    *,
    iiif_max_width: Optional[int] = 2000,
    iiif_format: str = "jpg",
    pdf_dpi: int = 200,
    pdf_text_policy: PdfTextPolicy = PdfTextPolicy(),
    timeout: int = 30,
    file_formats: list | None = None
) -> Iterator[PageItem]:

    src_kind, src_path = resolve_source(input)

    if src_kind == "file":
        kind = detect_file_kind(src_path)
    else:
        kind = detect_url_kind(input, timeout=timeout)

    if file_formats and not kind in file_formats:
        logger.warning(f"File {input} skipped as not in {file_formats}")
        return

    if kind in ("json", "iiif"):
        if src_kind == "file":
            manifest = json.loads(src_path.read_text(encoding="utf-8"))
        else:
            manifest = requests.get(input, timeout=timeout).json()
        img_urls = iiif_manifest_to_image_urls(manifest, max_width=iiif_max_width, fmt=iiif_format)
        if not img_urls:
            logger.warning(f"IIIF manifest {input} has no image canvases")
            return
        for i, img_url in enumerate(img_urls, start=1):
            logger.debug(f"iter_pages yielding page={i}")
            img = fetch_pil_image(img_url)

            if img is None:
                logger.error(f"Skipping page {i}: could not fetch image {img_url}")
                continue
            yield PageItem(i, "image", img, source=f"iiif:{img_url}")
        return

    if kind == "pdf":
        pdf_path = None
        try:
            if src_kind == "file":
                pdf_path = str(src_path)
            else:
                try:
                    pdf_path = stream_download_to_tempfile(input, suffix=".pdf")
                except Exception as e:
                    logger.error(f"Error downloading PDF {input}: {e}")
                    return
            # from pypdf import PdfReader
            # import pypdfium2 as pdfium
            PdfReader, pdfium = _get_pdf_libs()

            reader = PdfReader(pdf_path)
            doc = pdfium.PdfDocument(pdf_path)

            for i, page in enumerate(reader.pages, start=1):
                txt = page.extract_text() or ""
                if is_usable_pdf_text(txt, pdf_text_policy):
                    logger.info(f"Using PDF text {input}")
                    yield PageItem(i, "text", txt, source=f"pdf-text:{input}#page={i}")
                else:
                    pil = doc[i-1].render(scale=pdf_dpi/72).to_pil()
                    logger.debug(f"iter_pages yielding page={i}")
                    yield PageItem(i, "image", pil, source=f"pdf-image:{input}#page={i}")
        except Exception as e:
            logger.error(f"Error reading PDF {input}: {e}")

        finally:
            if pdf_path and src_kind != "file":
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
    
    if kind in ("text", "html", "xml"): # TODO XML parsing
        try:
            if src_kind == "file":
                raw = src_path.read_text(encoding="utf-8")
            else:
                r = requests.get(input, timeout=timeout)
                r.raise_for_status()
                if not r.encoding:
                    r.encoding = "utf-8"
                raw = r.text
            logger.debug(f"iter_pages yielding page={1}")
            yield PageItem(1, "text", raw, source=f"{kind}:{input}")
        except Exception as e:
            logger.error(f"Reading {kind.upper()} {input}: {e}")
        return
    raise ValueError("Unknown URL type.")


import numpy as np

def ink_ratio(pil_img):
    g = pil_img.convert("L")
    a = np.asarray(g)
    bg = np.median(a)
    thr = bg - 25
    ink = (a < thr).mean()
    return float(ink), float(bg), float(thr)


def kraken_image_to_text(
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
) -> str:
    if item.kind == "text":
        return item.data or "" # type: ignore[return-value]
    logger.debug(f"processing image {item.index} of {item.source}")
    if item.data is None:
        logger.error(f"page_to_text got None image: page={item.index} source={item.source}")
        return ""
    return kraken_image_to_text(
        item.data,
        config_path=config_path,
        domain=domain,
        model_name=model_name,
        segmenter=segmenter,
        binarize=binarize,
    )

def iter_text_pages(
    input: str,
    *,
    doc_id: str | None = None,
    iter_kwargs: Dict[str, Any],
    page_to_text_kwargs: Dict[str, Any],
    text_image_file_kwargs: Optional[Dict[str, Any]] = None,
    transformer: bool = False
) -> Iterator[Tuple[int, str]]:
    iter_kwargs = dict(iter_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    logger.debug(
        f"iter_text_pages received: {[iter_kwargs, page_to_text_kwargs, text_image_file_kwargs]}"
    )
    cfg = text_image_file_kwargs or {}

    if transformer:
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
            from .medieval_ocr_pipeline.complete_ocr_pipeline import process_complete_image, setup_models  
            MODELS = setup_models()
            # src\zotero_rdf_server\plugins\fts\medieval_ocr_pipeline\complete_ocr_pipeline.py
        except Exception as e:
            logger.exception("Transformer plugin import failed")

            transformer = False

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
        # erwartet 0001.txt / 0001.jpg etc.
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
    
    def _log_and_yield(page_no: int, txt: str):
        preview = " ".join((txt or "").split())[:100]
        logger.info(f"OCR result doc={_doc_id} page={page_no}: {preview}...")
        return page_no, txt
    
    cached_page_set = _cached_pages()

    logger.info(f"Found {len(set(cached_page_set['text']))} text files and {len(set(cached_page_set['image']))} image files")

    # If text file found and not overwrite, use as result and skip download + OCR
    if (
        save_text == "active"
        and save_image == "skip"
        and txt_dir is not None
        and any(txt_dir.glob(f"*.{txt_ext}"))
    ):      
        logger.warning(f"Using {len(set(cached_page_set['text']))} text files in {txt_dir}")
        yield from _yield_from_cache()
        return    

    # If image file found and not overwrite, use as result and skip download but proceed with OCR
    if save_image == "active" and img_dir is not None and img_dir.exists():
        cached_imgs = list(_iter_cached_image_pages(img_dir, img_ext))
        if cached_imgs:
            logger.warning(f"Using {len(set(cached_page_set['image']))} text files in {img_dir}; no remote download")
            for page_no, img_path in cached_imgs:
                tp = _text_path(page_no)
                if save_text == "active" and save_text != "overwrite" and tp and tp.exists():
                    yield page_no, tp.read_text(encoding="utf-8")
                    continue

                try:
                    with Image.open(img_path) as im:
                        pil = im.copy()
                    item = PageItem(page_no, "image", pil, source=f"cache-image:{img_path}")
                    if transformer:
                        logger.debug("### Using Tranformer from medieval_ocr_pipeline ###")                        
                        res = process_complete_image(item.data, verbose=False, cleanup_temp=True, models=MODELS)
                        txt = "" if res is None else res[0]
                    else:                        
                        txt = page_to_text(item, **page_to_text_kwargs)  # OCR local
                except Exception as e:
                    logger.error(f"Failed to load cached image for page {page_no} from {img_path}: {e}")

                    if on_error == "raise":
                        raise
                    if on_error == "skip":
                        continue
                    txt = "" # DEBUG

                if save_text in {"active", "overwrite"}:
                    _maybe_store_text(page_no, txt)
                yield _log_and_yield(page_no, txt)
            return
    
    for item in iter_pages(input=input, **iter_kwargs):
        page_no = getattr(item, "sequence", None) or getattr(item, "index", None)
        if page_no is None:
            raise AttributeError("PageItem has neither .sequence nor .index")

        # Image: if active and cached -> load from file and set in item.data
        if save_image == "active":
            ip = _image_path(page_no)
            if ip is not None and ip.exists():
                try:
                    with Image.open(ip) as im:
                        item.data = im.copy()
                    item.kind = getattr(item, "kind", "image")
                except Exception as e:
                    logger.error(f"Failed to load cached image for page {page_no} from {str(ip)}: {e}")

                    if on_error == "raise":
                        raise
                    if on_error == "skip":
                        continue
                    # empty/log -> weiter, dann ggf. OCR/remote

        # Text: if active and cached -> read directly, no OCR
        tp = _text_path(page_no)
        if save_text == "active" and tp is not None and tp.exists():
            try:
                txt = tp.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"Failed to load cached text for page {page_no} from {str(tp)}: {e}")
                if on_error == "raise":
                    raise
                if on_error == "skip":
                    continue
                txt = ""
            yield _log_and_yield(page_no, txt)
            continue

        # Save image (active/overwrite)
        if item.kind == "image" and save_image in {"active", "overwrite"} and img_dir is not None:
            ip = _image_path(page_no)
            if ip is not None and item.data is not None and hasattr(item.data, "save"):
                if save_image == "overwrite" or not ip.exists():
                    try:
                        _save_pil(item.data, ip)
                    except Exception as e:
                        logger.error(f"Failed to store image page {page_no} to {str(ip)}: {e}")
                        if on_error == "raise":
                            raise
                        if on_error == "skip":
                            continue

        # OCR / page_to_text
        try:
            if transformer and item.kind == "image":
                logger.debug("### Using Tranformer from medieval_ocr_pipeline ###")
                res = process_complete_image(item.data, verbose=False, cleanup_temp=True, models=MODELS)
                txt = "" if res is None else res[0]
            else:
                txt = page_to_text(item, **page_to_text_kwargs)
        except Exception as e:
            logger.error(f"iter_text_pages error on page {page_no}: {e}")
            if on_error == "raise":
                raise
            if on_error == "skip":
                continue
            txt = ""  # empty/log

        # Save text (active/overwrite)
        if save_text in {"active", "overwrite"} and tp is not None:
            if save_text == "overwrite" or not tp.exists():
                try:
                    _save_text(txt, tp)
                except Exception as e:
                    logger.error(f"Failed to store text page {page_no} to {str(tp)}: {e}")
                    if on_error == "raise":
                        raise
                    if on_error == "skip":
                        continue

        yield _log_and_yield(page_no, txt)