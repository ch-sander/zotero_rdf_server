from typing import Any, Dict, List, Optional, Union
from pathlib import Path
import json, csv
from uuid import uuid4
from fastapi import Body, HTTPException, Query, APIRouter

router = APIRouter()

JsonObj = Dict[str, Any]
JsonBody = Union[JsonObj, List[JsonObj]]

@router.post("/pipeline_async")
def ingest_route_async(
    input: Optional[JsonBody] = Body(default=None, examples=[None]),
    targets: str | list = Query(default=None, description="Index or alias"),
    ocr: bool = Query(default=True, description="If true, run OCR pages ingest via pages_fn"),
    vector: bool = Query(default=True, description="If true, vectorizes text with sentence transformer (1024 dimensions)"),
    transformer: bool = Query(None, description="If true, run transformer pipeline (doi:10.3390/electronics14153083)"),
    ingest: bool = Query(default=True, description="If true, ingest into Open Search"),
    query: Optional[str] = Query(default=None, description="SPARQL SELECT query or path to file with query code (used when body is null)"),
    graph: str | None = Query(default=None, description="Named graph IRI containing the attachments or documents (optional)"),
    config_path: Optional[str] = Query(None, description="Path to YAML config. If omitted: ENV FTS_CONFIG, otherwise ./config.yml"),
    store_path: Optional[str] = Query(default=None, description="Oxigraph store path (defaults to main store)"),
    open_search_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for Open Search Config", examples=[None]),
    ocr_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for OCR Config", examples=[None]),
    model_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for Kraken Config", examples=[None]),
    file_kwargs: Optional[dict] = Body(default=None, description="Keyword Arguments for File Output", examples=[{'img_out':'kraken/images','txt_out':'kraken/texts','save_text':'active','save_image':'active'}]),
):
    from .jobs_db import init_db, create_job
    from .tasks import drive_job

    try:
        from zotero_rdf_server.config import EXPORT_DIRECTORY
        export_root = Path(EXPORT_DIRECTORY) / "fts"  # -> app/exports/fts
    except Exception:
        export_root = Path("app/exports/fts")

    export_root.mkdir(parents=True, exist_ok=True)
    db_path = export_root / "jobs.sqlite"
    init_db(db_path)

    def save_items_snapshot(job_dir: Path, items: List[Dict[str, Any]], var_names: Optional[List[str]] = None) -> str:
        job_dir.mkdir(parents=True, exist_ok=True)

        items_json = job_dir / "items.json"
        with open(items_json, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        items_csv = job_dir / "items.csv"
        if var_names is None:
            var_names = sorted({k for row in items for k in row.keys()})
        with open(items_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=var_names)
            writer.writeheader()
            writer.writerows(items)

        return str(items_json)

    submitted: List[str] = []

    if input is None:
        try:
            if store_path:
                logger.warning(f"Reading from store in {store_path}")
                from pyoxigraph import Store, NamedNode
                store = Store.read_only(store_path)
                load_text_like = None
            else:
                from zotero_rdf_server.store import store, NamedNode
                from zotero_rdf_server.utils import load_text_like
                logger.warning("Reading from main store")
        except Exception as e:
            logger.error(f"Reading from store failed: {e}")
            raise HTTPException(status_code=400, detail="Reading from store failed")

        if graph or (graph is None and query is None):
            from zotero_rdf_server.store import get_graph
            checked_graph, all_graphs = get_graph(graph)
            if graph and not checked_graph:
                raise HTTPException(status_code=400, detail=f"Invalid graph IRI. Use one of these or None: {all_graphs}")

            from zotero_rdf_server.models import ZoteroLibrary
            from zotero_rdf_server.config import ZOTERO_LIBRARIES_CONFIGS

            for lib_cfg in ZOTERO_LIBRARIES_CONFIGS:
                lib = ZoteroLibrary(lib_cfg)
                if graph and graph != lib.base_url:
                    continue
                if not graph or graph == lib.base_url:
                    cfg = lib.plugin.get("fts") or []
                    cfg = [cfg] if isinstance(cfg, dict) else cfg

                    for ncfg in cfg:
                        os_cfg = open_search_kwargs if open_search_kwargs is not None else (ncfg.get("open-search") or {})
                        kraken_cfg = (ncfg.get("kraken") or {})
                        targets_x = targets or os_cfg.get("targets")
                        if not targets_x:
                            raise HTTPException(status_code=400, detail="Missing target indices/index")

                        config_path_x = config_path or os_cfg.get("config_path")
                        query_x = query or ncfg.get("query")
                        if not query_x:
                            raise HTTPException(status_code=400, detail="With no input, you must provide 'query' parameter")

                        ocr_x = ocr if ocr is not None else ncfg.get("ocr", True)
                        transformer_x = transformer if transformer is not None else ncfg.get("transformer", False)
                        ingest_x = ingest if ingest is not None else ncfg.get("ingest", True)
                        vector_x = vector if vector is not None else ncfg.get("vector", True)

                        iter_pages_kwargs = ocr_kwargs if ocr_kwargs is not None else dict(kraken_cfg.get("ocr_kwargs") or {})
                        page_to_text_kwargs = model_kwargs if model_kwargs is not None else dict(kraken_cfg.get("model_kwargs") or {})
                        text_image_file_kwargs = file_kwargs if file_kwargs is not None else dict(kraken_cfg.get("file_kwargs") or {})

                        sparql_query = load_text_like(query_x, label="Ingest Pipeline SPARQL Query") if load_text_like else query_x
                        bindings = store.query(
                            sparql_query,
                            use_default_graph_as_union=False,
                            default_graph=[NamedNode(lib.base_url), NamedNode(lib.knowledge_base_graph)],
                        )
                        var_names = [v.value for v in bindings.variables]
                        items = [
                            {name: (sol[name].value if sol[name] is not None else None) for name in var_names}
                            for sol in bindings
                        ]

                        # Freeze snapshot + create job
                        job_id = str(uuid4())
                        job_dir = export_root / "jobs" / job_id
                        items_json_path = save_items_snapshot(job_dir, items, var_names=var_names)

                        params = {
                            "targets": targets_x,
                            "ocr": bool(ocr_x),
                            "transformer": bool(transformer_x),
                            "vector": bool(vector_x),
                            "ingest": bool(ingest_x),
                            "iter_pages_kwargs": iter_pages_kwargs,
                            "page_to_text_kwargs": page_to_text_kwargs,
                            "text_image_file_kwargs": text_image_file_kwargs,
                            "config_path": config_path_x,
                        }

                        create_job(db_path=db_path, job_id=job_id, items_json_path=items_json_path, params=params)
                        drive_job.kiq(job_id)
                        submitted.append(job_id)

            return {"status": "accepted", "job_ids": submitted, "jobs": len(submitted)}

        elif graph is None and query:
            if not targets:
                raise HTTPException(status_code=400, detail="Missing target indices/index")

            sparql_query = load_text_like(query, label="Ingest Pipeline SPARQL Query") if load_text_like else query
            bindings = store.query(sparql_query, use_default_graph_as_union=True)
            var_names = [v.value for v in bindings.variables]
            items = [
                {name: (sol[name].value if sol[name] is not None else None) for name in var_names}
                for sol in bindings
            ]

            job_id = str(uuid4())
            job_dir = export_root / "jobs" / job_id
            items_json_path = save_items_snapshot(job_dir, items, var_names=var_names)

            params = {
                "targets": targets,
                "ocr": bool(ocr),
                "transformer": bool(transformer),
                "vector": bool(vector),
                "ingest": bool(ingest),
                "iter_pages_kwargs": ocr_kwargs or {},
                "page_to_text_kwargs": model_kwargs or {},
                "text_image_file_kwargs": file_kwargs or {},
                "config_path": config_path,
            }

            create_job(db_path=db_path, job_id=job_id, items_json_path=items_json_path, params=params)
            drive_job.kiq(job_id)
            return {"status": "accepted", "job_id": job_id, "items": len(items), "targets": targets}

        raise HTTPException(status_code=400, detail="Missing parameters for query!")

    # ---------- input provided ----------
    if not targets:
        raise HTTPException(status_code=400, detail="Missing target indices/index")

    if isinstance(input, str):
        from zotero_rdf_server.utils import load_dict_like
        input = load_dict_like(input, label="Ingest Pipeline Input")

    if isinstance(input, dict):
        items = [input]
    elif isinstance(input, list) and all(isinstance(x, dict) for x in input):
        items = input
    else:
        raise HTTPException(status_code=400, detail="Body must be a JSON object, a list of JSON objects, or null")

    job_id = str(uuid4())
    job_dir = export_root / "jobs" / job_id
    items_json_path = save_items_snapshot(job_dir, items)

    params = {
        "targets": targets,
        "ocr": bool(ocr),
        "transformer": bool(transformer),
        "vector": bool(vector),
        "ingest": bool(ingest),
        "iter_pages_kwargs": ocr_kwargs or {},
        "page_to_text_kwargs": model_kwargs or {},
        "text_image_file_kwargs": file_kwargs or {},
        "config_path": config_path,
    }

    create_job(db_path=db_path, job_id=job_id, items_json_path=items_json_path, params=params)
    drive_job.kiq(job_id)
    return {"status": "accepted", "job_id": job_id, "items": len(items), "targets": targets}
