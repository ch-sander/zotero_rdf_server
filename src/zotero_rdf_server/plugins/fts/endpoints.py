from __future__ import annotations
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter, Body, Depends
from fastapi.responses import StreamingResponse, FileResponse, Response, JSONResponse, PlainTextResponse
from typing import Literal, Any, Dict, Iterator, List, Optional, Union
from pathlib import Path
import json, io
from pydantic import BaseModel, Field
# from zotero_rdf_server.store import *
# from zotero_rdf_server.rdf import *
# from zotero_rdf_server.logging_config import logger, LogLevel
# from zotero_rdf_server.config import *
# from zotero_rdf_server.models import ZoteroLibrary
# from zotero_rdf_server.utils import *

from .helpers import plugin_logger
logger=plugin_logger()

router = APIRouter()
open_router = APIRouter()

class OcrPage(BaseModel):
    index: int = Field(..., description="0-based page index")
    text: str = Field(..., description="Extracted text for this page")


class OcrResponse(BaseModel):
    input: str = Field(..., description="Input URL/path (PDF or IIIF)")
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
    start_page:  int = Field(..., description="Start page for OCR")
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
    input: str = Query(..., description="PDF or IIIF URL or file path"),

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
    start_page: int = Query(
        1, ge=1,
        description="Page OCR starts on (for debugging mainly). Defaults to 1",
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
            input=input,
            # doc_id=input          
            iter_kwargs=dict(
                iiif_max_width=iiif_max_width,
                pdf_dpi=pdf_dpi,
                pdf_text_policy=pdf_text_policy,
                start_page=start_page
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
                "input": input,
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
            input=input,
            domain=domain,
            model_name=model_name,
            segmenter=segmenter,
            binarize=binarize,
            iiif_max_width=iiif_max_width,
            pdf_dpi=pdf_dpi,
            pdf_text_enabled=pdf_text_enabled,
            pdf_text_min_chars=pdf_text_min_chars,
            pdf_text_min_alpha_ratio=pdf_text_min_alpha_ratio,
            start_page=start_page,
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

    run = index_stream(
        doc_id=req.doc_id,
        targets=req.targets,
        doc=req.doc,
    )
    return run #{"status": "ok", "run_id": run_id}

def _default_filename(prefix: str, ext: str) -> str:
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}.{ext}"

JsonObj = Dict[str, Any]
JsonBody = Union[JsonObj, List[JsonObj]]

@router.post("/pipeline")
def ingest_route(
    input: Optional[JsonBody] = Body(default=None, examples=[None]),
    targets: str | list = Query(default=None, description="Index or alias"),    
    ocr: bool = Query(default=True, description="If true, run OCR pages ingest via pages_fn"),
    vector: bool = Query(default=True, description="If true, vectorizes text with sentence transformer (1024 dimensions)"),
    transformer: bool = Query(None, description="If true, run transformer pipeline (doi:10.3390/electronics14153083)"),
    ingest: bool = Query(default=True, description="If true, ingest into Open Search"),
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
    file_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for File Output", examples=[{'img_out':'kraken/images','txt_out':'kraken/texts','save_text':'active','save_image':'skip'}]),
):
    from .pipeline import ingest_pipeline
    import csv
    run_ids = []

    try:
        from zotero_rdf_server.config import EXPORT_DIRECTORY
        export_dir = Path(EXPORT_DIRECTORY) / "fts"
    except Exception:
        export_dir = Path()  / "fts"
    export_dir.mkdir(parents=True, exist_ok=True)

    def save_query_to_file(items, var_names=None, json_mode=True, csv_mode=True):
        try:
            if json_mode:
                filename = _default_filename("query_results", "json")
                with open(export_dir / filename, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2)
                logger.info(f"Saved query to {filename}")
            if csv_mode:
                if var_names is None:
                    var_names = sorted({k for row in items for k in row.keys()})
                filename = _default_filename("query_results", "csv")
                with open(export_dir / filename, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=var_names)
                    writer.writeheader()
                    writer.writerows(items)
                logger.info(f"Saved query to {filename}")
        except Exception as e:
            logger.exception(f"Failed to save query")

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
            targets_set = []
            from zotero_rdf_server.store import get_graph
            checked_graph, all_graphs = get_graph(graph)
            if graph and not checked_graph:
                raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")
            from zotero_rdf_server.models import ZoteroLibrary
            from zotero_rdf_server.config import ZOTERO_LIBRARIES_CONFIGS
            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)            
                if not graph or graph == lib.base_url:
                    logger.info(f"starting FTS pipeline for {lib.base_url}...")
                    
                    cfg = lib.plugin.get("fts") or []
                    cfg = [cfg] if isinstance(cfg,dict) else cfg
                    if len(cfg)>1:
                        logger.warning(f"Running {len(cfg)} FTS configuration for library {lib.base_url}")
                    # if not cfg:
                    #     raise HTTPException(status_code=400, detail=f"No FTS config for library {lib.base_url}")
                    for ncfg in cfg: # allow multiple runs per library

                        os_cfg = open_search_kwargs if open_search_kwargs is not None else (ncfg.get("open-search") or {})
                        kraken_cfg = (ncfg.get("kraken") or {})
                        targets_x = targets or os_cfg.get("targets")
                        if isinstance(targets_x, list):
                            targets_set.extend(targets_x)
                        else:
                            targets_set.append(targets_x)

                        if not targets_x:
                            raise HTTPException(
                                status_code=400,
                                detail="Missing target indices/index",
                            )
                        
                        config_path_x = config_path or os_cfg.get("config_path")
                        query_x = query or ncfg.get("query")

                        if not query_x:
                            raise HTTPException(
                                status_code=400,
                                detail="With no input, you must provide 'query' parameter",
                            )
                        
                        ocr_x = ocr if ocr is not None else ncfg.get("ocr", True)
                        transformer_x = transformer if transformer is not None else ncfg.get("transformer", False)
                        ingest_x = ingest if ingest is not None else ncfg.get("ingest", True)
                        vector_x = vector if vector is not None else ncfg.get("vector", True)

                        iter_pages_kwargs = ocr_kwargs if ocr_kwargs is not None else dict(kraken_cfg.get("ocr_kwargs") or {})
                        page_to_text_kwargs = model_kwargs if model_kwargs is not None else dict(kraken_cfg.get("model_kwargs") or {})
                        text_image_file_kwargs = file_kwargs if file_kwargs is not None else dict(kraken_cfg.get("file_kwargs") or {})
                        
                        items = [] 

                        try:
                            sparql_query=load_text_like(query_x,label="Ingest Pipeline SPARQL Query")
                            logger.debug(f"{sparql_query}")
                            bindings = store.query(
                                sparql_query,
                                use_default_graph_as_union=False,
                                default_graph=[NamedNode(lib.base_url), NamedNode(lib.knowledge_base_graph)])
                            var_names = [v.value for v in bindings.variables]
                            logger.info(f"SPARQL returned columns: {var_names}")
                            for sol in bindings:
                                items.append({
                                    name: (sol[name].value if sol[name] is not None else None)
                                    for name in var_names
                                })                            
                            logger.info(f"{len(items)} results")  

                            # Save as CSV
                            save_query_to_file(items=items,var_names=var_names)

                        except Exception as e:
                            logger.error(f"Query failed: {e}")
                            items = []
                        del store

                        
                        run_ids.extend(ingest_pipeline(items=items,
                                                targets=targets_x, 
                                                ocr=ocr_x,
                                                transformer=transformer_x,
                                                vector=vector_x,
                                                ingest=ingest_x,
                                                iter_pages_kwargs=iter_pages_kwargs,
                                                page_to_text_kwargs=page_to_text_kwargs, text_image_file_kwargs=text_image_file_kwargs,
                                                config_path=config_path_x))
                    
                elif graph and graph != lib.base_url:
                    logger.debug(f"{lib.base_url} skipped")
                else:
                    logger.warning(f"{graph} not yet supported but defined via config")

            targets=list(set(targets_set))

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
                logger.debug(f"{items} results")
                save_query_to_file(items=items,var_names=var_names)
            except Exception as e:
                logger.error(f"Query failed: {e}")
                raise HTTPException(
                    status_code=400,
                    detail="Query failed",
                )

            del store
            ocr = True if ocr is True else False

            run_ids.extend(ingest_pipeline(items=items,
                                            targets=targets, 
                                            ocr=ocr,
                                            transformer=transformer,
                                            vector=vector,
                                            ingest=ingest,
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
        save_query_to_file(items=items,var_names=var_names, json_mode=False)

        run_ids.extend(ingest_pipeline( items=items,
                                        targets=targets, 
                                        ocr=ocr,
                                        transformer=transformer,
                                        vector=vector,
                                        ingest=ingest,
                                        iter_pages_kwargs=ocr_kwargs,
                                        page_to_text_kwargs=model_kwargs,
                                        text_image_file_kwargs=file_kwargs,
                                        config_path=config_path))
    
    result = {
        "status": "ok",
        "run_ids": run_ids,
        "targets": list(targets),
        "runs": len(run_ids),
    }    
    try:
        _runs_filename = _default_filename("runs_result", "json")
        with open(export_dir / _runs_filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Couldn't save file: {e}")

    return result

### SEARCHES ###
MAX_SIZE = 2000

class OutputHeader(BaseModel):
    header_json: Optional[Dict[str, Any]] = None
    header_md: Optional[str] = None
    header_kv: Optional[Dict[str, str]] = None

def output_header_params(
    header_json: Optional[str] = Query(
        None,
        description=(
            "JSON string to be embedded as a codeblock in the Markdown header."
            "Example: {\"created by\":\"Zotero RDF\",\"job_id\":\"123\"}"
        ),
    ),
    header_md: Optional[str] = Query(
        None,
        description="Raw Markdown appended to the Markdown header.",
    ),
    header_kv: Optional[List[str]] = Query(
        None,
        description="Repeatable key:value pairs. Example: header_kv=createdBy:Zotero RDF&header_kv=job:123",
    ),
) -> OutputHeader:
    parsed_json = None
    if header_json:
        try:
            parsed_json = json.loads(header_json)
        except Exception as e:
            logger.error(f"Invalid header_json: {e}")

    kv: Dict[str, str] = {}
    if header_kv:
        for item in header_kv:
            if ":" in item:
                k, v = item.split(":", 1)
                k, v = k.strip(), v.strip()
                if k:
                    kv[k] = v

    return OutputHeader(header_json=parsed_json, header_md=header_md, header_kv=kv or None)

def make_header(out_header:OutputHeader):
    meta: Dict[str, Any] = {}
    if out_header.header_json:
        meta.update(out_header.header_json)
    if out_header.header_kv:
        meta.update(out_header.header_kv)
    return meta

class KeywordFilter(BaseModel):
    filter_field: Optional[str] = None
    filter_value: Optional[str] = None
    filter_values: Optional[List[str]] = None

def keyword_filter_params(
    filter_field: Optional[str] = Query(
        None,
        description="Keyword field to filter on (e.g. meta.parent_key)"
    ),
    filter_value: Optional[str] = Query(
        None,
        description="Single exact filter value"
    ),
    filter_values: Optional[str] = Query(
        None,
        description="CSV list of exact filter values"
    ),
) -> KeywordFilter:

    values = None
    if filter_values:
        values = [v.strip() for v in filter_values.split(",") if v.strip()]

    return KeywordFilter(
        filter_field=filter_field,
        filter_value=filter_value,
        filter_values=values,
    )

def format_search_response(
    *,
    resp: Dict[str, Any],
    debug_query: Dict[str, Any],
    output_format: str = "json",
    columns: Optional[str] = None,
    include_debug: bool = False,
    filename: Optional[str] = None,
    flatten_meta: bool = True,
    keep_meta: bool = False,
    include_aggs: bool = True,                 # include aggregations when present
    keep_highlight: bool = True,
    make_snippet: bool = False,
    highlight_field: Optional[str] = None,     # preferred highlight field for snippet selection
    truncate_chars: int = 0,                   # fallback snippet truncation if no highlight
    truncate_field: str = "text",              # fallback source field for truncation
    md_highlight_pre: str = "**",              # markdown highlight pre tag
    md_highlight_post: str = "**",             # markdown highlight post tag
    markdown_title: str = "Search Results",    # allow custom title in markdown
    markdown_max_rows: int = MAX_SIZE,               # cap markdown output
    api_call: Optional[str] = None,
    header_meta: Optional[Dict[str, Any]] = None,
    header_md_extra: Optional[str] = None,
):
    """
    Return search results as JSON or as downloadable CSV/Markdown file.
    """
    from .search import (
        normalize_hits,
        collect_columns,
        render_csv,
        render_markdown,
        render_markdown_query_header,
    )

    normalized = normalize_hits(
        resp,
        flatten_meta=flatten_meta,
        keep_meta=keep_meta,
        keep_highlight=keep_highlight,
        make_snippet=make_snippet,
        highlight_field=highlight_field,
        truncate_chars=truncate_chars,
        truncate_field=truncate_field,
    )

    rows = normalized.get("hits", [])
    aggs = normalized.get("aggregations") if include_aggs else None

    preferred_cols = None
    if columns:
        preferred_cols = [c.strip() for c in columns.split(",") if c.strip()]

    cols = collect_columns(
        rows,
        preferred=preferred_cols
        or ["_id", "_score", "doc_id", "source", "page", "snippet", "ingest_ts"],
    )
    if output_format in ("md", "markdown"):
        cols = [c for c in cols if c != "highlight"]
    # --- JSON (inline, not a file) -------------------------------------------
    if output_format == "json":
        payload: Dict[str, Any] = {"total": normalized.get("total"), "hits": rows}
        if header_meta:
            payload["meta"] = header_meta
        if include_debug and api_call:
            payload["api_call"] = api_call
        if aggs is not None:
            payload["aggregations"] = aggs
        if include_debug:
            payload["debug_query"] = debug_query
        return JSONResponse(payload)

    # --- CSV download --------------------------------------------------------
    if output_format == "csv":
        content = render_csv(rows, cols)
        stream = io.BytesIO(content.encode("utf-8"))

        return StreamingResponse(
            stream,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename or _default_filename("search", "csv")}"'
                )
            },
        )

    # --- Markdown download ---------------------------------------------------
    if output_format in ("md", "markdown"):
        header = render_markdown_query_header(debug_query)

        if header_meta:
            pretty_meta = json.dumps(header_meta, indent=2, ensure_ascii=False)
            header += (
                "## Metadata\n\n"
                "```json\n"
                f"{pretty_meta}\n"
                "```\n\n"
            )

        if header_md_extra:
            header += header_md_extra.rstrip() + "\n\n"

        if aggs is not None:
            pretty_aggs = json.dumps(aggs, indent=2, ensure_ascii=False)
            header += (
                "## Aggregations\n\n"
                "```json\n"
                f"{pretty_aggs}\n"
                "```\n\n"
            )

        if include_debug and api_call:
            header += (
                "## API Call\n\n"
                "```text\n"
                f"{api_call}\n"
                "```\n\n"
            )
            
        body = render_markdown(
            rows,
            cols,
            max_rows=markdown_max_rows,
            title=markdown_title,
        )

        content = header + body

        # Only convert <em> tags if caller enabled highlight/snippets
        if keep_highlight or make_snippet:
            content = content.replace("<em>", md_highlight_pre).replace("</em>", md_highlight_post)

        stream = io.BytesIO(content.encode("utf-8"))
        return StreamingResponse(
            stream,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename or _default_filename("search", "md")}"'
            },
        )

    raise HTTPException(status_code=400, detail="Invalid format. Use: json, csv, md.")

