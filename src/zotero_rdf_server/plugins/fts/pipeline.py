from fastapi import FastAPI, Request, Query, Form, HTTPException, APIRouter, Body
from fastapi.responses import StreamingResponse, FileResponse
from typing import Literal as TypeLiteral, Any, Dict, Iterator, List, Optional, Union
import json
from .helpers import plugin_logger
logger=plugin_logger()
from .helpers import plugin_logger

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


def ingest_pipeline(        
    items:list=[],
    targets:str|list=[],
    ocr:bool=False,
    iter_pages_kwargs:dict={},
    page_to_text_kwargs:dict={},
    text_image_file_kwargs:dict={},
    config_path:str=None
):
    from .db import index_stream
    pages_fn = None
    iter_pages_kwargs = dict(iter_pages_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    logger.debug(f"Ingest Pipeline started with {len(items)} items...")
    page_to_text_kwargs['config_path'] = config_path if (not page_to_text_kwargs.get('config_path') and config_path) else page_to_text_kwargs.get('config_path')
    if ocr:
        from .ocr import iter_text_pages, PdfTextPolicy
        


        ptp = iter_pages_kwargs.get("pdf_text_policy")
        if isinstance(ptp, dict):
            iter_pages_kwargs["pdf_text_policy"] = PdfTextPolicy.from_json(ptp)

        def pages_fn(u: str):
            yield from iter_text_pages(
                u,
                doc_id=doc_id,
                iter_kwargs=iter_pages_kwargs,
                page_to_text_kwargs=page_to_text_kwargs,
                text_image_file_kwargs=text_image_file_kwargs,  # or None
            )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    run_ids: List[str] = []

    for obj in items:
        payload = dict(obj)
        logger.debug(f"Ingest Pipeline payload: {payload}")
        doc_id = payload.pop("_id", None)
        url = payload.pop("_url", None)
        # iri = payload.pop("_iri", None)
        text = payload.pop("_text", "")
        sequence = payload.pop("_idx", 1)

        meta = _meta_flat_strings(payload)

        logger.debug(f"Ingest Pipeline index_stream with OCR: {ocr}")
        if ocr:
            if not url:
                logger.error("ocr=true requires '_url' in each item")
                continue
            
            run_ids.append(
                index_stream(
                    url=url,
                    doc_id=doc_id,
                    url_to_text_pages_fn=pages_fn,  # type: ignore[arg-type]
                    targets=targets,
                    meta=meta,
                    config_path=config_path
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
                    config_path=config_path
                )
            )
    logger.debug(f"Ingest Pipeline finsihed with {len(run_ids)} runs!")
    return run_ids