from __future__ import annotations
from .helpers import ensure_import, resolve_config_path, plugin_logger, write_data_to_file, safe_doc_id
ensure_import("opensearchpy")
import os
import yaml
import uuid
from datetime import datetime, timezone
from typing import Dict, Iterator, Tuple, Any, Iterable, Callable, Optional
from opensearchpy import OpenSearch
from opensearchpy.helpers import streaming_bulk
from functools import lru_cache
from pathlib import Path
from collections import Counter
import logging

logger=plugin_logger()


@lru_cache(maxsize=8)
def get_os_config(config_path: Path) -> dict[str, Any]:
    # import yaml
    from zotero_rdf_server.utils import load_dict_like
    cfg = load_dict_like(config_path,label="Open Search Config",verbose=False)
    # path = Path(config_path).expanduser().resolve()
    # with path.open("r", encoding="utf-8") as f:
    #     cfg = yaml.safe_load(f) or {}
    # cfg = load_dict_like(path, "Open Search YAML")
    return cfg.get("open-search") or cfg


def make_client(cfg: dict) -> OpenSearch:
    if "client" not in cfg:
        raise ValueError("Missing 'client' configuration")
    try:
        cli = OpenSearch(**cfg["client"])
        logger.info("OS client created")
        return cli
    except Exception as e:
        logger.critical("Client failed. Service running?")
        logger.info(e)

def ensure_ingest_pipeline(client: OpenSearch, *, name: str, body: dict) -> None:
    logger.debug("putting ingest_pipeline")
    client.ingest.put_pipeline(id=name, body=body)

def ensure_component_template(client: OpenSearch, *, name: str, body: dict) -> None:
    """
    PUT _component_template/<name>
    body should be the component template body (e.g. {"template": {...}, ...})
    """
    # opensearchpy has indices.put_component_template in newer versions; fallback to perform_request for compatibility
    logger.debug("putting component_template")
    try:
        client.cluster.put_component_template(name=name, body=body)
    except AttributeError:
        client.transport.perform_request(
            method="PUT",
            url=f"/_component_template/{name}",
            body=body,
        )

def ensure_index_template(client: OpenSearch, *, name: str, body: dict) -> None:
    """
    PUT _index_template/<name>
    body should be the composable index template body
    """
    logger.debug("putting index_template")
    try:
        client.indices.put_index_template(name=name, body=body)
    except AttributeError:
        client.transport.perform_request(
            method="PUT",
            url=f"/_index_template/{name}",
            body=body,
        )


def ensure_index_from_schema(client: OpenSearch, *, index: str, index_def: dict) -> None:
    """
    Create a concrete index with a small body (aliases/settings overrides),
    while mappings/settings/analysis come from templates matched by index name.
    """
    if client.indices.exists(index=index):
        return

    # Accept your YAML "indices.<index_name>" structure directly.
    # Only pass through fields OpenSearch accepts for index creation.
    body: dict = {}

    if "settings" in index_def:
        body["settings"] = index_def["settings"]

    if "aliases" in index_def:
        body["aliases"] = index_def["aliases"]

    if "mappings" in index_def:
        # Rarely needed with templates, but allow it
        body["mappings"] = index_def["mappings"]

    # If body ends up empty, create index with no body; template still applies.
    if body:
        client.indices.create(index=index, body=body)
    else:
        client.indices.create(index=index)
    logger.debug("ensured index from schema")

