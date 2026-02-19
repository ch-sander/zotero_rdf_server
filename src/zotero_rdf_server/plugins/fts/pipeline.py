from typing import Literal as TypeLiteral, Any, Dict, Iterator, List, Optional, Union
import json
from .helpers import plugin_logger
logger=plugin_logger()

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
    transformer:bool=False,
    vector: bool = True,
    ingest:bool=True,
    iter_pages_kwargs:dict={},
    page_to_text_kwargs:dict={},
    text_image_file_kwargs:dict={},
    config_path:str=None
):
    from .db import index_stream
    items = list(items or [])
    total = len(items)
    targets = targets or []
    iter_pages_kwargs = dict(iter_pages_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    logger.info(f"Ingest Pipeline started with {len(items)} items...")
    
    page_to_text_kwargs['config_path'] = config_path if (not page_to_text_kwargs.get('config_path') and config_path) else page_to_text_kwargs.get('config_path')

    logger.info(
        "Pipeline configuration:\n"
        "iter_pages_kwargs:\n%s\n\n"
        "page_to_text_kwargs:\n%s\n\n"
        "text_image_file_kwargs:\n%s",
        json.dumps(iter_pages_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(page_to_text_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(text_image_file_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
    )

    if not ocr and not ingest:
        return([{"error":"nothing to do here: no ocr, no ingest!"}])
    
    if vector:
        logger.info("####### Loading Sentence Transformer Model for Vectors #######")
        from .vector import embed
        from .helpers import clean_ocr

    

    if ocr:
        from .ocr import iter_text_pages, PdfTextPolicy, IiifOcrPolicy     

        ptp = iter_pages_kwargs.get("pdf_text_policy")
        if isinstance(ptp, dict):
            iter_pages_kwargs["pdf_text_policy"] = PdfTextPolicy.from_json(ptp)
        
        iiif_ocr_policy = iter_pages_kwargs.get("iiif_ocr_policy")
        if isinstance(iiif_ocr_policy, dict):
            iter_pages_kwargs["iiif_ocr_policy"] = IiifOcrPolicy.from_json(iiif_ocr_policy)

        def make_pages_fn(doc_id: str, stats: dict):
            def pages_fn(u: str):
                try:
                    for page in iter_text_pages(
                        u,
                        doc_id=doc_id,
                        iter_kwargs=iter_pages_kwargs,
                        page_to_text_kwargs=page_to_text_kwargs,
                        text_image_file_kwargs=text_image_file_kwargs,
                        transformer=transformer
                    ):
                        stats["pages_emitted"] += 1
                        yield page
                except Exception:
                    logger.exception("pages_fn failed for doc_id=%s input=%r", doc_id, u)
                    raise
            return pages_fn
        
        if not ingest:
            results: List[Dict[str, Any]] = []
            for i, obj in enumerate(items, start=1):
                logger.info(f"[{i}/{total}] Loading {obj.get('_id')}")                
                stats = {"pages_emitted": 0}
                payload = dict(obj)
                doc_id = payload.pop("_id", None)
                input_ = payload.pop("_input", None) or payload.pop("_url", None)
                meta = _meta_flat_strings(payload)

                if not input_:
                    results.append({
                        "doc_id": doc_id,
                        "ocr": True,
                        "vector": vector,
                        "ingest": False,
                        "error": "ocr=true requires '_input' in each item",
                    })
                    continue

                pages = []
                try:
                    for page_no, text in make_pages_fn(doc_id or "", stats)(input_):
                        item = {
                            "page": int(page_no),
                            "text": text,
                        }
                        if vector:
                            vector_doc = embed(clean_ocr(text))
                            logger.debug(vector_doc)
                            item["vector"] = vector_doc

                        pages.append(item)

                except Exception as e:
                    results.append({
                        "doc_id": doc_id,
                        "input": input_,
                        "meta": meta,
                        "ocr": True,
                        "vector": vector,
                        "ingest": False,
                        "error": str(e),
                    })
                    continue

                results.append({
                    "doc_id": doc_id,
                    "input": input_,
                    "meta": meta,
                    "ocr": True,
                    "vector": vector,
                    "transformer": bool(transformer),
                    "ocr_pages": len(pages),
                    "ingest": False,
                    "targets": targets,
                    # "pages": pages, # Too big for result
                })
            logger.info(f"OCR Pipeline finsihed with {len(results)} results!")

            return results  
          
    runs: List[dict] = []
    if ingest:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()        

        for i, obj in enumerate(items, start=1):
            logger.info(f"[{i}/{total}] Loading {obj.get('_id')}") 
            stats = {"pages_emitted": 0}
            payload = dict(obj)
            logger.debug(f"Ingest Pipeline payload: {payload}")
            doc_id = payload.pop("_id", None)
            input = payload.pop("_input", None)
            # iri = payload.pop("_iri", None)
            text = payload.pop("_text", "")
            sequence = payload.pop("_idx", 1)

            meta = _meta_flat_strings(payload)

            logger.debug(f"Ingest Pipeline index_stream with OCR: {ocr}")
            if ocr:
                if not input:
                    logger.error("ocr=true requires '_url' in each item")
                    continue
                
                digest = index_stream(
                        input=input,
                        doc_id=doc_id,
                        url_to_text_pages_fn=make_pages_fn(doc_id or "", stats),
                        targets=targets,
                        meta=meta,
                        config_path=config_path,
                        vector=vector
                    )         
                digest["ocr"] = True
                digest["transformer"] = bool(transformer)       
                digest["ocr_pages"] = stats["pages_emitted"]
                digest["ingest"] = True
                runs.append(digest)
            else:
                d: Dict[str, Any] = {"ingest_ts": now, "meta": meta}
                if input is not None:
                    d["input"] = input
                if doc_id is not None:
                    d["doc_id"] = doc_id
                if sequence is not None:
                    d["page"] = sequence
                if text != "":
                    d["text"] = text
                if vector:
                    vector_doc = embed(clean_ocr(text))
                    d["vector"] = vector_doc

                digest = index_stream(
                        targets=targets,
                        doc_id=doc_id,
                        doc=d,
                        config_path=config_path
                    )
                
                digest["ocr"] = False
                digest["transformer"] = bool(transformer)
                digest["ingest"] = True
                runs.append(digest)

        logger.info(f"Ingest Pipeline finsihed with {len(runs)} runs!")
        return runs
    return runs