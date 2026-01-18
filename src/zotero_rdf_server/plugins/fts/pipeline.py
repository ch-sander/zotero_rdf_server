from dataclasses import dataclass
from typing import Iterator, Optional, Dict, Any, List, Literal, Union, Tuple
import io, json, os, tempfile
import requests
from PIL import Image
from functools import lru_cache
from pathlib import Path
# from zotero_rdf_server.utils import ensure_import
import subprocess, importlib, sys

here = Path(__file__).resolve().parent
requirements = here / "requirements.txt"

@dataclass(frozen=True)
class PageItem:
    index: int
    kind: Literal["image", "text"]
    data: Union[Image.Image, str]
    source: str
    meta: Optional[dict] = None

@dataclass(frozen=True)
class PdfTextPolicy:
    enabled: bool = True
    min_chars: int = 80
    min_alpha_ratio: float = 0.6

def ensure_import(module, attr=None, requirements=None):
    try:
        mod = importlib.import_module(module)
    except ImportError:
        if requirements is None:
            raise

        print("%s not found. Installing dependencies...", module)
        subprocess.check_call([
            sys.executable,
            "-m", "pip",
            "install",
            "-r", str(requirements),
        ])
        mod = importlib.import_module(module)

    return getattr(mod, attr) if attr else mod

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


def detect_url_kind(url: str, timeout: int = 30) -> str:
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
    return "json"

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
    r = requests.get(url, stream=True, timeout=timeout)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content))
    im.load()
    return im

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
    PdfReader = ensure_import("pypdf", attr="PdfReader", requirements=requirements)
    pdfium = ensure_import("pypdfium2", requirements=requirements)
    return PdfReader, pdfium

def iter_pages(
    url: str,
    *,
    iiif_max_width: Optional[int] = 2000,
    iiif_format: str = "jpg",
    pdf_dpi: int = 200,
    pdf_text_policy: PdfTextPolicy = PdfTextPolicy(),
    timeout: int = 30,
) -> Iterator[PageItem]:

    kind = detect_url_kind(url, timeout=timeout)

    if kind == "json":
        manifest = requests.get(url, timeout=timeout).json()
        img_urls = iiif_manifest_to_image_urls(manifest, max_width=iiif_max_width, fmt=iiif_format)
        if not img_urls:
            raise ValueError("IIIF-Manifest found but no image canvas.")
        for i, img_url in enumerate(img_urls, start=1):
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
                    yield PageItem(i, "image", pil, source=f"pdf-image:{url}#page={i}")
        finally:
            try:
                os.remove(pdf_path)
            except OSError:
                pass
        return

    raise ValueError("Unknown URL type.")

def kraken_image_to_text(
    im: Image.Image,
    *,
    model_path: str,
    binarize: bool = False,
) -> str:
    ensure_import("kraken", requirements=requirements)
    from kraken import binarization, blla, rpred, pageseg
    from kraken.lib import models, vgsl
    from importlib import resources
    default_seg_model = resources.files("kraken").joinpath("blla.mlmodel")
    seg_model = vgsl.TorchVGSLModel.load_model(default_seg_model)
    work = binarization.nlbin(im) if binarize else im
    # seg = pageseg.segment(work)
    seg_blla = blla.segment(work, model=seg_model)
    model = models.load_any(model_path)
    preds = rpred.rpred(network=model, im=work, bounds=seg_blla)
    return "\n".join(p.prediction for p in preds)

def page_to_text(item: PageItem, *, kraken_model_path: str, binarize: bool=False) -> str:
    if item.kind == "text":
        return item.data  # type: ignore[return-value]
    return kraken_image_to_text(item.data, model_path=kraken_model_path, binarize=binarize)

def url_to_text_pages(
    url: str,
    *,
    kraken_model_path: str,
    iiif_max_width: int = 2000,
    pdf_dpi: int = 200,
    pdf_text_policy: PdfTextPolicy = PdfTextPolicy(),
    binarize: bool = True,
) -> Iterator[Tuple[int, str]]:
    for item in iter_pages(
        url,
        iiif_max_width=iiif_max_width,
        pdf_dpi=pdf_dpi,
        pdf_text_policy=pdf_text_policy,
    ):
        yield item.index, page_to_text(item, kraken_model_path=kraken_model_path, binarize=binarize)
