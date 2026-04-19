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
    items: list = [],
    targets: str | list = [],
    from_source: bool = True,
    framework: TypeLiteral["kraken", "tesseract", "transformer", "source", "none"] = "kraken",
    vector_kwargs: dict | None = None,
    llm_kwargs: dict | None = None,
    ingest: bool = True,
    iter_pages_kwargs: dict = {},
    page_to_text_kwargs: dict = {},
    text_image_file_kwargs: dict = {},
    config_path: str = None
):
    from .db import index_stream
    items = list(items or [])
    total = len(items)
    targets = targets or []
    iter_pages_kwargs = dict(iter_pages_kwargs or {})
    page_to_text_kwargs = dict(page_to_text_kwargs or {})
    vector_kwargs = dict(vector_kwargs or {})
    text_image_file_kwargs = dict(text_image_file_kwargs or {})
    llm_kwargs = dict(llm_kwargs or {})
    # rag_kwargs = dict(rag_kwargs or {})
    logger.info(f"Ingest Pipeline started with {len(items)} items using framework={framework}...")

    page_to_text_kwargs['config_path'] = config_path if (not page_to_text_kwargs.get('config_path') and config_path) else page_to_text_kwargs.get('config_path')
    vector_kwargs['config_path'] = config_path if (not vector_kwargs.get('config_path') and config_path) else vector_kwargs.get('config_path')
    llm_kwargs['config_path'] = config_path if (not llm_kwargs.get('config_path') and config_path) else llm_kwargs.get('config_path')

    logger.info(
        "Pipeline configuration:\n"
        "iter_pages_kwargs:\n%s\n\n"
        "page_to_text_kwargs:\n%s\n\n"
        "text_image_file_kwargs:\n%s",
        json.dumps(iter_pages_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(page_to_text_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(text_image_file_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
    )

    if not from_source and not ingest:
        return([{"error":"nothing to do here: no from_source, no ingest!"}])
    
    vector = isinstance(vector_kwargs, dict) and vector_kwargs.get('framework')
    if vector:
        from .analysis.vector import embed
        from .helpers import clean_ocr    

    
    use_llm = isinstance(llm_kwargs, dict) and llm_kwargs.get('config_path')
    
    if use_llm:
        from .analysis.llm import llm
        llm_mapping_key = llm_kwargs.pop('mapping_key','llm')        

    if from_source:
        from .ocr import iter_text_pages, PdfTextPolicy, IiifOcrPolicy, TextPolicy, HtmlPolicy, XmlPolicy, JsonPolicy

        ptp = iter_pages_kwargs.get("pdf_text_policy")
        if isinstance(ptp, dict):
            iter_pages_kwargs["pdf_text_policy"] = PdfTextPolicy.from_json(ptp)
        
        iiif_ocr_policy = iter_pages_kwargs.get("iiif_ocr_policy")
        if isinstance(iiif_ocr_policy, dict):
            iter_pages_kwargs["iiif_ocr_policy"] = IiifOcrPolicy.from_json(iiif_ocr_policy)

        text_policy = iter_pages_kwargs.get("text_policy")
        if isinstance(text_policy, dict):
            iter_pages_kwargs["text_policy"] = TextPolicy.from_json(text_policy)  

        html_policy = iter_pages_kwargs.get("html_policy")
        if isinstance(html_policy, dict):
            iter_pages_kwargs["html_policy"] = HtmlPolicy.from_json(html_policy)  

        xml_policy = iter_pages_kwargs.get("xml_policy")
        if isinstance(xml_policy, dict):
            iter_pages_kwargs["xml_policy"] = XmlPolicy.from_json(xml_policy) 

        json_policy = iter_pages_kwargs.get("json_policy")
        if isinstance(json_policy, dict):
            iter_pages_kwargs["json_policy"] = JsonPolicy.from_json(json_policy) 

        def make_pages_fn(doc_id: str, stats: dict):
            def pages_fn(u: str):
                try:
                    for page in iter_text_pages(
                        u,
                        doc_id=doc_id,
                        iter_kwargs=iter_pages_kwargs,
                        page_to_text_kwargs=page_to_text_kwargs,
                        text_image_file_kwargs=text_image_file_kwargs,
                        framework=framework,
                        yield_result=ingest
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
                              
                stats = {"pages_emitted": 0}
                payload = dict(obj)
                doc_id = payload.pop("_id", None)
                input_ = payload.pop("_input", None) or payload.pop("_url", None)
                label = payload.pop("_label", "no label")
                meta = _meta_flat_strings(payload)
                logger.info(f"\n\n[{i}/{total}] Loading {obj.get('_id')}\n{label}\n\n")  
                if not input_:
                    results.append({
                        "doc_id": doc_id,
                        "label": label,
                        "from_source": True,
                        "vector": vector,
                        "llm":use_llm,
                        "ingest": False,
                        "error": "from_source=true requires '_input' in each item",
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
                            vector_doc = embed(clean_ocr(text),**vector_kwargs)
                            logger.debug(vector_doc)
                            item["vector"] = vector_doc

                        if use_llm:                            
                            llm_response = llm(clean_ocr(text), llm_kwargs)
                            logger.debug(llm_response)
                            from zotero_rdf_server.utils import load_dict_like
                            llm_dict = load_dict_like(llm_response)
                            item[llm_mapping_key] = llm_dict

                        pages.append(item)

                except Exception as e:
                    results.append({
                        "doc_id": doc_id,
                        "label": label,
                        "input": input_,
                        "meta": meta,
                        "from_source": True,
                        "vector": vector,
                        "llm":use_llm,
                        "ingest": False,
                        "error": str(e),
                    })
                    continue

                results.append({
                    "doc_id": doc_id,
                    "label": label,
                    "input": input_,
                    "meta": meta,
                    "from_source": True,
                    "framework": framework,
                    "vector": vector,
                    "llm":use_llm,
                    "ocr_pages": len(pages),
                    "ingest": False,
                    "targets": targets,
                })
            logger.info(f"Pipeline finsihed with {len(results)} results!")

            return results  
          
    runs: List[dict] = []
    if ingest:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()        

        for i, obj in enumerate(items, start=1):            
            stats = {"pages_emitted": 0}
            payload = dict(obj)
            logger.debug(f"Ingest Pipeline payload: {payload}")
            doc_id = payload.pop("_id", None)
            input = payload.pop("_input", None)
            # iri = payload.pop("_iri", None)
            text = payload.pop("_text", "")
            sequence = payload.pop("_idx", 1)
            label = payload.pop("_label", "no label")
            meta = _meta_flat_strings(payload)
            logger.info(f"\n\n[{i}/{total}] Loading {obj.get('_id')}\n{label}\n\n")  
            logger.debug(f"Ingest Pipeline index_stream from source: {from_source}")
            if from_source:
                if not input:
                    logger.error("from_source=true requires '_input' in each item")
                    continue
                logger.info(f"Ingest Pipeline with input from source!")
                digest = index_stream(
                        input=input,
                        doc_id=doc_id,
                        label=label,
                        url_to_text_pages_fn=make_pages_fn(doc_id or "", stats),
                        targets=targets,
                        meta=meta,
                        config_path=config_path,
                        vector_kwargs=vector
                    )         
                digest["from_source"] = True
                digest["framework"] = framework     
                digest["ocr_pages"] = stats["pages_emitted"]
                digest["ingest"] = True
                runs.append(digest)
            else:
                logger.info(f"Ingest Pipeline with no input from source!")
                d: Dict[str, Any] = {"ingest_ts": now, "meta": meta}
                if input is not None:
                    d["input"] = input
                if doc_id is not None:
                    d["doc_id"] = doc_id
                if sequence is not None:
                    d["page"] = sequence
                if text != "":
                    d["text"] = text
                if label != "":
                    d["label"] = label
                if vector:
                    vector_doc = embed(clean_ocr(text),**vector_kwargs)
                    d["vector"] = vector_doc
                if use_llm:                            
                    llm_response = llm(clean_ocr(text), llm_kwargs)
                    logger.debug(llm_response)
                    d[llm_mapping_key] = llm_response
                digest = index_stream(
                        targets=targets,
                        doc_id=doc_id,
                        doc=d,
                        config_path=config_path
                    )
                
                digest["from_source"] = False
                digest["framework"] = framework
                digest["ingest"] = True
                runs.append(digest)

        logger.info(f"Ingest Pipeline finsihed with {len(runs)} runs!")
        return runs
    return runs