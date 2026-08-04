from __future__ import annotations
from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter, Body, Depends, status
from fastapi.responses import StreamingResponse, FileResponse, Response, JSONResponse, PlainTextResponse, HTMLResponse, RedirectResponse
from typing import Literal, Any, Dict, Iterator, List, Optional, Union, Tuple
from pathlib import Path
import json, io, html, os
from pydantic import BaseModel, Field
from .helpers import plugin_logger, safe_doc_id
logger=plugin_logger()

from zotero_rdf_server.config import OS_MAX_SIZE as MAX_SIZE

# MAX_SIZE = 2000000

router = APIRouter()
open_router = APIRouter()

@open_router.get("test-url-kinds")
def test_url_kinds():
    from dataclasses import asdict
    from .tests import test_url_kind_detection

    results = test_url_kind_detection(verbose=True)
    passed = sum(result.passed for result in results)

    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": [asdict(result) for result in results],
    }

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

    output: Literal["json", "ndjson", "zip"] = Query(
        "json",
        description="Output mode: json (single document), ndjson (one line per page), zip (metadata + pages as json/ndjson).",
    ),
    config_path: Optional[str] = Query(
        None,
        description="Path to YAML config. If omitted: ENV FTS_CONFIG, otherwise ./config.yml",
    ),

    framework: Literal["kraken", "tesseract", "transformer", "source", "none"] = Query(
        "kraken",
        description="OCR backend: kraken, tesseract, or transformer. Choose 'none' to skip ATR/OCR",
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

    tesseract_lang: Optional[str] = Query(
        "lat",
        description="Tesseract language override, e.g. deu, eng, deu+eng.",
    ),
    tesseract_config: Optional[str] = Query(
        "--oem 3",
        description='Extra Tesseract config, e.g. "--psm 6 --oem 3".',
    ),

    binarize: bool = Query(True, description="If true, apply nlbin binarization before segmentation/OCR."),

    iiif_max_width: int = Query(
        2000, ge=200, le=8000,
        description="Maximum width for IIIF images (scaling parameter).",
    ),
    pdf_dpi: int = Query(
        200, ge=72, le=600,
        description="DPI used to rasterize PDF pages.",
    ),
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
    meta_out: Optional[str] = Query(
        None,
        description="Relative directory (under EXPORT_DIRECTORY) to store metadata report as JSON.",
    ),
    img_ext: str = Query(
        "jpg",
        description="Image file extension (jpg, png, webp, ...).",
    ),
    txt_ext: str = Query(
        "txt",
        description="Text file extension.",
    ),
    save_text: Literal["skip", "overwrite", "active"] = Query(
        "skip",
        description="What to do if text file already exists.",
    ),
    save_image: Literal["skip", "cache", "overwrite", "active", "smart", "sniff"] = Query(
        "skip",
        description="What to do if image file already exists.",
    ),
    on_error: Literal["log", "raise", "skip", "empty"] = Query(
        "raise",
        description="Behaviour if OCR/text extraction fails.",
    )
) -> Union[OcrResponse, StreamingResponse, dict]:
    from .ocr import iter_text_pages, PdfTextPolicy

    text_image_file_kwargs = {
        "img_out": img_out,
        "txt_out": txt_out,
        "meta_out": meta_out,
        "img_ext": img_ext,
        "txt_ext": txt_ext,
        "save_text": save_text,
        "save_image": save_image,
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
                tesseract_lang=tesseract_lang,
                tesseract_config=tesseract_config,
            ),
            text_image_file_kwargs=text_image_file_kwargs,
            framework=framework,
            yield_result=True
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
                "framework": framework,
                "domain": domain,
                "model_name": model_name,
                "segmenter": segmenter,
                "tesseract_lang": tesseract_lang,
                "tesseract_config": tesseract_config,
                "binarize": binarize,
                "iiif_max_width": iiif_max_width,
                "pdf_dpi": pdf_dpi,
                "pdf_text_enabled": pdf_text_enabled,
                "pdf_text_min_chars": pdf_text_min_chars,
                "pdf_text_min_alpha_ratio": pdf_text_min_alpha_ratio,
            }

            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

                lines = []
                for obj in iter_page_results():
                    lines.append(json.dumps(obj, ensure_ascii=False))
                zf.writestr("pages.ndjson", "\n".join(lines) + ("\n" if lines else ""))

            return FileResponse(
                tmp_path,
                media_type="application/zip",
                filename="ocr.zip",
            )

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
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR failed: {e}")

class OpenSearchDocRequest(BaseModel):
    doc_id: str | None = Field(default=None, description="Optional _id; generated if omitted")
    targets: str | list[str]
    doc: dict | list[dict]


def _default_filename(prefix: str, ext: str) -> str:
    from zotero_rdf_server.utils import default_filename
    return default_filename(prefix=prefix,ext=ext)
    # import datetime
    # ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    # return f"{prefix}-{ts}.{ext}"

JsonObj = Dict[str, Any]
JsonBody = Union[JsonObj, List[JsonObj]]

