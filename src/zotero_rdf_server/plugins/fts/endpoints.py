from __future__ import annotations
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter, Body
from fastapi.responses import StreamingResponse, FileResponse
from typing import Literal as TypeLiteral, Literal, Any, Dict
import logging
from pathlib import Path
# from zotero_rdf_server.store import *
# from zotero_rdf_server.rdf import *
# from zotero_rdf_server.logging_config import logger, LogLevel
# from zotero_rdf_server.config import *
# from zotero_rdf_server.models import ZoteroLibrary
# from zotero_rdf_server.utils import *
from dataclasses import dataclass

from .helpers import plugin_logger
logger=plugin_logger()

router = APIRouter()


import json
from typing import Iterator, List, Optional, Union
from pydantic import BaseModel, Field

router = APIRouter(tags=["OCR"])

@dataclass(frozen=True)
class PdfTextPolicy:
    enabled: bool = True
    min_chars: int = 80
    min_alpha_ratio: float = 0.6

class OcrPage(BaseModel):
    index: int = Field(..., description="0-based page index")
    text: str = Field(..., description="Extracted text for this page")


class OcrResponse(BaseModel):
    url: str = Field(..., description="Input URL (PDF or IIIF)")
    domain: Optional[str] = Field(None, description="Effective domain override (if provided)")
    model_name: Optional[str] = Field(None, description="Effective recognition model override (if provided)")
    segmenter: Optional[str] = Field(None, description="Effective segmenter override (if provided)")
    binarize: bool = Field(..., description="Whether nlbin binarization was applied")
    iiif_max_width: int = Field(..., description="Maximum width for IIIF image requests")
    pdf_dpi: int = Field(..., description="DPI used for PDF rasterization")
    pdf_text_enabled: bool = Field(..., description="Whether embedded PDF text extraction is enabled")
    pdf_text_min_chars: int = Field(..., description="Minimum characters required to accept embedded PDF text")
    pdf_text_min_alpha_ratio: float = Field(
        ..., description="Minimum alphabetic character ratio required to accept embedded PDF text"
    )
    pages: List[OcrPage] = Field(..., description="Per-page OCR/text output")