def provision_from_cfg(client: OpenSearch, cfg: dict) -> None:
    """
    Provision resources described in the YAML schema:
    - component_templates
    - ingest_pipelines
    - index_templates
    - indices (concrete indices)
    If cfg contains 'plan', follow it; otherwise do a best-effort default order.
    """

    component_templates: dict = cfg.get("component_templates", {}) or {}
    ingest_pipelines: dict = cfg.get("ingest_pipelines", {}) or {}
    index_templates: dict = cfg.get("index_templates", {}) or {}
    indices: dict = cfg.get("indices", {}) or {}

    plan = cfg.get("plan")

    def put_component_templates(names: Iterable[str]) -> None:
        for name in names:
            ensure_component_template(client, name=name, body=component_templates[name])

    def put_ingest_pipelines(names: Iterable[str]) -> None:
        for name in names:
            ensure_ingest_pipeline(client, name=name, body=ingest_pipelines[name])

    def put_index_templates(names: Iterable[str]) -> None:
        for name in names:
            ensure_index_template(client, name=name, body=index_templates[name])

    def create_indices(names: Iterable[str]) -> None:
        for name in names:
            ensure_index_from_schema(client, index=name, index_def=indices[name])

    if plan:
        for step in plan:
            logger.debug("starting step: %s", step)

            if "put_component_templates" in step:
                for name in step["put_component_templates"]:
                    logger.debug("PUT component_template %s", name)
                    ensure_component_template(client, name=name, body=component_templates[name])
                    logger.debug("DONE component_template %s", name)

            elif "put_ingest_pipelines" in step:
                for name in step["put_ingest_pipelines"]:
                    logger.debug("PUT ingest_pipeline %s", name)
                    ensure_ingest_pipeline(client, name=name, body=ingest_pipelines[name])
                    logger.debug("DONE ingest_pipeline %s", name)

            elif "put_index_templates" in step:
                for name in step["put_index_templates"]:
                    logger.debug("PUT index_template %s", name)
                    ensure_index_template(client, name=name, body=index_templates[name])
                    logger.debug("DONE index_template %s", name)

            elif "create_indices" in step:
                for name in step["create_indices"]:
                    logger.debug("CREATE index %s", name)
                    ensure_index_from_schema(client, index=name, index_def=indices[name])
                    logger.debug("DONE index %s", name)
        return

    # Fallback: best-effort default order (safe for most cases)
    if component_templates:
        put_component_templates(component_templates.keys())
    if ingest_pipelines:
        put_ingest_pipelines(ingest_pipelines.keys())
    if index_templates:
        put_index_templates(index_templates.keys())
    if indices:
        create_indices(indices.keys())


def apply_ingest_tuning(client: OpenSearch, *, index: str, refresh_interval: str, replicas: int) -> None:
    client.indices.put_settings(
        index=index,
        body={"index": {"refresh_interval": refresh_interval, "number_of_replicas": replicas}},
    )

try:
    from zotero_rdf_server.config import EXPORT_DIRECTORY
    EXPORT_DIRECTORY = Path(EXPORT_DIRECTORY)
except Exception:    
    EXPORT_DIRECTORY = Path().resolve()

def run_llm_tasks(
    *,
    text: str,
    doc_id: str | None,
    sequence: int,
    llm_kwargs: dict,
) -> dict:
    from .analysis.llm import llm
    from .helpers import clean_ocr, make_json_safe
    from zotero_rdf_server.utils import load_dict_like
    import json
    _doc_id = safe_doc_id(doc_id)
    def _resolve_out(p: Optional[str], doc_dir:str|None = _doc_id) -> Optional[Path]:

        if not p:
            return None
        pp = Path(p)
        if pp.is_absolute():
            logger.error(f"Absolute paths are not allowed: {pp}")
            return (EXPORT_DIRECTORY / doc_dir).resolve()
        result_path = (EXPORT_DIRECTORY / pp / doc_dir).resolve() if doc_dir else (EXPORT_DIRECTORY / pp ).resolve()
        logger.debug(f"Export path set: {result_path}")
        return result_path

    llm_dict = {}
    llm_tasks = llm_kwargs.get("tasks") or []

    for llm_task in llm_tasks:        
        llm_meta = dict(llm_task or {})
        llm_task_kwargs = dict(llm_task or {"task": "task"})
        
        llm_datatype = str(llm_task_kwargs.pop("datatype", "json")).lower().strip()
        llm_task_kwargs["config_path"] = (
            llm_task_kwargs.get("config_path") or llm_kwargs.get("config_path")
        )
        llm_mapping_key = llm_task_kwargs.pop("mapping_key", "llm")
        llm_mapping_keys = llm_task_kwargs.pop('mapping_keys', None) or [llm_mapping_key]
        llm_file_kwargs = llm_task_kwargs.pop("file_kwargs", {}) or {}
        llm_out = llm_file_kwargs.get("llm_out")
        save_llm = str(llm_file_kwargs.get("save_llm", "skip")).lower().strip()

        llm_file = None
        llm_response = None
        cache_exists = False
        used_cache = False

        if llm_out:
            llm_file = _resolve_out(llm_out, _doc_id) / f"{sequence:04d}.json"
            cache_exists = llm_file.exists()

        should_read_cache = (
            llm_file is not None
            and save_llm in {"skip", "active"}
            and cache_exists
        )

        if should_read_cache:
            try:
                llm_response_file = json.loads(llm_file.read_text(encoding="utf-8"))
                llm_response = llm_response_file.get("response")
                used_cache = True
                logger.info(f"Used Cache\nTask {str(llm_task.get('task', 'n/a')).upper()}\nDocument {doc_id}:{sequence}")
            except Exception:
                llm_response = None
                used_cache = False

        if llm_response is None:
            llm_response = llm(clean_ocr(text), llm_task_kwargs)
            logger.info(f"LLM Processing\nTask {str(llm_task.get('task', 'n/a')).upper()}\nDocument {doc_id}:{sequence}\nModel {llm_task_kwargs.get('model', 'n/a')}")

        if llm_datatype == "json":
            llm_result = load_dict_like(
                llm_response,
                label=f"LLM page response for {doc_id}:{sequence}",
                default=[],
                verbose=not used_cache,
            )        
        else:
            llm_result = llm_response or []

        for key in llm_mapping_keys:
            llm_dict[key] = llm_result

        should_write_cache = (
            llm_file is not None
            and not used_cache
            and (
                save_llm == "overwrite"
                or (save_llm == "active" and not cache_exists)
            )
        )

        if should_write_cache:
            llm_result_file = {"config": llm_meta, "response": llm_result or [], 'generated':datetime.now(timezone.utc).isoformat(), 'input':{'text':text, 'doc_id':doc_id,'sequence':sequence}}
            llm_safe = make_json_safe(llm_result_file)
            llm_file.parent.mkdir(parents=True, exist_ok=True)
            llm_file.write_text(
                json.dumps(llm_safe, indent=4, default=str),
                encoding="utf-8",
            )
            logger.info(f"Stored {llm_file}...")

    return llm_dict