@router.post("/pipeline")
def ingest_route(
    input: Optional[JsonBody] = Body(default=None, examples=[None],description="Provide JSON List or SPARQL query result bindings. If none, runs query to get input from results."),
    targets: str | list = Query(default=None, description="Index or alias"),
    from_source: bool = Query(default=None, description="If true, generates input from external source (either produced via OCR or read from cache). If false, you must provide text in input directly."),
    framework: Literal["kraken", "tesseract", "transformer", "source", "none"] = Query(
        default=None,
        description="OCR backend: kraken, tesseract, or transformer. Choose 'none' to skip ATR/OCR",
    ),
    ingest: bool = Query(default=None, description="If true, ingest into Open Search"),
    delete_index: bool = Query(default=None, description="If true, deletes Open Search target index (or all, if no target given) before ingesting"),
    query: Optional[str] = Query(default=None, description="SPARQL SELECT query or path to file with query code (used when body is null)"),
    graph: str | None = Query(default=None, description="Named graph IRI containing the attachments or documents (optional)"),
    config_path: Optional[str] = Query(
        None,
        description="Path to YAML config. If omitted: ENV FTS_CONFIG, otherwise ./config.yml",
    ),
    triple_store: Optional[str] = Query(default=None, description="Oxigraph store path (defaults to main store) or SPARQL endpoint (POST)"),
    pipeline_ids: list[str] | None = Query(
        default=None,
        description="Pipeline IDs to run. If provided, only these pipelines are executed, even if inactive.",
    ),
    open_search_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for Open Search Config", examples=[None]),
    source_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for OCR Config", examples=[None]),
    framework_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for OCR Backend Config", examples=[None]),
    file_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for File Output", examples=[None]),
    vector_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for embedding Backend Config", examples=[None]),
    llm_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for LLM Backend Config", examples=[None]),
    rdf_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for RDF Backend Config", examples=[None]),
    qlever_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for RDF Backend Config", examples=[None]),
    reverse: bool = Query(default=None, description="If true, reverses order of item results"),

    worker_id: int = Query(default=None, ge=0, description="Worker index, 0-based"),
    total_workers: int = Query(default=None, ge=0, description="Total number of workers"),

):
    from .pipeline import ingest_pipeline
    from .helpers import convert_bindings, get_worker_slice
    import csv   
    run_ids = []
    if total_workers is not None and total_workers > 0:
        if worker_id >= total_workers:
            raise HTTPException(
                status_code=400,
                detail="worker_id must be < total_workers",
            )

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

    def _query_bindings(
        sparql_query: str,
        *,
        store=None,
        endpoint_url: str | None = None,
        use_default_graph_as_union: bool = False,
        default_graphs: list[str] | None = None,
        method: str = "POST",
        timeout: int | None = None,
    ):
        if endpoint_url:

            from .helpers import ensure_import
            ensure_import("SPARQLWrapper", None)
            from SPARQLWrapper import SPARQLWrapper, JSON, POST, GET

            client = SPARQLWrapper(endpoint_url)
            client.setQuery(sparql_query)
            client.setReturnFormat(JSON)
            client.setMethod(POST if method.upper() == "POST" else GET)

            if timeout is not None:
                client.setTimeout(timeout)

            if not use_default_graph_as_union and default_graphs:
                for graph in default_graphs:
                    client.addDefaultGraph(graph)

            return client.query().convert()

        if default_graphs:
            from pyoxigraph import NamedNode
            default_graphs = [
                g if isinstance(g, NamedNode) else NamedNode(str(g))
                for g in default_graphs
            ]
        return store.query(
            sparql_query,
            use_default_graph_as_union=use_default_graph_as_union,
            default_graph=default_graphs,
        )

    if input is None:
        try:
            from zotero_rdf_server.utils import load_text_like, is_url
            if triple_store and is_url(triple_store):
                sparql_endpoint = triple_store
                store_path = None
            else:
                store_path = triple_store
                sparql_endpoint = None

            if sparql_endpoint:
                logger.warning(f"Reading from SPARQL endpoint {sparql_endpoint}")
                from pyoxigraph import NamedNode
                store = None
            elif store_path:
                logger.warning(f"Reading from store in {store_path}")
                from pyoxigraph import Store, NamedNode
                store = Store.read_only(store_path)
            else:
                from zotero_rdf_server.global_store import  NamedNode, Store
                from zotero_rdf_server import global_store
                store = global_store.get_store(force=True)
                logger.warning("Reading from main store")

        except Exception as e:
            logger.error(f"Reading from main store failed: {e}")
            raise HTTPException(
                status_code=400,
                detail="Reading from store failed",
            )

        if graph or (graph is None and query is None):  # take one or multple graphs if no query given in API
            targets_set = []
            from zotero_rdf_server.global_store import get_graph
            checked_graph, all_graphs = get_graph(graph)
            if graph and not checked_graph:
                raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")
            from zotero_rdf_server.models import ZoteroLibrary
            import importlib
            import zotero_rdf_server.config as zcfg
            importlib.reload(zcfg)
            # from zotero_rdf_server.config import ZOTERO_LIBRARIES_CONFIGS
            selected_ids = set(pipeline_ids or [])
            delete_targets: set[str] = set()


            for lib_cfg in zcfg.ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)
                if not graph or graph == lib.base_uri:
                    logger.info(f"starting FTS pipeline for {lib.base_uri}...")

                    cfg = lib.plugin.get("fts") or [] # TODO load_dict_like
                    cfg = [cfg] if isinstance(cfg,dict) else cfg
                    if len(cfg)>1:
                        logger.warning(f"Running {len(cfg)} FTS configuration for library {lib.base_uri}")
                    # if not cfg:
                    #     raise HTTPException(status_code=400, detail=f"No FTS config for library {lib.base_uri}")
                    for n, ncfg in enumerate(cfg, start=1): # allow multiple runs per library
                        name = ncfg.get('name','n/a')
                        pipe_id = ncfg.get("id")

                        logger.info(f"PID {os.getpid()} running pipeline {pipe_id}")

                        if not pipe_id:
                            logger.warning(f"\n\n{n}/{len(cfg)}: Pipeline {name} has no id\n\n")

                        if selected_ids:
                            if pipe_id not in selected_ids:
                                logger.info(
                                    f"\n\n{n}/{len(cfg)}: Skipping pipeline {name} [{pipe_id}], not selected\n\n"
                                )
                                continue
                        else:
                            if not ncfg.get("active", True):
                                logger.info(
                                    f"\n\n{n}/{len(cfg)}: Skipping deactivated pipeline {name} [{pipe_id}]\n\n"
                                )
                                continue

                        logger.info(f"\n\n{n}/{len(cfg)}: Deploying pipeline {name}\n\n")

                        os_cfg = open_search_kwargs if open_search_kwargs is not None else (ncfg.get("open-search") or {})
                        pipeline_cfg = (ncfg.get("pipeline") or {})
                        targets_x = targets if targets is not None else os_cfg.get("targets")
                        targets_x = targets_x if isinstance(targets_x, list) else [targets_x]
                        targets_x = [str(t) for t in targets_x if t]
                        targets_set.extend(targets_x)

                        ingest_x = ingest if ingest is not None else ncfg.get("ingest", True)


                        if not targets_x and ingest_x==True:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Missing target indices/index in library {lib.library_id}, {ingest_x}",
                            )

                        config_path_x = config_path or os_cfg.get("config_path")


                        if delete_index is not None:
                            requested_delete_targets = targets_x if delete_index else []
                        else:
                            delete_cfg = os_cfg.get("delete_index", [])
                            if delete_cfg is True:
                                requested_delete_targets = targets_x
                            elif delete_cfg is False or delete_cfg is None:
                                requested_delete_targets = []
                            elif isinstance(delete_cfg, str):
                                requested_delete_targets = [delete_cfg]
                            else:
                                requested_delete_targets = list(delete_cfg)

                        delete_x = [
                            str(t)
                            for t in requested_delete_targets
                            if t and str(t) not in delete_targets
                        ]

                        delete_targets.update(delete_x)
                        
                        query_x = query or ncfg.get("query")
                        sparql_endpoint_x = sparql_endpoint or ncfg.get("sparql_endpoint")

                        if not query_x:
                            raise HTTPException(
                                status_code=400,
                                detail="With no input, you must provide 'query' parameter",
                            )

                        from_source_x = from_source if from_source is not None else ncfg.get("from_source", True)

                        framework_x = framework  if framework is not None else ncfg.get("framework", "kraken")
                        vector_x = vector_kwargs if vector_kwargs is not None else ncfg.get("vector")

                        llm_x = llm_kwargs if llm_kwargs is not None else ncfg.get("llm_kwargs")

                        rdf_x = rdf_kwargs if rdf_kwargs is not None else pipeline_cfg.get("rdf_kwargs")
                        ql_x = qlever_kwargs if qlever_kwargs is not None else pipeline_cfg.get("qlever_kwargs")
                        if rdf_x:
                            import re
                            from copy import deepcopy
                            rdf_x = deepcopy(rdf_x)

                            output_dir = Path(rdf_x["output"])

                            library_id = re.sub(
                                r"[^A-Za-z0-9_.-]+",
                                "_",
                                str(lib.library_id),
                            )

                            pipeline_id = re.sub(
                                r"[^A-Za-z0-9_.-]+",
                                "_",
                                str(pipe_id or f"pipeline-{n}"),
                            )

                            rdf_x["output"] = str(
                                output_dir
                                / f"{library_id}.{pipeline_id}.nq.gz"
                            )
                            
                        reverse_x = reverse if reverse is not None else ncfg.get("reverse_results", False)

                        iter_pages_kwargs = source_kwargs if source_kwargs is not None else dict(pipeline_cfg.get("source_kwargs") or {})
                        page_to_text_kwargs = framework_kwargs if framework_kwargs is not None else dict(pipeline_cfg.get("framework_kwargs") or {})
                        text_image_file_kwargs = file_kwargs if file_kwargs is not None else dict(pipeline_cfg.get("file_kwargs") or {})

                        items = []

                        try:
                            sparql_query=load_text_like(query_x,label="Ingest Pipeline SPARQL Query")
                            logger.info(f"SPARQL query:\n\n{sparql_query}")

                            bindings = _query_bindings(
                                sparql_query,
                                store=store,
                                endpoint_url=sparql_endpoint_x,
                                use_default_graph_as_union=False,
                                default_graphs=[lib.base_uri, lib.knowledge_base_graph],
                            )
                            items, var_names = convert_bindings(
                                bindings,
                                reverse=reverse_x,
                            )

                            if total_workers is not None and total_workers > 0:
                                logger.warning(f"SLICE of {len(items)} for worker {worker_id} of {total_workers} workers")
                                items = get_worker_slice(items=items,worker_id=worker_id,total_workers=total_workers)
                                logger.info(f"Got {len(items)} after SLICE!")

                            logger.info(f"SPARQL returned columns: {var_names}")
                            logger.info(f"{len(items)} results (store LEN: {len(store)})")

                            # Save as CSV
                            save_query_to_file(items=items,var_names=var_names)

                        except Exception as e:
                            logger.error(f"Query failed: {e}")
                            items = []
                        pipeline_meta:dict = {'name_pipeline': name, 'id_pipeline': pipe_id, 'i_worker': worker_id, 'len_worker': total_workers, 'reverse': reverse_x}
                        iter_pages_kwargs['reverse'] = reverse_x
                        run_ids.extend(ingest_pipeline(items=items,
                                                targets=targets_x,
                                                from_source=from_source_x,
                                                framework=framework_x,
                                                vector_kwargs=vector_x,
                                                llm_kwargs=llm_x,
                                                ingest=ingest_x,
                                                delete_index=delete_x,
                                                iter_pages_kwargs=iter_pages_kwargs,
                                                page_to_text_kwargs=page_to_text_kwargs, text_image_file_kwargs=text_image_file_kwargs,
                                                config_path=config_path_x,
                                                pipeline_meta=pipeline_meta,
                                                rdf_kwargs=rdf_x,qlever_tsv_paths=ql_x))

                elif graph and graph != lib.base_uri:
                    logger.debug(f"{lib.base_uri} skipped")
                else:
                    logger.warning(f"{graph} not yet supported but defined via config")

            targets=list(set(targets_set))
            try:
                store = None
                import gc
                gc.collect()
            except:
                logger.warning("Store not found, maybe check!")

        elif graph is None and query: # query directly
            if not targets and ingest==True:
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing target indices/index for query",
                )
            logger.info("Starting FTS pipeline for entire store with query...")
            try:

                sparql_query=load_text_like(query,label="Ingest Pipeline SPARQL Query")
                logger.debug(f"{sparql_query}")
                # bindings = store.query(sparql_query, use_default_graph_as_union=True)
                # items, var_names = convert_bindings(bindings)
                bindings = _query_bindings(
                    sparql_query,
                    store=store,
                    endpoint_url=sparql_endpoint,
                    use_default_graph_as_union=True,
                    default_graphs=None,
                )
                items, var_names = convert_bindings(
                    bindings,
                    reverse=reverse,
                )

                logger.info(f"SPARQL returned columns: {var_names}")
                logger.debug(f"{items} results")
                save_query_to_file(items=items,var_names=var_names)
            except Exception as e:
                logger.error(f"Query failed: {e}")
                raise HTTPException(
                    status_code=400,
                    detail="Query failed",
                )

            try:
                store = None
                import gc
                gc.collect()
            except:
                logger.warning("Store not found, maybe check!")

            from_source = True if from_source is True else False

            source_kwargs['reverse'] = reverse

            run_ids.extend(ingest_pipeline(items=items,
                                            targets=targets,
                                            from_source=from_source,
                                            framework=framework,
                                            vector_kwargs=vector_kwargs,
                                            llm_kwargs=llm_kwargs,
                                            ingest=ingest,
                                            delete_index=delete_index,
                                            iter_pages_kwargs=source_kwargs,
                                            page_to_text_kwargs=framework_kwargs,
                                            text_image_file_kwargs=file_kwargs,
                                            config_path=config_path,
                                            rdf_kwargs=rdf_kwargs,qlever_tsv_paths=qlever_kwargs))
        else:
            raise HTTPException(
                    status_code=400,
                    detail="Missing parameters for query!",
                )

    else:
        logger.warning(f"Input {input}")  # use given bindings
        if not targets and ingest==True:
            raise HTTPException(
                status_code=400,
                detail="Missing target indices/index for bindings",
            )
        items = []

        if isinstance(input, str):
            from zotero_rdf_server.utils import load_dict_like
            input = load_dict_like(input,label="Ingest Pipeline Input") # (CSV should work)!

        if (
            isinstance(input, dict)
            and "head" in input
            and "vars" in input["head"]
            and "results" in input
            and "bindings" in input["results"]
        ):
            logger.info("Input is SPARQL-JSON!")
            items, var_names = convert_bindings(input, reverse=reverse)
        elif isinstance(input, list) and all(isinstance(x, dict) for x in input):
            logger.info("Input is JSON objects list!")
            items, var_names = convert_bindings(input, reverse=reverse)
        else:
            raise HTTPException(status_code=400, detail="Body (can be read from file path) must be a SPARQL-JSON object, a list of JSON objects, a CSV, or null (then provide a query)")

        from_source = True if from_source is True else False
        save_query_to_file(items=items,var_names=var_names, json_mode=False)

        run_ids.extend(ingest_pipeline( items=items,
                                        targets=targets,
                                        from_source=from_source,
                                        framework=framework,
                                        vector_kwargs=vector_kwargs,
                                        llm_kwargs=llm_kwargs,
                                        ingest=ingest,
                                        delete_index=delete_index,
                                        iter_pages_kwargs=source_kwargs,
                                        page_to_text_kwargs=framework_kwargs,
                                        text_image_file_kwargs=file_kwargs,
                                        config_path=config_path,
                                        rdf_kwargs=rdf_kwargs,qlever_tsv_paths=qlever_kwargs))

    result = {
        "status": "ok",
        "run_ids": run_ids[:2],
        "targets": list(targets),
        "runs": len(run_ids),
        "pipeline_ids": list(pipeline_ids) if pipeline_ids else [],
    }
    try:
        _runs_filename = _default_filename("runs_result", "json")
        with open(export_dir / _runs_filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Couldn't save file: {e}")

    result.pop('run_ids') # do not retun full data

    return result

### SEARCHES ###

@router.post("/opensearch")
def ingest_opensearch(req: OpenSearchDocRequest = Body(...)):
    from .db import index_stream

    run = index_stream(
        doc_id=req.doc_id,
        targets=req.targets,
        doc=req.doc,
    )
    return run #{"status": "ok", "run_id": run_id}

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
    context_query: Dict[str, Any],
    output_format: str = "json",
    columns: Optional[str] = None,
    include_context: bool = False,
    filename: Optional[str] = None,
    flatten_dict: list = ["meta", "analysis"],
    keep_dict: list = ["analysis"],
    include_aggs: bool = True,
    keep_highlight: bool = True,
    make_snippet: bool = False,
    highlight_field: Optional[str] = None,
    truncate_chars: int = 0,
    truncate_field: str = "text",
    md_highlight_pre: str = "**",
    md_highlight_post: str = "**",
    markdown_title: str = "Search Results",
    markdown_max_rows: int = MAX_SIZE,
    api_call: Optional[str] = None,
    header_meta: Optional[Dict[str, Any]] = None,
    header_md_extra: Optional[str] = None,
    root_path: str = ""
):
    from .search import (
        normalize_hits,
        # collect_columns,
        render_csv,
        render_markdown,
        render_markdown_query_header,
        render_html,
        render_html_query_header,
        extract_buckets,
        render_markdown_table,
        render_html_table,
        normalize_output_column,
    )

    if not output_format == "json":
        normalized = normalize_hits(
            resp,
            flatten_dict=flatten_dict,
            keep_dict=keep_dict,
            keep_highlight=keep_highlight,
            make_snippet=make_snippet,
            highlight_field=highlight_field,
            truncate_chars=truncate_chars,
            truncate_field=truncate_field,
        )



        rows = normalized.get("hits", [])
        aggs = normalized.get("aggregations") if include_aggs else None
        def is_analysis_col(col: str) -> bool:
            return col.startswith("analysis.") or col.startswith("analysis_")

        default_cols = ["_id", "_score", "source", "page", "snippet", "ingest_ts", "meta_parent", "label"]
        default_cols_small = ["source", "page", "snippet", "label"]

        analysis_cols = []
        preferred_cols = []

        if columns:
            for raw in columns.split(","):
                c = normalize_output_column(raw.strip())
                if not c:
                    continue
                if is_analysis_col(c):
                    analysis_cols.append(c)
                else:
                    preferred_cols.append(c)

        base_defaults = default_cols if include_context else default_cols_small
        combined_cols = list(dict.fromkeys(preferred_cols + base_defaults + analysis_cols))


        # cols = collect_columns(
        #     rows,
        #     preferred=combined_cols,
        # )

        cols = combined_cols # [c for c in combined_cols if c in {k for r in rows for k in r.keys()}]


    if output_format in ("md", "markdown", "html"):
        cols = [c for c in cols if c != "highlight"]

    if output_format == "json":
        hits = resp.get("hits", {}).get("hits", [])
        payload: Dict[str, Any] = {
            "hits": {
                "total": resp.get("hits", {}).get("total"),
                "hits": hits,
            }
        }
        if header_meta:
            payload["meta"] = header_meta
        if include_context and api_call:
            payload["api_call"] = api_call
        if include_aggs:
            payload["aggregations"] = resp.get("aggregations")
        if include_context:
            payload["context_query"] = context_query
        return JSONResponse(payload)

    if output_format == "csv":
        content = render_csv(rows, cols)
        stream = io.BytesIO(content.encode("utf-8"))

        return StreamingResponse(
            stream,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_default_filename(filename or "search", "csv")}"'
                )
            },
        )

    if output_format == "csv-analysis":
        from .search import collect_columns, add_analysis_columns
        normalized = normalize_hits(
            resp,
            flatten_dict=["meta"],
            keep_dict=[],
            keep_highlight=False,
            make_snippet=False,
        )

        rows = [add_analysis_columns(r) for r in normalized["hits"]]
        exclude = {"text", "highlight"}
        cols = [c for c in collect_columns(rows, preferred=["_id", "_score", "ingest_ts", "source", "page"]) if c not in exclude]
        content = render_csv(rows, cols)
        stream = io.BytesIO(content.encode("utf-8"))

        return StreamingResponse(
            stream,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_default_filename(filename or "search", "csv")}"'
                )
            },
        )
    if output_format in {"json-analysis", "atlas"}:
        from .search import add_analysis_columns

        normalized = normalize_hits(
            resp,
            flatten_dict=["meta"],
            keep_dict=[],
            keep_highlight=False,
            make_snippet=False,
        )

        logger.info(json.dumps(normalized['hits'][0],indent=4))

        rows = [add_analysis_columns(r) for r in normalized["hits"]]

        exclude = {"highlight"}
        include = {"neighbors"}

        def _is_scalar_list(v):
            return isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v)

        def _clean_row(row):
            out = {}
            for k, v in row.items():
                if k in exclude:
                    continue
                if isinstance(v, dict) and k not in include:
                    continue
                if isinstance(v, list):
                    if _is_scalar_list(v):
                        out[k] = v
                    continue
                out[k] = v
            return out

        cleaned_rows = [_clean_row(r) for r in rows]

        row_by_id = {row.get("__row_index__"): row for row in cleaned_rows}

        sample_found = False
        for idx, row in enumerate(cleaned_rows):
            neigh = row.get("neighbors") or {}
            ids = neigh.get("ids") or []
            if not ids:
                continue

            sample_found = True
            logger.info(
                "ATLAS SAMPLE idx=%s row_id=%r keys=%r",
                idx,
                row.get("__row_index__"),
                list(row.keys()),
            )
            logger.info(
                "ATLAS SAMPLE source parent-ish values: parent=%r meta_parent=%r",
                row.get("parent"),
                row.get("meta_parent"),
            )
            logger.info("ATLAS SAMPLE neighbor ids=%r", ids[:10])

            for nid in ids[:10]:
                nrow = row_by_id.get(nid)
                logger.info(
                    "  nid=%r found=%r parent=%r meta_parent=%r",
                    nid,
                    nrow is not None,
                    None if nrow is None else nrow.get("parent"),
                    None if nrow is None else nrow.get("meta_parent"),
                )
            break

        if not sample_found:
            logger.warning("No rows with non-empty neighbors found in atlas payload")

        content = json.dumps(
            cleaned_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        if output_format == "atlas":
            try:
                from .viewer import export_atlas_folder, ATLAS_URL
                safe_atlas_url = f"{root_path}/{ATLAS_URL.lstrip('/')}" if root_path and ATLAS_URL else ATLAS_URL
                _input = list(cleaned_rows)
                logger.warning(f"Redirect to {safe_atlas_url}")
                export_atlas_folder(_input)
                return RedirectResponse(safe_atlas_url)
            except Exception as e:
                logger.error(e)
                pass

        stream = io.BytesIO(content.encode("utf-8"))

        return StreamingResponse(
            stream,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_default_filename(filename or "search", "json")}"'
                )
            },
        )

    if output_format in ("md", "markdown"):
        header = render_markdown_query_header(context_query) if include_context else ""

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
            buckets = extract_buckets(aggs)
            header += "## Aggregations\n\n"
            header += render_markdown_table(buckets)


        if include_context and api_call and include_context:
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
            verbose=include_context
        )

        content = header + body

        if keep_highlight or make_snippet:
            content = content.replace("<em>", md_highlight_pre).replace("</em>", md_highlight_post)

        stream = io.BytesIO(content.encode("utf-8"))
        return StreamingResponse(
            stream,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{_default_filename(filename or "search", "md")}"'
            },
        )

    if output_format == "html":
        header = render_html_query_header(context_query) if include_context else ""

        if header_meta:
            pretty_meta = html.escape(json.dumps(header_meta, indent=2, ensure_ascii=False))
            header += f"<h2>Metadata</h2><pre><code>{pretty_meta}</code></pre>"

        if header_md_extra:
            header += f"<div>{header_md_extra.rstrip()}</div>"

        if aggs is not None:
            buckets = extract_buckets(aggs)
            header += "<h2>Aggregations</h2>"
            header += render_html_table(buckets)

        if include_context and api_call and include_context:
            header += f"<h2>API Call</h2><pre><code>{html.escape(api_call)}</code></pre>"

        body = render_html(
            rows,
            cols,
            max_rows=markdown_max_rows,
            title=markdown_title,
            verbose=include_context
        )

        content = (
            "<!doctype html>"
            "<html><head><meta charset='utf-8'>"
            f'<title>{_default_filename(filename or "search", "html").rstrip(".html")}</title>'
            "<style>"
            "body{font-family:system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px;line-height:1.5;}"
            "pre{background:#f6f8fa;padding:12px;border-radius:8px;overflow:auto;}"
            "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}"
            "hr{margin:32px 0;}"
            "a{color:#0969da;text-decoration:none;}"
            "a:hover{text-decoration:underline;}"
            "strong{font-weight:600;}"
            "</style>"
            "</head><body>"
            f"{header}{body}"
            "</body></html>"
        )

        # stream = io.BytesIO(content.encode("utf-8"))
        # return StreamingResponse(
        #     stream,
        #     media_type="text/html; charset=utf-8",
        #     headers={
        #         "Content-Disposition": f'attachment; filename="{filename or _default_filename("search", "html")}"'
        #     },
        # )
        # return HTMLResponse(content=content)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'inline; filename="{_default_filename(filename or "search", "html")}"'
            },
        )

    raise HTTPException(status_code=400, detail="Invalid format. Use: json, csv, md, html.")