@router.get(
    "/ocr",
    response_model=OcrResponse,
    summary="OCR a URL (PDF or IIIF) and return text per page",
    description=(
        "Iterates pages from a PDF/IIIF URL and runs OCR. "
        "Output can be JSON, NDJSON (one JSON object per line), or ZIP. "
        "If `domain/model_name/segmenter` are omitted, defaults are resolved from YAML/ENV."
    ),
    responses={
        200: {
            "content": {
                "application/zip": {},
                "application/x-ndjson": {},
                "application/json": {},
            }
        }
    }
)
def ocr_url(
    url: str = Query(..., description="PDF or IIIF URL"),

    # Output mode
    # stream: bool = Query(
    #     False,
    #     description="If true, stream results as NDJSON (one line per page). If false, return a single JSON document.",
    # ),
    output: Literal["json", "ndjson", "zip"] = Query(
        "json",
        description="Output mode: json (single document), ndjson (one line per page), zip (metadata + pages as json/ndjson).",
    ),
    # Config resolution: param > ENV > YAML > fallback
    config_path: Optional[str] = Query(
        None,
        description="Path to YAML config. If omitted: ENV FTS_CONFIG, otherwise ./config.yml",
    ),
    domain: Optional[str] = Query(
        None,
        description="Domain override (e.g., print/handwriting/medieval). If omitted, resolved from YAML/ENV.",
    ),
    model_name: Optional[str] = Query(
        None,
        description="Recognition model name override. If omitted, resolved from YAML/ENV defaults for the domain.",
    ),
    segmenter: Optional[str] = Query(
        None,
        description='Segmenter override: "BLLA" (package fallback) or a model name from YAML.',
    ),

    # OCR behavior
    binarize: bool = Query(True, description="If true, apply nlbin binarization before segmentation/OCR."),

    # Input rendering controls
    iiif_max_width: int = Query(
        2000, ge=200, le=8000,
        description="Maximum width for IIIF images (scaling parameter).",
    ),
    pdf_dpi: int = Query(
        200, ge=72, le=600,
        description="DPI used to rasterize PDF pages.",
    ),

    # Embedded PDF text policy (matches your dataclass defaults)
    pdf_text_enabled: bool = Query(
        True,
        description="If true, attempt to use embedded PDF text when it looks reliable.",
    ),
    pdf_text_min_chars: int = Query(
        80, ge=0, le=100000,
        description="Minimum character count to accept embedded PDF text.",
    ),
    pdf_text_min_alpha_ratio: float = Query(
        0.6, ge=0.0, le=1.0,
        description="Minimum alphabetic ratio to accept embedded PDF text.",
    ),
) -> Union[OcrResponse, StreamingResponse, dict]:
    # Local imports to avoid heavy imports at app startup (and to match your earlier pattern).
    from .ocr import iter_pages, page_to_text

    pdf_text_policy = PdfTextPolicy(
        enabled=pdf_text_enabled,
        min_chars=pdf_text_min_chars,
        min_alpha_ratio=pdf_text_min_alpha_ratio,
    )

    def iter_page_results() -> Iterator[dict]:
        for item in iter_pages(
            url,
            iiif_max_width=iiif_max_width,
            pdf_dpi=pdf_dpi,
            pdf_text_policy=pdf_text_policy,
        ):
            text = page_to_text(
                item,
                config_path=config_path,
                domain=domain,
                model_name=model_name,
                segmenter=segmenter,
                binarize=binarize,
            )
            yield {"index": item.index, "text": text}

    try:
        if output == "ndjson":
            def gen():
                for obj in iter_page_results():
                    yield json.dumps(obj, ensure_ascii=False) + "\n"
            return StreamingResponse(gen(), media_type="application/x-ndjson; charset=utf-8")

        if output == "zip":
            import tempfile, zipfile
            tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
            tmp_path = tmp.name
            tmp.close()

            meta = {
                "url": url,
                "domain": domain,
                "model_name": model_name,
                "segmenter": segmenter,
                "binarize": binarize,
                "iiif_max_width": iiif_max_width,
                "pdf_dpi": pdf_dpi,
                "pdf_text_enabled": pdf_text_enabled,
                "pdf_text_min_chars": pdf_text_min_chars,
                "pdf_text_min_alpha_ratio": pdf_text_min_alpha_ratio,
            }

            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

                # pages as NDJSON inside zip (most efficient)
                lines = []
                for obj in iter_page_results():
                    lines.append(json.dumps(obj, ensure_ascii=False))
                zf.writestr("pages.ndjson", "\n".join(lines) + ("\n" if lines else ""))

            return FileResponse(
                tmp_path,
                media_type="application/zip",
                filename="ocr.zip",
            )

        # default: single JSON document
        pages: List[OcrPage] = [OcrPage(**obj) for obj in iter_page_results()]
        return OcrResponse(
            url=url,
            domain=domain,
            model_name=model_name,
            segmenter=segmenter,
            binarize=binarize,
            iiif_max_width=iiif_max_width,
            pdf_dpi=pdf_dpi,
            pdf_text_enabled=pdf_text_enabled,
            pdf_text_min_chars=pdf_text_min_chars,
            pdf_text_min_alpha_ratio=pdf_text_min_alpha_ratio,
            pages=pages,
        )

    except ValueError as e:
        # Typical examples: checksum mismatch, invalid config values, etc.
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR failed: {e}")


@router.get("/dev-test")
def dev_test_ingest():
    from .db import index_stream
    from .ocr import iter_pages, page_to_text
    logger.debug("d")
    # --- test input ---
    url = "https://dlib.biblhertz.it/iiif/khagenarna2403/manifest.json"
    doc_id = "dev-test-001"

    # --- OCR page generator (matches PagesFn) ---
    def pages_fn(u: str):
        for item in iter_pages(u):
            text = page_to_text(item)
            yield item.index, text

    # --- optional metadata (flat dict, string values) ---
    meta = {
        "source": "dev-test",
        "env": "local",
    }

    # --- call ingest ---
    run_id = index_stream(
        url=url,
        doc_id=doc_id,
        url_to_text_pages_fn=pages_fn,
        targets="ocr-pages-write", # alias or index
        meta=meta,
    )

    return {
        "status": "ok",
        "run_id": run_id,
        "url": url,
        "doc_id": doc_id,
        "targets": ["ocr-pages-write"],
    }