def page_docs(
    *,
    targets: Iterable[str],
    input: str,
    doc_id: str | None,
    label: str | None,
    pages: Iterator[Tuple[int, str]],
    meta: dict = {}, 
    vector_kwargs: dict | None = None,
    llm_kwargs: dict | None = None,
    # config_path: str | None = None,
) -> Iterator[Dict[str, Any]]:

    now = datetime.now(timezone.utc).isoformat()
    vector = isinstance(vector_kwargs, dict) and vector_kwargs.get('framework')
    use_llm = isinstance(llm_kwargs, (dict)) and llm_kwargs.get('tasks')

    if vector:
        from .analysis.vector import embed
        from .helpers import clean_ocr
        vector_doc = None   

    for sequence, text in pages:
        label_s = f"{label.rstrip(',.:')}: {sequence}"
        
        if vector:
            vector_doc = embed(clean_ocr(text),**vector_kwargs)

        llm_dict = {}
        if use_llm and text:
            llm_dict = run_llm_tasks(
                text=text,
                doc_id=doc_id,
                llm_kwargs=llm_kwargs,
                sequence=sequence,
            )

        for index_name in targets:            
            source = {
                "source": input,
                "doc_id": doc_id,
                "label": label_s,
                "page": sequence,
                "text": text,
                "ingest_ts": now,
                "meta": meta,
            }
            if vector:
                source["vector"] = vector_doc

            if use_llm and llm_dict and isinstance(llm_dict,dict):
                source.update(llm_dict)

            action = {
                "_op_type": "index",
                "_index": index_name,
                "_source": source,
            }
            if doc_id is not None:
                action["_id"] = f"{doc_id}:{sequence}"
            logger.debug(f"yielded action for page={sequence} index={index_name}")
            yield action

def doc_action(
    *,
    targets: Iterable[str],
    doc_id: str | None,
    doc: Any,
) -> Iterator[Dict[str, Any]]:
    
    if isinstance(doc, list):
        for i, item in enumerate(doc):
            for index_name in targets:
                action = {
                    "_op_type": "index",
                    "_index": index_name,
                    "_source": item,
                }
                if doc_id is not None:
                    action["_id"] = f"{doc_id}:{i}"
                yield action
        return
    if isinstance(doc, dict):
        for index_name in targets:
            action = {
                "_op_type": "index",
                "_index": index_name,
                "_source": doc,
            }
            if doc_id is not None:
                action["_id"] = doc_id
            logger.debug(f"yielded action for index={index_name}")
            yield action