from enum import Enum
from typing import Annotated

from fastapi import Depends

class OutputFormat(str, Enum):
    json = "json"
    csv = "csv"
    csv_analysis = "csv-analysis"
    json_analysis = "json-analysis"
    markdown = "markdown"
    html = "html"
    atlas = "atlas"

def resolve_format(
    request: Request,
    format: Annotated[
        OutputFormat | None,
        Query(
            description=(
                "Response format. "
                "json: structured API response. "
                "csv: flat table from hits.hits. "
                "html: human-readable HTML document hits.hits. "
                "md/markdown: human-readable document blocks from hits.hits."
            )
        ),
    ] = None
) -> OutputFormat:
    if format is not None:
        return format

    accept = request.headers.get("accept", "").lower()

    if "text/html" in accept:
        return OutputFormat.html

    return OutputFormat.json

class AggMode(str, Enum):
    terms = "terms"
    significant_text = "significant_text"
    composite = "composite"

class AggregationParams(BaseModel):
    agg_field: Optional[str] = Field(
        default=None,
        description=(
            "Field to aggregate on. "
            "For terms, keyword fields are usually best. "
            "Text fields are allowed too, but on text fields terms aggregation "
            "requires fielddata=true and aggregates analyzed tokens."
        ),
    )

    agg_mode: AggMode | None = Field(
        default=AggMode.terms,
        description=(
            "Aggregation mode. "
            "'terms' returns the most frequent buckets."
            "'significant_text' returns statistically significant terms from text."
        ),
    )

    agg_size: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Number of buckets/terms to return.",
    )

    agg_shard_size: Optional[int] = Field(
        default=-1,
        ge=1,
        le=10000,
        description=(
            "Optional shard_size for the aggregation. "
            "Supported for terms and significant_text."
        ),
    )

    agg_min_doc_count: Optional[int] = Field(
        default=None,
        ge=1,
        le=1000000,
        description=(
            "Optional min_doc_count for the aggregation. "
            "Useful especially for significant_text."
        ),
    )

    agg_shard_min_doc_count: Optional[int] = Field(
        default=None,
        ge=1,
        le=1000000,
        description=(
            "Optional shard_min_doc_count for significant_text."
        ),
    )

    agg_sampler_shard_size: Optional[int] = Field(
        default=None,
        ge=1,
        le=100000,
        description=(
            "Optional sampler shard_size. "
            "If set, wraps the aggregation in a sampler aggregation. "
            "Usually useful for significant_text."
        ),
    )

