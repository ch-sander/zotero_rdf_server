from __future__ import annotations
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter, Body
from fastapi.responses import StreamingResponse, FileResponse
from typing import Literal, Any, Dict, Iterator, List, Optional, Union
from pathlib import Path
import json
from pydantic import BaseModel, Field
# from zotero_rdf_server.store import *
# from zotero_rdf_server.rdf import *
# from zotero_rdf_server.logging_config import logger, LogLevel
# from zotero_rdf_server.config import *
# from zotero_rdf_server.models import ZoteroLibrary
# from zotero_rdf_server.utils import *
from dataclasses import dataclass

from .helpers import plugin_logger
logger=plugin_logger()

router = APIRouter(tags=["FTS"])

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
    img_out: Optional[str] = Query(
        None,
        description="Relative directory (under EXPORT_DIRECTORY) to store page images.",
    ),
    txt_out: Optional[str] = Query(
        None,
        description="Relative directory (under EXPORT_DIRECTORY) to store page texts.",
    ),
    img_ext: str = Query(
        "jpg",
        description="Image file extension (jpg, png, webp, ...).",
    ),
    txt_ext: str = Query(
        "txt",
        description="Text file extension.",
    ),
    safe_text: str = Query(
        "skip",
        pattern="^(skip|overwrite|active)$",
        description="What to do if text file already exists.",
    ),
    safe_image: str = Query(
        "skip",
        pattern="^(skip|overwrite|active)$",
        description="What to do if image file already exists.",
    ),
    on_error: str = Query(
        "raise",
        pattern="^(log|raise|skip|empty)$",
        description="Behaviour if OCR/text extraction fails.",
    )
) -> Union[OcrResponse, StreamingResponse, dict]:
    # Local imports to avoid heavy imports at app startup (and to match your earlier pattern).
    from .ocr import iter_text_pages, PdfTextPolicy

    text_image_file_kwargs = {
        "img_out": img_out,
        "txt_out": txt_out,
        "img_ext": img_ext,
        "txt_ext": txt_ext,
        "safe_text": safe_text,
        "safe_image": safe_image,
        "on_error": on_error,
    }
    pdf_text_policy = PdfTextPolicy(
        enabled=pdf_text_enabled,
        min_chars=pdf_text_min_chars,
        min_alpha_ratio=pdf_text_min_alpha_ratio,
    )

    def iter_page_results() -> Iterator[dict]:
        for page_no, text in iter_text_pages(
            url,
            # doc_id=url          
            iter_kwargs=dict(
                iiif_max_width=iiif_max_width,
                pdf_dpi=pdf_dpi,
                pdf_text_policy=pdf_text_policy,
            ),
            page_to_text_kwargs=dict(
                config_path=config_path,
                domain=domain,
                model_name=model_name,
                segmenter=segmenter,
                binarize=binarize,
            ),
            text_image_file_kwargs=text_image_file_kwargs,
        ):
            yield {"index": page_no, "text": text}
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

JsonObj = Dict[str, Any]
JsonBody = Union[JsonObj, List[JsonObj]]