from enum import Enum

class OutputFormat(str, Enum):
    json = "json"
    csv = "csv"
    markdown = "markdown"

class AggType(str, Enum):
    terms = "terms"
    composite = "composite"

@open_router.get(
    "/search/terms",
    summary="Search comma-separated terms (OR) with phrase/prefix/fuzzy modes",
    description=(
        "Search for any of the comma-separated expressions in the given field. "
        "Supports analyzed phrase match (match_phrase), phrase prefix (match_phrase_prefix), and fuzzy match."
    ),
    tags=["Search"]
)
def search_terms(
    request: Request,
    index: str = Query(
        None,
        description="Name of the OpenSearch index to query. Default alias can be set in cofiguration.",
    ),
    q: str = Query(
        ...,
        description="Search query. Comma-separated expressions unless lucene=true.",
    ),
    field: str = Query(
        "text",
        description="Analyzed text field to search in. Defaults to 'text'.",
    ),

    exact: bool = Query(
        True,
        description="Enable match_phrase (analyzed phrase match with slop).",
    ),
    truncated: bool = Query(
        True,
        description="Enable match_phrase_prefix (prefix matching on last token).",
    ),
    fuzzy: bool = Query(
        True,
        description="Enable fuzzy matching (edit distance depending on token length).",
    ),

    size: int = Query(
        10,
        ge=1,
        le=MAX_SIZE,
        description=(
            f"Number of documents returned in hits.hits (top-level hits). "
            f"Hard-capped at {MAX_SIZE} for cluster safety. "
            "Does NOT limit aggregation bucket counts."
        ),
    ),

    offset: int = Query(
        0,
        ge=0,
        description=(
            "Starting offset for hits.hits via 'from'. "
            "Use together with 'size' for basic pagination. "
            "Does NOT affect aggregations."
        ),
    ),

    lucene: bool = Query(
        False,
        description="Interpret 'q' as a Lucene query_string instead of comma-separated terms.",
    ),
    lucene_lenient: bool = Query(
        True,
        description="Enable lenient parsing for query_string.",
    ),

    highlight: bool = Query(
        True,
        description="Include OpenSearch highlight snippets in the response.",
    ),
    highlight_field: Optional[str] = Query(
        None,
        description="Field to highlight. Defaults to the search field.",
    ),
    fragment_size: int = Query(
        160,
        ge=20,
        le=500,
        description="Size of each highlight fragment.",
    ),
    fragments: int = Query(
        2,
        ge=0,
        le=10,
        description="Number of highlight fragments to return (0 disables fragments).",
    ),

    pre_tag: str = Query(
        "**",
        description="Markdown prefix for highlighted terms.",
    ),
    post_tag: str = Query(
        "**",
        description="Markdown suffix for highlighted terms.",
    ),

    truncate_chars: int = Query(
        0,
        ge=0,
        le=5000,
        description="If >0: truncate plain text when no highlight is present.",
    ),

    agg_field: Optional[str] = Query(
        None,
        description=(
            "If set, compute an aggregation over this field and return it under "
            "'aggregations.by_field'. "
            "Use a keyword field (e.g. meta.parent_key). "
            "Aggregation does NOT group hits.hits."
        ),
    ),

    agg_size: int = Query(
        10,
        ge=1,
        le=1000,
        description=(
            "Number of aggregation buckets (groups) to return. "
            "For agg_type=terms: top-N buckets by doc_count. "
            "For agg_type=composite: buckets per page."
        ),
    ),

    # agg_type: AggType = Query(
    #     AggType.terms,
    #     description=(
    #         "Aggregation type. "
    #         "'terms' returns top buckets sorted by doc_count. "
    #         "'composite' supports bucket pagination (after_key handling not implemented here)."
    #     ),
    # ),
    out_header: OutputHeader = Depends(output_header_params),
    format: OutputFormat = Query(
        OutputFormat.json,
        description=(
            "Response format. "
            "json: structured API response. "
            "csv: flat table from hits.hits. "
            "md/markdown: human-readable document blocks from hits.hits."
        ),
    ),

    columns: Optional[str] = Query(
        None,
        description="Comma-separated list of fields to include from _source.",
    ),
    filters: KeywordFilter = Depends(keyword_filter_params),
    debug: bool = Query(
        False,
        description="Include generated OpenSearch query in the response.",
    ),
):
    from .search import parse_csv, build_terms_should_queries, os_search, apply_paging, apply_keyword_filter
    api_call = str(request.url) 
    # --- Build query ---------------------------------------------------------
    if lucene:
        # TODO safer for end-user input would be simple_query_string
        body: Dict[str, Any] = {
            "query": {
                "query_string": {
                    "query": q,
                    "default_field": field,
                    "lenient": lucene_lenient,
                }
            }
        }
    else:
        try:
            terms = parse_csv(q)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if not (exact or truncated or fuzzy):
            raise HTTPException(status_code=400, detail="Enable at least one mode: exact/truncated/fuzzy.")

        should = build_terms_should_queries(
            terms=terms,
            field=field,
            exact=exact,
            truncated=truncated,
            fuzzy=fuzzy,
        )
        body = {"query": {"bool": {"should": should, "minimum_should_match": 1}}}
        
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size, offset=offset)

    # --- Highlight -----------------------------------------------------------
    if highlight:
        hf = highlight_field or field
        body["highlight"] = {
            "pre_tags": ["<em>"],
            "post_tags": ["</em>"],
            "fields": {
                hf: {
                    "fragment_size": fragment_size,
                    "number_of_fragments": fragments,
                }
            },
            "require_field_match": False,
        }

    # --- Aggregation ---------------------------------------------------------
    if agg_field:
        agg_type="terms" #  pinning parameter
        if agg_type == AggType.terms:
            body["aggs"] = {
                "by_field": {
                    "terms": {"field": agg_field, "size": agg_size}
                }
            }
        else:
            # composite for paging buckets; pass "after" separately for paging
            body["aggs"] = {
                "by_field": {
                    "composite": {
                        "size": agg_size,
                        "sources": [{"v": {"terms": {"field": agg_field}}}],
                    }
                }
            }

    source_columns = columns
    render_columns = columns
    if highlight or truncate_chars > 0:
        if render_columns:
            cols = [c.strip() for c in render_columns.split(",") if c.strip()]
            if "snippet" not in cols:
                cols.insert(0, "snippet")
            render_columns = ",".join(cols)
        else:
            render_columns = None

    try:
        resp = os_search(index=index, body=body, columns=source_columns)
        if resp.get("hits", {}).get("hits"):
            h0 = resp["hits"]["hits"][0]
            logger.info("First hit keys: %s", list(h0.keys()))
            logger.info("First hit highlight: %s", h0.get("highlight"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")
    
    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        debug_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=render_columns,
        include_debug=debug,
        flatten_meta=True,
        keep_meta=False,
        include_aggs=True,
        keep_highlight=True,
        make_snippet=highlight,
        highlight_field=highlight_field or field,
        truncate_chars=truncate_chars,
        truncate_field=field,
        md_highlight_pre=pre_tag,
        md_highlight_post=post_tag,
        markdown_max_rows=size,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
    )

@open_router.get(
    "/search/proximity",
    summary="Proximity search between two CSV lists (A x B) using intervals",
    description=(
        "Search for any pair (ai, bj) where ai is near bj within a token gap window. "
        "Supports match/prefix/fuzzy via intervals."
    ),
    tags=["Search"]
)
def search_proximity(
    request: Request,
    index: str = Query(None, description="OpenSearch index name. Default alias can be set in cofiguration"),
    a: str = Query(..., description="CSV list A"),
    b: str = Query(..., description="CSV list B"),
    field: str = Query("text", description="Text field to search"),
    proximity: int = Query(5, ge=0, le=50, description="Max token gaps between A and B"),
    ordered: bool = Query(False, description="If true, enforce A then B order"),
    allow_match: bool = Query(True),
    allow_prefix: bool = Query(True),
    allow_fuzzy: bool = Query(True),
    fuzzy_edits: int = Query(1, ge=0, le=2),
    size: int = Query(10, ge=1, le=1000),
    filters: KeywordFilter = Depends(keyword_filter_params),
    format: OutputFormat = Query(
        OutputFormat.json,
        description=(
            "Response format. "
            "json: structured API response. "
            "csv: flat table from hits.hits. "
            "md/markdown: human-readable document blocks from hits.hits."
        ),
    ),    
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    debug: bool = Query(False, description="Include debug_query (JSON only)"),
):
    from .search import parse_csv, build_proximity_intervals_query, os_search, apply_paging, apply_keyword_filter
    api_call = str(request.url) 
    try:
        list_a = parse_csv(a)
        list_b = parse_csv(b)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not (allow_match or allow_prefix or allow_fuzzy):
        raise HTTPException(status_code=400, detail="Enable at least one mode: match/prefix/fuzzy.")

    body = build_proximity_intervals_query(
        list_a=list_a,
        list_b=list_b,
        field=field,
        proximity=proximity,
        ordered=ordered,
        allow_match=allow_match,
        allow_prefix=allow_prefix,
        allow_fuzzy=allow_fuzzy,
        fuzzy_edits=fuzzy_edits,
    )
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")
    
    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        debug_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_debug=debug,
        markdown_max_rows=size,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
    )