def get_aggregation_params(
    agg_field: Annotated[
        Optional[str],
        Query(
            description=(
                "Field to aggregate on. "
                "For terms, keyword fields are usually best. "
                "Text fields are allowed, but terms on text requires fielddata=true."
            ),
        ),
    ] = None,
    agg_mode: Annotated[
        AggMode | None,
        Query(
            description="Aggregation mode: terms or significant_text.",
        ),
    ] = None,
    agg_size: Annotated[
        int,
        Query(
            ge=1,
            le=1000,
            description="Number of buckets/terms to return.",
        ),
    ] = 10,
    agg_shard_size: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=10000,
            description="Optional shard_size for terms/significant_text.",
        ),
    ] = None,
    agg_min_doc_count: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=1000000,
            description="Optional min_doc_count.",
        ),
    ] = None,
    agg_shard_min_doc_count: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=1000000,
            description="Optional shard_min_doc_count for significant_text.",
        ),
    ] = None,
    agg_sampler_shard_size: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=100000,
            description=(
                "Optional sampler shard_size. "
                "If set, wraps aggregation in sampler."
            ),
        ),
    ] = None,
) -> AggregationParams:
    return AggregationParams(
        agg_field=agg_field,
        agg_mode=agg_mode,
        agg_size=agg_size,
        agg_shard_size=agg_shard_size,
        agg_min_doc_count=agg_min_doc_count,
        agg_shard_min_doc_count=agg_shard_min_doc_count,
        agg_sampler_shard_size=agg_sampler_shard_size,
    )