@router.post("/pipeline")
def ingest_route(
    input: Optional[JsonBody] = Body(default=None, examples=[None]),
    targets: str | list = Query(default=None, description="Index or alias"),
    ocr: bool = Query(None, description="If true, run OCR pages ingest via pages_fn"), 
    query: Optional[str] = Query(default=None, description="SPARQL SELECT query or path to file with query code (used when body is null)"),
    graph: str | None = Query(default=None, description="Named graph IRI containing the attachments or documents (optional)"),
    config_path: Optional[str] = Query(
        None,
        description="Path to YAML config. If omitted: ENV FTS_CONFIG, otherwise ./config.yml",
    ),
    store_path: Optional[str] = Query(default=None, description="Oxigraph store path (defaults to main store)"),
    open_search_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for Open Search Config", examples=[None]),
    ocr_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for OCR Config", examples=[None]),
    model_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for Kraken Config", examples=[None]),
    file_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for File Output", examples=[{'img_out':'kraken/images','txt_out':'kraken/texts','save_text':'active','save_image':'active'}]),
):
    from .pipeline import ingest_pipeline
    run_ids: List[str] = []

    if input is None:
        try:
            if store_path:
                logger.warning(f"Reading from store in {store_path}")
                from pyoxigraph import Store, NamedNode
                store = Store.read_only(store_path)
            else:
                from zotero_rdf_server.store import store, NamedNode
                from zotero_rdf_server.utils import load_text_like
                logger.warning(f"Reading from main store")
        except Exception as e:
            logger.error(f"Reading from main store failed: {e}")                        
            raise HTTPException(
                status_code=400,
                detail="Reading from store failed",
            )

        if graph or (graph is None and query is None):  # take one or multple graphs if no query given in API
            from zotero_rdf_server.store import get_graph
            checked_graph, all_graphs = get_graph(graph)
            if not checked_graph:
                raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")
            from zotero_rdf_server.models import ZoteroLibrary
            from zotero_rdf_server.config import ZOTERO_LIBRARIES_CONFIGS
            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)            
                if not graph or graph == lib.base_url:
                    logger.info(f"starting FTS pipeline for {lib.base_url}...")
                    
                    cfg = lib.plugin.get("fts") or [{}]
                    cfg = [cfg] if isinstance(cfg,dict) else cfg
                    if len(cfg)>1:
                        logger.warning(f"Running {len(cfg)} FTS configuration for library {lib.base_url}")
                    for ncfg in cfg: # allow multiple runs per library

                        os_cfg = open_search_kwargs if open_search_kwargs is not None else (ncfg.get("open-search") or {})
                        kraken_cfg = (ncfg.get("kraken") or {})
                        targets_x = targets or os_cfg.get("targets")

                        if not targets_x:
                            raise HTTPException(
                                status_code=400,
                                detail="Missing target indices/index",
                            )
                        
                        config_path_x = config_path or os_cfg.get("config_path")
                        query_x = query or cfg.get("query")

                        if not query_x:
                            raise HTTPException(
                                status_code=400,
                                detail="With no input, you must provide 'query' parameter",
                            )
                        
                        ocr_x = ocr if ocr is not None else cfg.get("ocr", False)

                        
                        iter_pages_kwargs = ocr_kwargs if ocr_kwargs is not None else dict(kraken_cfg.get("ocr_kwargs") or {})
                        page_to_text_kwargs = model_kwargs if model_kwargs is not None else dict(kraken_cfg.get("model_kwargs") or {})
                        text_image_file_kwargs = file_kwargs if file_kwargs is not None else dict(kraken_cfg.get("file_kwargs") or {})
                        
                        items = [] 

                        try:
                            sparql_query=load_text_like(query_x,label="Ingest Pipeline SPARQL Query")
                            logger.debug(f"{sparql_query}")
                            items = [
                                {v: sol[v].value for v in sol}
                                for sol in store.query(sparql_query, use_default_graph_as_union=False, default_graph=[NamedNode(lib.base_url), NamedNode(lib.knowledge_base_graph)])
                            ]
                            logger.debug(f"{len(items)} items found")       
                        except Exception as e:
                            logger.error(f"Query failed: {e}")
                            items = []
                        del store

                        
                        run_ids.append(ingest_pipeline(items=items,
                                                targets=targets_x, 
                                                ocr=ocr_x,
                                                iter_pages_kwargs=iter_pages_kwargs,
                                                page_to_text_kwargs=page_to_text_kwargs, text_image_file_kwargs=text_image_file_kwargs,
                                                config_path=config_path_x))
                    
                elif graph and graph != lib.base_url:
                    logger.debug(f"{graph} skipped")
                else:
                    logger.warning(f"{graph} not yet supported but defined via config")

        elif graph is None and query: # query directly
            if not targets:
                raise HTTPException(
                    status_code=400,
                    detail="Missing target indices/index",
                )
            logger.info("Starting FTS pipeline for entire store with query...")
            try:
                
                sparql_query=load_text_like(query,label="Ingest Pipeline SPARQL Query")
                logger.debug(f"{sparql_query}")
                bindings = store.query(sparql_query, use_default_graph_as_union=True)
                var_names = [v.value for v in bindings.variables]
                logger.info(f"{var_names}")
                items = []
                for sol in bindings:
                    items.append({
                        name: (sol[name].value if sol[name] is not None else None)
                        for name in var_names
                    })
                logger.debug(f"{items}")
            except Exception as e:
                logger.error(f"Query failed: {e}")
                raise HTTPException(
                    status_code=400,
                    detail="Query failed",
                )

            del store
            ocr = True if ocr is True else False

            run_ids.append(ingest_pipeline(items=items,
                                            targets=targets, 
                                            ocr=ocr,
                                            iter_pages_kwargs=ocr_kwargs,
                                            page_to_text_kwargs=model_kwargs,
                                            text_image_file_kwargs=file_kwargs,
                                            config_path=config_path))
        else:
            raise HTTPException(
                    status_code=400,
                    detail="Missing parameters for query!",
                )

    else:
        logger.warning(f"Input {input}")  # use given bindings
        if not targets:
            raise HTTPException(
                status_code=400,
                detail="Missing target indices/index",
            )
        items = []

        if isinstance(input, str):
            from zotero_rdf_server.utils import load_dict_like
            input = load_dict_like(input,label="Ingest Pipeline Input") # TODO not proper, yet, for lists (CSV should work)!
        if isinstance(input, dict):
            items = [input]
        elif isinstance(input, list) and all(isinstance(x, dict) for x in input):
            items = input
        else:
            raise HTTPException(status_code=400, detail="Body must be a JSON object, a list of JSON objects, or null")
        ocr = True if ocr is True else False
        run_ids.append(ingest_pipeline( items=items,
                                        targets=targets, 
                                        ocr=ocr,
                                        iter_pages_kwargs=ocr_kwargs,
                                        page_to_text_kwargs=model_kwargs,
                                        text_image_file_kwargs=file_kwargs,
                                        config_path=config_path))
    return {
        "status": "ok",
        "run_ids": run_ids,
        "ocr_mode": ocr,
        "targets": targets,
        "runs": len(run_ids),
    }