def ingest_streaming_bulk(
    client: OpenSearch,
    *,
    actions: Iterable[dict],
    index: str | None,
    bulk_cfg: dict,
    run_id: str | None = None
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    run_id = run_id or uuid.uuid4().hex

    logging.getLogger("opensearchpy").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    digest = {
        "run_id": run_id,
        "total": 0,
        "ok": 0,
        "failed": 0,
        "status_counts": Counter(),
        "errors": [],
        "doc_ids": []
    }
    # from rdflogger import LogEvent, log_event_via_trig_template
    # event = LogEvent(
    #     run_id=run_id,
    #     level="ERROR",
    #     message="OpenSearch bulk item failed",
    #     ts=now,
    #     doc_id=doc_id,
    #     page=str(page_no),
    #     target=index or "",
    # )
    try:
        bulk_kwargs = dict(
            chunk_size=int(bulk_cfg.get("chunk_size", 250)),
            max_chunk_bytes=int(bulk_cfg.get("max_chunk_bytes", 5 * 1024 * 1024)),
            max_retries=int(bulk_cfg.get("max_retries", 3)),
            initial_backoff=int(bulk_cfg.get("initial_backoff", 1)),
            max_backoff=int(bulk_cfg.get("max_backoff", 32)),
            raise_on_error=False,
            raise_on_exception=False,
        )
        logger.debug(f"bulk ingest with cfg: {bulk_kwargs}")
        # IMPORTANT: only pass a fixed index if you really want a single target
        if index is not None:
            bulk_kwargs["index"] = index
        ids = []
        for ok, item in streaming_bulk(client, actions, **bulk_kwargs):
            digest["total"] += 1
            op_key = next(iter(item.keys()))
            result = item[op_key]
            status = result.get("status")
            err = result.get("error")
            # logger.debug(f"ingest_streaming_bulk result: {result}")
            
            _id = result.get("_id", "")
            ids.append(_id)
            
            digest["bulk_kwargs"] = bulk_kwargs
            # doc_id, page = (_id.split(":", 1) + ["-1"])[:2] # TODO
            # page_no = int(page) if page.isdigit() else -1
            logger.debug(f"OS Ingesting {_id} with status {status}: {err if err else 'no errors'}")
            if status is not None:
                digest["status_counts"][str(status)] += 1
            if ok:
                digest["ok"] += 1
            else:
                digest["failed"] += 1
                logger.error(
                    "OS bulk item failed run=%s id=%s status=%s error=%r",
                    run_id, _id, status, err
                )
                if len(digest["errors"]) < 50:
                    digest["errors"].append({"_id": _id, "status": status, "error": err})

            # TODO logger in RDF
            # log_event_via_trig_template(
            #     store,
            #     template_path="log_events.trig",
            #     event=event
            # )
        digest["doc_ids"] = ids
        return digest # run_id
    except Exception as e:
        logger.exception(f"ingest_streaming_bulk failed: {e}")
        return {'run_id': run_id,'error':str(e)}


PagesFn = Callable[[str], Iterator[Tuple[int, str]]]

def index_stream(
    *,
    config_path: str | None = None,
    input: str | None = None,
    doc_id: str | None = None,
    label: str | None = None,
    url_to_text_pages_fn: PagesFn | None = None,
    targets: str | Iterable[str],
    source_kwargs: dict | None = None,
    meta: dict | None = None,
    doc: Any | None = None,
    vector_kwargs: dict | None = None,
    llm_kwargs: dict | None = None
) -> dict:
    logger.debug(f"OS index_stream started...")
    cfg_path = resolve_config_path(config_path)
    oscfg = get_os_config(cfg_path)
    client = make_client(oscfg)
    try:
        logger.info(f"Provisioning {targets}...")
        provision_from_cfg(client, oscfg)
        logger.info("Provisioning completed!")
    except Exception as e:
        logger.critical(f"Open Search failed: {e}. Open Search running?")

    # Normalize targets
    if isinstance(targets, str):
        targets_list = [targets]
    else:
        targets_list = list(targets)
    if not targets_list:
        raise ValueError("targets must not be empty")

    if doc is not None:
        # Doc mode (agnostic)
        actions = doc_action(
            targets=targets_list,
            doc_id=doc_id,
            doc=doc,
        )
    else:
        # Pages mode
        if input is None:
            raise ValueError("input is required when doc is None")
        if url_to_text_pages_fn is None:
            raise ValueError("url_to_text_pages_fn is required when doc is None")

        pages_iter = url_to_text_pages_fn(input, **(source_kwargs or {}))

        actions = page_docs(
            targets=targets_list,
            input=input,
            doc_id=doc_id,
            label=label,
            pages=pages_iter,
            meta=meta or {},
            vector_kwargs=vector_kwargs,
            llm_kwargs=llm_kwargs,
            # config_path=cfg_path,
        )

    # IMPORTANT: index=None so per-action _index is respected
    try:
        run = ingest_streaming_bulk(
            client,
            actions=actions,
            index=None,
            bulk_cfg=oscfg.get("bulk", {}),
        )
    except Exception as e:
        logger.error(f"Ingest Error: {e}")
        run = {"run_id": None, "error": f"ingest failed: {str(e)}"}

    for t in targets_list:
        try:
            client.indices.refresh(index=t)
        except Exception:
            pass
    
    run_id = run.get("run_id") or run.get("error") or "no id/error"

    logger.info(
        "from OCOS index_stream for run %s completed for %s...",
        run_id,
        doc_id,
    )
    return run