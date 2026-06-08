from typing import Literal as TypeLiteral, Any, Dict, Iterator, List, Optional, Union
from pathlib import Path
from shutil import move, copy2

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
    delete_index: bool = True,
    iter_pages_kwargs: dict = {},
    page_to_text_kwargs: dict = {},
    text_image_file_kwargs: dict = {},
    config_path: str = None,
    pipeline_meta:dict = {},
):
    from .db import index_stream
    from zotero_rdf_server.utils import load_dict_like

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
        "text_image_file_kwargs:\n%s\n\n"
        "llm_kwargs:\n%s\n",
        json.dumps(iter_pages_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(page_to_text_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(text_image_file_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
        json.dumps(llm_kwargs, indent=2, sort_keys=True, ensure_ascii=False),
    )

    if not from_source and not ingest:
        return([{"error":"nothing to do here: no from_source, no ingest!"}])
    
    vector = isinstance(vector_kwargs, dict) and vector_kwargs.get('framework')
    if vector:
        from .analysis.vector import embed
        from .helpers import clean_ocr    

    
    use_llm = isinstance(llm_kwargs, dict) and llm_kwargs.get('tasks')
    if use_llm:
        logger.warning("LLM active!")
        from .analysis.llm import llm                

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

        pipeline_meta['len_items'] = total


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
                        yield_result=ingest,
                        pipeline_meta = pipeline_meta
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
                pipeline_meta['i_items'] = i
                pipeline_meta['label_items'] = label
                logger.info(f"\n\n[{i}/{total}] Loading {obj.get('_id')}\n{label}\n\n")  
                if not input_:
                    results.append({
                        "doc_id": doc_id,
                        "label": label,
                        "from_source": True,
                        "vector": vector,
                        "llm":use_llm,
                        "ingest": False,
                        "delete_index": delete_index,
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
                        if vector: # TODO why apply vector?
                            vector_doc = embed(clean_ocr(text),**vector_kwargs)
                            logger.debug(vector_doc)
                            item["vector"] = vector_doc

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
                        "delete_index": False,
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
                    "delete_index": False,
                })
            logger.info(f"Pipeline finsihed with {len(results)} results!")

            return results  
          
    runs: List[dict] = []
    if ingest:
        from .db import resolve_config_path, get_os_config, make_client, provision_from_cfg

        resolve_config_path.cache_clear()
        get_os_config.cache_clear()
        
        cfg_path = resolve_config_path(config_path)
        oscfg = get_os_config(cfg_path)
        client = make_client(oscfg)
        try:
            logger.info(f"Provisioning {targets}...")
            provision_from_cfg(client, oscfg)
            logger.info("Provisioning completed!")
        except Exception as e:
            logger.critical(f"Open Search failed: {e}. Open Search running?")

        if delete_index:
            targets_list = [targets] if isinstance(targets, str) else list(targets)
            for t in targets_list:
                response = client.indices.delete(index=str(t), ignore=[400, 404])
                logger.warning(f"Deleted Index {t}: {response}")

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
            pipeline_meta['i_items'] = i
            pipeline_meta['label_items'] = label
            logger.info(f"\n\n[{i}/{total}] Loading {obj.get('_id')}\n{label}\n\n")  
            logger.debug(f"Ingest Pipeline index_stream from source: {from_source}")
            if from_source:
                if not input:
                    logger.error("from_source=true requires '_input' in each item")
                    continue
                logger.info(f"Ingest Pipeline with input from source!")
                digest = index_stream(
                        client=client,
                        oscfg=oscfg,
                        input=input,
                        doc_id=doc_id,
                        label=label,
                        url_to_text_pages_fn=make_pages_fn(doc_id or "", stats),
                        targets=targets,
                        meta=meta,
                        vector_kwargs=vector,
                        llm_kwargs=llm_kwargs,
                    )         
                digest["from_source"] = True
                digest["framework"] = framework     
                digest["ocr_pages"] = stats["pages_emitted"]
                digest["ingest"] = True
                digest["delete_index"] = delete_index
                digest["llm"] = True
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
                if use_llm: # TODO adjust          
                    llm_mapping_key = llm_kwargs.pop('mapping_key','llm')
                    llm_mapping_keys = llm_kwargs.pop('mapping_keys', None) or [llm_mapping_key]
                    llm_response = llm(clean_ocr(text), llm_kwargs)                    
                    logger.debug(llm_response)
                    llm_dict = load_dict_like(llm_response)
                    if llm_dict:                        
                        for key in llm_mapping_keys:
                            d[key] = llm_response
                        logger.debug(json.dumps(llm_dict,indent=4))

                digest = index_stream(
                        client=client,
                        oscfg=oscfg,
                        targets=targets,
                        doc_id=doc_id,
                        doc=d,
                    )
                
                digest["from_source"] = False
                digest["framework"] = framework
                digest["ingest"] = True
                digest["delete_index"] = delete_index
                digest["llm"] = True
                runs.append(digest)

        logger.info(f"Ingest Pipeline finsihed with {len(runs)} runs!")
        return runs
    return runs



Action = TypeLiteral["delete", "move", "copy"]


def clean_files(
    root_dir: str | Path,
    extension: str,
    min_bytes: int | None = None,
    min_content_len: int | None = None,
    action: Action = "delete",
    move_to: str | Path | None = None,
    all_files: bool = False
) -> dict:
    """
    Recursively finds files by extension and deletes or moves files that are
    smaller than min_bytes or whose decoded content length is smaller than
    min_content_len.
    """

    root = Path(root_dir).resolve()

    if not root.exists() or not root.is_dir():
        raise ValueError(f"root_dir does not exist or is not a directory: {root}")

    if not all_files and min_bytes is None and min_content_len is None:
        raise ValueError("At least one of all_files, min_bytes or min_content_len must be set")

    if not extension.startswith("."):
        extension = f".{extension}"

    target_dir: Path | None = None

    if action in {"move", "copy"}:
        if move_to is None:
            raise ValueError(
                "move_to must be set when action='move' or action='copy'"
            )

        target_dir = Path(move_to).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        
    deleted = 0
    moved = 0
    copied = 0
    skipped = 0
    errors: list[dict] = []

    for file_path in root.rglob(f"*{extension}"):
        if not file_path.is_file():
            continue

        should_clean = all_files

        try:
            # Check file size in bytes without reading the file.
            if min_bytes is not None and file_path.stat().st_size < min_bytes:
                should_clean = True

            # Check content length only if needed.
            if min_content_len is not None:
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if len(content) < min_content_len:
                        should_clean = True
                except OSError as exc:
                    errors.append({"file": str(file_path), "error": str(exc)})
                    continue

            if not should_clean:
                skipped += 1
                continue

            if action == "delete":
                file_path.unlink()
                deleted += 1

            elif action in {"move", "copy"}:
                assert target_dir is not None

                relative_path = file_path.relative_to(root)
                destination = target_dir / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)

                if action == "move":
                    move(str(file_path), str(destination))
                    moved += 1

                else:
                    copy2(str(file_path), str(destination))
                    copied += 1

        except OSError as exc:
            errors.append({"file": str(file_path), "error": str(exc)})

    return {
        "root_dir": str(root),
        "extension": extension,
        "action": action,
        "deleted": deleted,
        "moved": moved,
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
    }