@open_router.get(
    "/search/knn/by-id",
    summary="Vector k-NN similarity search by reference document _id",
    description="Fetches the vector from a reference document and runs a k-NN search to find similar documents.",
    tags=["Search"]
)
def knn_by_id(
    request: Request,
    index: str = Query(None, description="OpenSearch index name. Default alias can be set in cofiguration"),
    os_id: str = Query(..., description="Reference document OpenSearch _id"),
    vector_field: str = Query("vector", description="knn_vector field name"),
    k: int = Query(50, ge=1, le=10000),
    size: int = Query(20, ge=1, le=1000),
    ef_search: Optional[int] = Query(None, ge=1),
    exclude_self: bool = Query(True),
    filters: KeywordFilter = Depends(keyword_filter_params),
    format: OutputFormat = Query(
        OutputFormat.json,
        description=(
            "Response format. "
            "json: structured API response. "
            "csv: flat table from hits.hits. "
            "md/markdown: human-readable document blocks from hits.hits."
        ),
    ),    
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    debug: bool = Query(False, description="Include debug_query (JSON only)"),
):
    from .search import get_doc_vector, os_search, apply_paging, apply_keyword_filter
    api_call = str(request.url) 
    try:
        query_vec = get_doc_vector(index=index, os_id=os_id, vector_field=vector_field)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not fetch vector for doc {os_id}: {e}")
    
    if not query_vec:
        raise HTTPException(404, detail="Reference doc has no vector")
    
    knn_inner = {"vector": query_vec, "k": k}
    if ef_search is not None:
        knn_inner["method_parameters"] = {"ef_search": ef_search}

    knn_query = {"knn": {vector_field: knn_inner}}
    if exclude_self:
        query = {
            "bool": {
                "must": [knn_query],
                "must_not": [{"ids": {"values": [os_id]}}],
            }
        }
    else:
        query = knn_query

    body: Dict[str, Any] = {"query": query}

    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")
    
    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        debug_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_debug=debug,
        markdown_max_rows=size,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
    )