def build_aggregation(agg: AggregationParams) -> Optional[dict]:
    """
    Build the OpenSearch aggs object.
    Returns None if no aggregation was requested.
    """
    if not agg.agg_field:
        return None

    if agg.agg_mode == AggMode.terms:
        inner = {
            "terms": {
                "field": agg.agg_field,
                "size": agg.agg_size,
            }
        }

        if agg.agg_shard_size is not None:
            inner["terms"]["shard_size"] = agg.agg_shard_size

        if agg.agg_min_doc_count is not None:
            inner["terms"]["min_doc_count"] = agg.agg_min_doc_count

        if agg.agg_sampler_shard_size is not None:
            return {
                "by_field": {
                    "sampler": {
                        "shard_size": agg.agg_sampler_shard_size
                    },
                    "aggs": {
                        "values": inner
                    },
                }
            }

        return {"by_field": inner}

    if agg.agg_mode == AggMode.significant_text:
        inner = {
            "significant_text": {
                "field": agg.agg_field,
                "size": agg.agg_size,
            }
        }

        if agg.agg_shard_size is not None:
            inner["significant_text"]["shard_size"] = agg.agg_shard_size

        if agg.agg_min_doc_count is not None:
            inner["significant_text"]["min_doc_count"] = agg.agg_min_doc_count

        if agg.agg_shard_min_doc_count is not None:
            inner["significant_text"]["shard_min_doc_count"] = (
                agg.agg_shard_min_doc_count
            )

        if agg.agg_sampler_shard_size is not None:
            return {
                "by_field": {
                    "sampler": {
                        "shard_size": agg.agg_sampler_shard_size
                    },
                    "aggs": {
                        "values": inner
                    },
                }
            }

        return {"by_field": inner}

    raise HTTPException(status_code=400, detail=f"Unsupported agg_mode: {agg.agg_mode}")

from datetime import datetime, timezone, date

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class IngestTsRangeFilter(BaseModel):
    ingest_from: Optional[datetime] = None
    ingest_to: Optional[datetime] = None
    ingest_field: str = "ingest_ts"

def get_ingest_ts_range_filter(
    ingest_from: Annotated[
        Optional[datetime],
        Query(description=(
                "Lower bound for ingest_ts (inclusive), ISO-8601. "
            )),
    ] = None,
    ingest_to: Annotated[
        Optional[datetime],
        Query(description=("Upper bound for ingest_ts (inclusive), ISO-8601."
                        f" Example: {now_iso()}")),
    ] = None,
):
    return IngestTsRangeFilter(
        ingest_from=ingest_from,
        ingest_to=ingest_to,
    )