class OpenSearchDocRequest(BaseModel):
    doc_id: str | None = Field(default=None, description="Optional _id; generated if omitted")
    targets: str | list[str]
    doc: dict | list[dict]

@router.post("/opensearch")
def ingest_opensearch(req: OpenSearchDocRequest = Body(...)):
    from .db import index_stream

    run_id = index_stream(
        doc_id=req.doc_id,
        targets=req.targets,
        doc=req.doc,
    )
    return {"status": "ok", "run_id": run_id}

def _meta_flat_strings(d: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = str(v)
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out

JsonObj = Dict[str, Any]
JsonBody = Union[JsonObj, List[JsonObj]]

@router.post("/pipeline")
def ingest_route( # TODO add graph and set defaults per library: indices, sparql query --> see note parsing
    input: Optional[JsonBody] = Body(default=None),
    targets: str = Query(..., description="Index or alias"),
    ocr: bool = Query(False, description="If true, run OCR pages ingest via pages_fn"),
    # Fallback-Quelle, wenn input_json=None:
    q: Optional[str] = Query(default=None, description="SPARQL SELECT query (used when body is null)"),
    # store_path: Optional[str] = Query(default=None, description="Oxigraph store path (used when body is null)"),
):
    from .db import index_stream

    if input is None:
        if not q:
            raise HTTPException(
                status_code=400,
                detail="If body is null, you must provide 'q' query parameter",
            )

        # from pyoxigraph import Store
        # store = Store.read_only(store_path)
        from zotero_rdf_server.store import store
        from zotero_rdf_server.utils import load_text_like
        try:
            query=load_text_like(q,label="Ingest Pipeline SPARQL Query")
            items: List[Dict[str, Any]] = [
                {v: sol[v].value for v in sol}
                for sol in store.query(query, use_default_graph_as_union=True)
            ]
        except Exception as e:
            logger.error(f"Query failed: {e}")
            items = []
    else:
        
        if isinstance(input, str):
            from zotero_rdf_server.utils import load_dict_like
            input = load_dict_like(input,label="Ingest Pipeline Input")

        if isinstance(input, dict):
            items = [input]
        elif isinstance(input, list) and all(isinstance(x, dict) for x in input):
            items = input
        else:
            raise HTTPException(status_code=400, detail="Body must be a JSON object, a list of JSON objects, or null")



    pages_fn = None
    if ocr:
        from .ocr import iter_pages, page_to_text

        def pages_fn(u: str):
            for item in iter_pages(u):
                text = page_to_text(item)
                yield item.index, text
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    run_ids: List[str] = []

    for obj in items:
        payload = dict(obj)

        doc_id = payload.pop("_id", None)
        url = payload.pop("_url", None)
        text = payload.pop("_text", "")
        sequence = payload.pop("_idx", None)

        meta = _meta_flat_strings(payload)

        if ocr:
            if not url:
                raise HTTPException(status_code=400, detail="ocr=true requires '_url' in each item")

            run_ids.append(
                index_stream(
                    url=url,
                    doc_id=doc_id,
                    url_to_text_pages_fn=pages_fn,  # type: ignore[arg-type]
                    targets=targets,
                    meta=meta,
                )
            )
        else:
            d: Dict[str, Any] = {"ingest_ts": now, "meta": meta}
            if url is not None:
                d["url"] = url
            if doc_id is not None:
                d["doc_id"] = doc_id
            if sequence is not None:
                d["page"] = sequence
            if text != "":
                d["text"] = text

            run_ids.append(
                index_stream(
                    targets=targets,
                    doc_id=doc_id,
                    doc=d,
                )
            )

    return {
        "status": "ok",
        "run_ids": run_ids,
        "ocr_mode": ocr,
        "targets": [targets],
        "count": len(items),
    }