@open_router.get(
    "/search/mlt/by-id",
    summary="Token similarity search by reference document _id (More Like This)",
    description="Runs a More Like This query using a reference document to find token-similar documents.",
    tags=["Search"]
)
def mlt_by_id(
    request: Request,
    index: str = Query(None, description="OpenSearch index name. Default alias can be set in cofiguration"),
    os_id: str = Query(..., description="Reference document OpenSearch _id"),
    fields: str = Query("text", description="CSV list of fields, typically 'text'"),
    min_term_freq: int = Query(1, ge=0),
    min_doc_freq: int = Query(1, ge=0),
    max_query_terms: int = Query(25, ge=1, le=100),
    minimum_should_match: str = Query("30%", description="e.g. '30%' or '2'"),
    size: int = Query(20, ge=1, le=1000),
    exclude_self: bool = Query(True),
    filters: KeywordFilter = Depends(keyword_filter_params),
    format: OutputFormat = Query(
        OutputFormat.json,
        description=(
            "Response format. "
            "json: structured API response. "
            "csv: flat table from hits.hits. "
            "md/markdown: human-readable document blocks from hits.hits."
        ),
    ),    
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    debug: bool = Query(False, description="Include debug_query (JSON only)"),
    
):
    from .search import os_search, apply_paging, apply_keyword_filter
    api_call = str(request.url) 
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    if not field_list:
        raise HTTPException(status_code=400, detail="No fields provided.")

    mlt_query: Dict[str, Any] = {
        "more_like_this": {
            "fields": field_list,
            "like": [{"_index": index, "_id": os_id}],
            "min_term_freq": min_term_freq,
            "min_doc_freq": min_doc_freq,
            "max_query_terms": max_query_terms,
            "minimum_should_match": minimum_should_match,
        }
    }

    if exclude_self:
        query = {"bool": {"must": [mlt_query], "must_not": [{"ids": {"values": [os_id]}}]}}
    else:
        query = mlt_query
    body = {"query": query}

    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")
    
    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        debug_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_debug=debug,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
    )