# NLP
from .analysis.models import ResultAnalysisParams, get_result_analysis_params

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
        "*",
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
    use_shingles: bool = Query(True),
    shingle_field: Optional[str] = Query(None),
    phrase_slop: int = Query(2, ge=0, le=20),
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
        500,
        ge=20,
        le=10000,
        description="Size of each highlight fragment.",
    ),
    fragments: int = Query(
        10,
        ge=0,
        le=100,
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
    agg: AggregationParams = Depends(get_aggregation_params),
    out_header: OutputHeader = Depends(output_header_params),
    format: OutputFormat = Depends(resolve_format),
    columns: Optional[str] = Query(
        None,
        description="Comma-separated list of fields to include from _source.",
    ),
    filters: KeywordFilter = Depends(keyword_filter_params),
    ingest_ts: IngestTsRangeFilter = Depends(get_ingest_ts_range_filter),
    analysis: ResultAnalysisParams = Depends(get_result_analysis_params),

    context: bool = Query(True, description="Include query context in the response."),

):
    from .search import parse_csv, build_terms_should_queries, os_search, apply_paging, apply_keyword_filter, apply_ingest_ts_range_filter, TermQueryConfig

    api_call = str(request.url)
    # --- Build query ---------------------------------------------------------
    terms=[]
    match_all = q == "*" or q == ""
    if match_all:
        body = {
            "query": {"match_all": {}},
            "sort": [
                {"doc_id": "asc"},
                {"page": "asc"}
            ]
        }

    elif lucene:
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
            config=TermQueryConfig(
                field=field,
                exact=exact,
                truncated=truncated,
                fuzzy=fuzzy,
                use_shingles=use_shingles,
                shingle_field=shingle_field,
                phrase_slop=phrase_slop,
            ),
        )

        body = {"query": {"bool": {"should": should, "minimum_should_match": 1}}}

    apply_ingest_ts_range_filter(body,ingest_ts)
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size, offset=offset)

    def _split_columns(columns: Optional[str]) -> List[str]:
        if not columns:
            return []
        return [c.strip() for c in columns.split(",") if c.strip()]

    def _join_columns(cols: List[str]) -> Optional[str]:
        seen = set()
        out = []
        for c in cols:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return ",".join(out) if out else None

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
    if not agg.agg_field and agg.agg_mode == AggMode.significant_text:
        agg.agg_field = field
    if agg.agg_mode and agg.agg_field:
        aggs = build_aggregation(agg)
        if aggs:
            body["aggs"] = aggs

    # --- Columns ---------------------------------------------------------
    base_columns = _split_columns(columns)

    source_cols = list(base_columns)
    render_cols = list(base_columns)

    if highlight or truncate_chars > 0:
        if "snippet" not in render_cols:
            render_cols.insert(0, "snippet")

    return_analysis = format in {OutputFormat.csv_analysis, OutputFormat.json_analysis, OutputFormat.atlas} or analysis.perform_analysis

    if return_analysis:
        analysis.perform_analysis=True
        analysis.cluster_enabled=True
        analysis.neighbors_enabled=True

    if analysis.cluster_enabled:
        required_modes = {analysis.cluster_source, analysis.cluster_label_source}

        if analysis.analysis_mode != "both":
            if analysis.analysis_mode not in required_modes:
                analysis.analysis_mode = "both"
            elif len(required_modes) == 2:
                analysis.analysis_mode = "both"
        render_cols.append("analysis_cluster_label")
        render_cols.append("analysis_cluster_id")


    if analysis.perform_analysis:
        # if source_cols and field and field not in source_cols:
        #     source_cols.append(field)
        if analysis.analysis_mode == "both" or analysis.analysis_mode == "hits_documents":
            if "analysis_local_key_terms" not in render_cols:
                render_cols.append("analysis_local_key_terms")

        if analysis.analysis_mode == "both" or analysis.analysis_mode == "index_documents":
            if "analysis_global_key_terms" not in render_cols:
                render_cols.append("analysis_global_key_terms")

    source_columns = _join_columns(source_cols)
    render_columns = _join_columns(render_cols)

    # --- Query ---------------------------------------------------------
    try:
        from .search import os_search_all_scroll, enrich_hits_with_analysis
        from .viewer import add_viewer_url

        if return_analysis and match_all and size > 10000:
            resp = os_search_all_scroll(
                index=index,
                body=body,
                columns=source_columns,
                batch_size=1000,
                scroll_ttl="2m",
            )
            logger.info("Got all scroll hits!")
        else:
            resp = os_search(index=index, body=body, columns=source_columns)


        hits = resp.get("hits", {}).get("hits", [])

        add_viewer_url(hits,request=None)

        hits = enrich_hits_with_analysis(
            hits,
            index=index,
            analysis=analysis,
            field=field,
            return_analysis=return_analysis,
            # sort = format!=OutputFormat.atlas
        )

        resp["hits"]["hits"] = hits

        if return_analysis and match_all and size > 10000:
            resp["hits"]["total"] = {"value": len(hits), "relation": "eq"}

        if hits:
            h0 = hits[0]
            logger.info("First hit keys: %s", list(h0.keys()))
            logger.debug("First hit highlight: %s", h0.get("highlight"))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")

    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        context_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=render_columns,
        include_context=context,
        filename=f"search_terms_{terms[0] if terms and not lucene else 'query'}",
        # flatten_dict=True,
        # keep_dict=False,
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
        root_path=request.scope.get("root_path", "")
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
    ingest_ts: IngestTsRangeFilter = Depends(get_ingest_ts_range_filter),
    format: OutputFormat = Depends(resolve_format),
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    context: bool = Query(False, description="Include context"),
):
    from .search import parse_csv, build_proximity_intervals_query, os_search, apply_paging, apply_keyword_filter, apply_ingest_ts_range_filter
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
    apply_ingest_ts_range_filter(body,ingest_ts)
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")

    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        context_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_context=context,
        markdown_max_rows=size,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
        root_path=request.scope.get("root_path", "")
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
    ingest_ts: IngestTsRangeFilter = Depends(get_ingest_ts_range_filter),
    format: OutputFormat = Depends(resolve_format),
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    context: bool = Query(True, description="Include query context in the response."),
):
    from .search import get_doc_vector, os_search, apply_paging, apply_keyword_filter, apply_ingest_ts_range_filter
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
    apply_ingest_ts_range_filter(body,ingest_ts)
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")

    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        context_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_context=context,
        markdown_max_rows=size,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
        root_path=request.scope.get("root_path", "")
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
    ingest_ts: IngestTsRangeFilter = Depends(get_ingest_ts_range_filter),
    format: OutputFormat = Depends(resolve_format),
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    out_header: OutputHeader = Depends(output_header_params),
    context: bool = Query(True, description="Include query context in the response."),

):
    from .search import os_search, apply_paging, apply_keyword_filter, apply_ingest_ts_range_filter
    api_call = str(request.url)
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    if not field_list:
        raise HTTPException(status_code=400, detail="No fields provided.")
    if not index:
        from .search import DEFAULT_ALIAS
        mlt_index = DEFAULT_ALIAS
    else:
        mlt_index = index

    mlt_query: Dict[str, Any] = {
        "more_like_this": {
            "fields": field_list,
            "like": [{"_index": mlt_index, "_id": os_id}],
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
    apply_ingest_ts_range_filter(body,ingest_ts)
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)
    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")

    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        context_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_context=context,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
        root_path=request.scope.get("root_path", "")
    )

@open_router.post(
    "/search/mlt/by-text",
    summary="Token similarity search by free text (More Like This)",
    description="Runs a More Like This query using input text to find token-similar documents.",
    tags=["Search"]
)
def mlt_by_text(
    request: Request,
    like_text: str = Body(..., min_length=1, max_length=20_000, examples=["Similis simili gaudet"], media_type="text/plain"),
    index: str = Query(None, description="OpenSearch index name. Default alias can be set in configuration"),
    fields: str = Query("text", description="CSV list of fields, typically 'text'"),
    min_term_freq: int = Query(1, ge=0),
    min_doc_freq: int = Query(1, ge=0),
    max_query_terms: int = Query(25, ge=1, le=100),
    minimum_should_match: str = Query("30%", description="e.g. '30%' or '2'"),
    size: int = Query(20, ge=1, le=1000),
    columns: Optional[str] = Query(None, description="Optional CSV list of columns to include"),
    context: bool = Query(True, description="Include query context in the response."),
    filters: KeywordFilter = Depends(keyword_filter_params),
    ingest_ts: IngestTsRangeFilter = Depends(get_ingest_ts_range_filter),
    format: OutputFormat = Depends(resolve_format),
    out_header: OutputHeader = Depends(output_header_params),
):
    from .search import os_search, apply_paging, apply_keyword_filter, apply_ingest_ts_range_filter

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
    apply_ingest_ts_range_filter(body,ingest_ts)
    apply_keyword_filter(body, filters)
    apply_paging(body, size=size)

    try:
        resp = os_search(index=index, body=body, columns=columns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenSearch search error: {e}")

    meta = make_header(out_header)

    return format_search_response(
        resp=resp,
        context_query=body,
        output_format=format.value if hasattr(format, "value") else format,
        columns=columns,
        include_context=context,
        api_call=api_call,
        header_meta=meta or None,
        header_md_extra=out_header.header_md,
        root_path=request.scope.get("root_path", "")
    )

# VIEWER
from fastapi import Form

@open_router.get(
    "/image/{os_doc_id:path}",
    summary="Return image for one page",
    name = "image",
    tags=["Viewer"])
def get_image(os_doc_id: str):
    from .viewer import split_doc_id, image_file
    doc_key, page = split_doc_id(os_doc_id)
    img_path = image_file(doc_key, page)

    logger.info(f"get_image os_doc_id={os_doc_id}")
    logger.info(f"get_image img_path={img_path} exists={img_path.exists()}")

    if not img_path.exists() or not img_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)

@open_router.get(
    "/text/{os_doc_id:path}",
    summary="Return text for one page",
    name = "text",
    tags=["Viewer"])
def get_text(os_doc_id: str):
    from .viewer import split_doc_id, text_file
    doc_key, page = split_doc_id(os_doc_id)

    txt_path = text_file(doc_key, page)

    logger.info(f"get_text os_doc_id={os_doc_id}")
    logger.info(f"get_text txt_path={txt_path} exists={txt_path.exists()}")

    if not txt_path.exists() or not txt_path.is_file():
        raise HTTPException(status_code=404, detail="Text file not found")

    return FileResponse(txt_path)

@open_router.get(
    "/view",
    summary="Viewer start page",
    name="view-root",
    tags=["Viewer"],
)
def view_root(request: Request):
    return build_view_response(request, None, editable=False)

@open_router.get(
    "/view/{os_doc_id:path}",
    summary="View image page and OCR",
    name = "view",
    description="Indicate root directories and file extensions in configuration under 'viewer'.",
    tags=["Viewer"],
)
def view_page(request: Request, os_doc_id: str):
    return build_view_response(request, os_doc_id, editable=False)

@router.get("/edit-view/{os_doc_id:path}",
            name = "edit-view",
            tags=["Viewer"])
def edit_page(request: Request, os_doc_id: str):
    return build_view_response(request, os_doc_id, editable=True)

@router.post(
    "/save-view/{os_doc_id:path}",
    summary="Save OCR text",
    name = "save-view",
    tags=["Viewer"],
)
def save_page(request: Request, os_doc_id: str, text: str = Form(...)):
    from .viewer import split_doc_id, text_file

    original_os_doc_id = (os_doc_id or "").strip()
    doc_key, page = split_doc_id(original_os_doc_id)
    txt_path = text_file(doc_key, page)

    try:
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(text, encoding="utf-8")
    except Exception as exc:
        logger.exception(f"Could not write text file {txt_path}: {exc}")
        raise HTTPException(status_code=500, detail="Could not save text")

    redirect_url = str(
        router.url_for("edit-view", os_doc_id=original_os_doc_id)
    )

    return RedirectResponse(
        url=redirect_url,
        status_code=303,
    )

def build_view_response(
    request: Request,
    original_os_doc_id: str | None,
    editable: bool = False
) -> HTMLResponse:
    from .viewer import (
        render_page,
        split_doc_id,
        image_file,
        text_file,
        list_pages,
        discover_doc_url,
        IMAGE_EXT,
    )

    def url_for_path(name: str, **params) -> str:
        try:
            return str(request.url_for(name, **params).path)
        except:
            return ""

    raw_os_doc_id = (original_os_doc_id or "").strip()

    root_path = request.scope.get("root_path", "")



    if not raw_os_doc_id:
        view_root_url = url_for_path("view-root")
        view_base_url = url_for_path("view", os_doc_id="__ID__").removesuffix("/__ID__")
        edit_base_url = url_for_path("edit-view", os_doc_id="__ID__").removesuffix("/__ID__")
        ocr_base_url = url_for_path("ocr-view", os_doc_id="__ID__").removesuffix("/__ID__")

        html = render_page(
            os_doc_id="",
            page="",
            pages=[],
            image_url=None,
            text="[no text selected]",
            prev_page=None,
            next_page=None,
            discover_url=None,
            editable=editable,
            save_url=None,
            page_url_base=view_base_url,
            edit_url=None,
            ocr_url=None,
            current_framework="kraken",
            root_path=root_path,
        )
        logger.info(f"Building: {view_root_url} for {raw_os_doc_id}")
        return HTMLResponse(html)

    doc_key, page = split_doc_id(raw_os_doc_id)
    doc_id_only = raw_os_doc_id.rsplit(":", 1)[0]

    img_path = image_file(doc_key, page)
    txt_path = text_file(doc_key, page)
    pages = list_pages(doc_key)

    if not pages and not img_path.exists() and not txt_path.exists():
        raise HTTPException(status_code=404, detail=f"Image {img_path} and text {txt_path} not found")

    image_url = None
    from urllib.parse import quote

    if img_path.exists():
        from .viewer import image_root

        image_rel_path = quote(f"{doc_key}/{page}.{IMAGE_EXT}", safe="/")
        image_url = request.app.url_path_for("image-files", path=f"/{image_rel_path}")
        logger.debug(f"img_path: {img_path}")
        logger.debug(f"image_root: {image_root}")
        logger.debug(f"image_rel_path: {image_rel_path}")
        logger.debug(f"image_url: {image_url}")


    if txt_path.exists():
        try:
            text = txt_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning(f"Could not read text file {txt_path}: {exc}")
            text = "[error reading text]"
    else:
        text = None

    if not text:
        try:
            from .db import get_os_client
            from .search import DEFAULT_ALIAS

            client = get_os_client()

            result = client.get(
                index=DEFAULT_ALIAS,
                id=raw_os_doc_id,
            )

            text = result["_source"].get("text", "[no text in index]")

        except Exception as exc:
            logger.warning(f"Could not load text from OpenSearch for {raw_os_doc_id}: {exc}")
            text = "[no text on this page]"

    prev_page = None
    next_page = None
    if page in pages:
        idx = pages.index(page)
        if idx > 0:
            prev_page = pages[idx - 1]
        if idx < len(pages) - 1:
            next_page = pages[idx + 1]

    view_url = url_for_path("view", os_doc_id=f"{doc_id_only}:{page}")
    edit_url = url_for_path("edit-view", os_doc_id=f"{doc_id_only}:{page}")
    save_url = url_for_path("save-view", os_doc_id=f"{doc_id_only}:{page}")
    ocr_url = url_for_path("ocr-view", os_doc_id=f"{doc_id_only}:{page}")
    view_base_url = url_for_path("view", os_doc_id="__ID__").removesuffix("/__ID__")

    logger.info(f"Building: {view_url} for {raw_os_doc_id}")

    html = render_page(
        os_doc_id=doc_id_only,
        page=page,
        pages=pages,
        image_url=image_url,
        text=text,
        prev_page=prev_page,
        next_page=next_page,
        discover_url=discover_doc_url(raw_os_doc_id),
        editable=editable,
        save_url=save_url if editable else None,
        page_url_base=view_base_url,
        edit_url=edit_url if not editable else None,
        ocr_url=ocr_url,
        current_framework="kraken",
        root_path=root_path,
    )
    return HTMLResponse(html)