@open_router.get(
    "/search/mlt/by-text",
    summary="Token similarity search by free text (More Like This)",
    description="Runs a More Like This query using input text to find token-similar documents.",
    tags=["Search"]
)
def mlt_by_text(
    request: Request,
    index: str = Query(None, description="OpenSearch index name. Default alias can be set in configuration"),
    like_text: str = Query(..., description="Reference text, sentence, or paragraph"),
    fields: str = Query("text", description="CSV list of fields, typically 'text'"),
    min_term_freq: int = Query(1, ge=0),
    min_doc_freq: int = Query(1, ge=0),
    max_query_terms: int = Query(25, ge=1, le=100),
    minimum_should_match: str = Query("30%", description="e.g. '30%' or '2'"),
    size: int = Query(20, ge=1, le=1000),
    filters: KeywordFilter = Depends(keyword_filter_params),
    format: OutputFormat = Query(
        OutputFormat.json,
        description=(
            "Response format. "
            "json: structured API response. "
            "csv: flat table from hits.hits. "
            "md/markdown: human-readable document blocks from hits.hits."
        ),
    ),
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    debug: bool = Query(False, description="Include debug_query (JSON only)"),
):
    from .search import os_search, apply_paging, apply_keyword_filter

    api_call = str(request.url)
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    if not field_list:
        raise HTTPException(status_code=400, detail="No fields provided.")

    body: Dict[str, Any] = {
        "query": {
            "more_like_this": {
                "fields": field_list,
                "like": [like_text],
                "min_term_freq": min_term_freq,
                "min_doc_freq": min_doc_freq,
                "max_query_terms": max_query_terms,
                "minimum_should_match": minimum_should_match,
            }
        }
    }

    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")

    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        debug_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_debug=debug,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
    )