@open_router.get(
    "/ocr-view/{os_doc_id:path}",
    name = "ocr-view",
    summary="Run OCR again for page image",
    tags=["Viewer"],
)
def rerun_ocr(
    os_doc_id: str,
    framework: Literal["kraken", "tesseract", "transformer", "source", "none"] = Query("kraken"),
):
    from .viewer import split_doc_id
    from .ocr import page_to_text, PageItem

    def build_local_page_item(
        doc_key: str,
        page: str,
        *,
        crop_box: tuple[int, int, int, int] | None = None,
        blur_radius: float | None = None,
    ) -> PageItem:
        from .viewer import image_file
        from PIL import Image, ImageFilter

        img_path = image_file(doc_key, page)
        if not img_path.exists():
            raise FileNotFoundError(img_path)

        with Image.open(img_path) as img:
            pil_img = img.convert("RGB")

            # optional crop
            if crop_box:
                w, h = pil_img.size

                # crop_box = (left%, top%, right%, bottom%)
                left_p, top_p, right_p, bottom_p = crop_box

                left   = int(w * left_p)
                top    = int(h * top_p)
                right  = int(w * (1 - right_p))
                bottom = int(h * (1 - bottom_p))

                pil_img = pil_img.crop((left, top, right, bottom))

            # optional blur
            if blur_radius and blur_radius > 0:
                pil_img = pil_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))

            pil_img = pil_img.copy()

        return PageItem(
            index=int(page) if str(page).isdigit() else 0,
            kind="image",
            data=pil_img,
            source=str(img_path),
            total=1,
        )

    original_os_doc_id = (os_doc_id or "").strip()
    doc_key, page = split_doc_id(original_os_doc_id)

    try:
        page_item = build_local_page_item(
            doc_key, page,
            # crop_box = (0.0, 0.0, 0.0, 0.13),
            # blur_radius=1
            )

        logger.info(f"Built image from {doc_key}, {page}")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Page image not found")
    except Exception as exc:
        logger.exception(f"Could not load page image for OCR: {exc}")
        raise HTTPException(status_code=500, detail="Could not load page image")

    if page_item.kind != "image":
        return JSONResponse({"text": page_item.data or "", "framework": framework})

    try:
        logger.info(f"Getting OCR: {framework}")
        text = page_to_text(page_item, framework=framework)
        logger.info(f"OCR: {text}")
    except Exception as exc:
        logger.exception(f"OCR failed for {original_os_doc_id} with {framework}: {exc}")
        raise HTTPException(status_code=500, detail="Could not run OCR")

    return JSONResponse({
        "text": text or "",
        "framework": framework,
    })

# Proxies

@router.post("/proxy/ollama", tags=["Proxy"],)
async def ollama_proxy(
    user_input: str = Body(..., embed=True),
    llm_kwargs: dict = Body(default_factory=dict, embed=True),
):
    logger.info("Initializing ollama...")
    from .analysis.llm import llm
    from .helpers import resolve_config_path

    try:
        llm_kwargs = dict(llm_kwargs or {})
        llm_kwargs["config_path"] = llm_kwargs.get("config_path", resolve_config_path())
        logger.info("Receiving llm response...")
        raw = llm(user_input=user_input, llm_kwargs=llm_kwargs)

        # --- JSON Parsing ---
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=502,
                detail=f"Model did not return valid JSON: {raw}"
            )

        if not isinstance(parsed, list):
            raise HTTPException(
                status_code=502,
                detail=f"Model JSON is not a list: {raw}"
            )

        return parsed

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ollama proxy failed: {e}")

@open_router.get("/proxy/search/{index}/_mapping", tags=["Proxy"])
async def get_mapping(index: str):
    from .db import get_os_client
    client = get_os_client()
    return client.indices.get_mapping(index=index)

@open_router.post("/proxy/search/_msearch", tags=["Proxy"])
async def msearch(request: Request):
    body = await request.body()
    from .db import get_os_client
    client = get_os_client()
    resp = client.transport.perform_request(
        method="POST",
        url="/_msearch",
        body=body,
        headers={"content-type": "application/x-ndjson"},
    )
    return resp

@router.delete("/proxy/index", tags=["Proxy"])
async def delete_os_indices(payload: dict = Body(...)):
    from .db import get_os_client

    client = get_os_client()

    indices = payload.get("indices")
    if isinstance(indices, str):
        indices = [indices]

    if not indices:
        raise HTTPException(status_code=400, detail="Missing indices")

    response = client.indices.delete(
        index=",".join(indices),
        ignore=[400, 404],
    )

    return {
        "deleted": indices,
        "response": response,
    }

## Cleaning

class CleanRequest(BaseModel):
    root_dir: str
    extension: str
    action: Literal["delete", "move", "copy"] = "delete"
    move_to: str | None = None

    min_bytes: int | None = None
    max_bytes: int | None = None

    min_content_len: int | None = None
    max_content_len: int | None = None

    all_files: bool = False


@router.post("/clean-files")
def clean_files_endpoint(payload: CleanRequest):
    from zotero_rdf_server.config import EXPORT_DIRECTORY
    from .pipeline import clean_files

    export_root = Path(EXPORT_DIRECTORY).resolve()
    root_dir = (export_root / payload.root_dir).resolve()

    move_to: Path | None = None

    if payload.move_to:
        move_to = (export_root / payload.move_to).resolve()

    # Prevent path traversal outside EXPORT_DIRECTORY.
    if not root_dir.is_relative_to(export_root):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"root_dir must be inside {EXPORT_DIRECTORY}",
        )

    if move_to is not None and not move_to.is_relative_to(export_root):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"move_to must be inside {EXPORT_DIRECTORY}",
        )

    if not root_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Directory not found: {payload.root_dir}",
        )

    if not root_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path is not a directory: {payload.root_dir}",
        )

    try:
        return clean_files(
            root_dir=root_dir,
            extension=payload.extension,
            action=payload.action,
            move_to=move_to,
            min_bytes=payload.min_bytes,
            max_bytes=payload.max_bytes,
            min_content_len=payload.min_content_len,
            max_content_len=payload.max_content_len,
            all_files=payload.all_files,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Filesystem error: {exc}",
        ) from exc

# end