import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from taskiq import TaskiqDepends, task

from .helpers import plugin_logger
from .jobs_db import (
    get_job,
    set_state,
    update_progress,
    is_cancel_requested,
    claim_job,
    renew_lease,
    release_lease,
)

logger = plugin_logger()

def _db_path() -> Path:
    from zotero_rdf_server.config import EXPORT_DIRECTORY
    return Path(EXPORT_DIRECTORY) / "fts" / "jobs.sqlite"

def _worker_id() -> str:
    return os.environ.get("HOSTNAME") or f"worker-{os.getpid()}"

def _load_items(items_json_path: str) -> list[dict]:
    with open(items_json_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _progress_fn(db: Path, job_id: str, worker_id: str):
    # return False => cancel
    def progress(**info) -> bool:
        renew_lease(db, job_id, worker_id=worker_id, lease_seconds=180)

        phase = info.get("phase")
        doc_id = info.get("doc_id")
        item_index = info.get("item_index")
        total = info.get("total")
        page = info.get("page")
        pages_emitted = info.get("pages_emitted")

        update_progress(
            db,
            job_id,
            phase=phase,
            item_index=item_index,
            total_items=total,
            doc_id=doc_id,
            page=page,
            pages_emitted=pages_emitted,
        )
        return not is_cancel_requested(db, job_id)
    return progress

@task
def drive_job(job_id: str) -> None:
    db = _db_path()
    wid = _worker_id()

    if not claim_job(db, job_id, worker_id=wid, lease_seconds=180):
        logger.info("Job %s not claimed (already running elsewhere?)", job_id)
        return

    try:
        job = get_job(db, job_id)

        if job.cancel_requested:
            set_state(db, job_id, "CANCELED")
            return

        set_state(db, job_id, "RUNNING")
        update_progress(db, job_id, phase="starting")

        from .jobs_db import get_runtime

        runtime = get_runtime(db, job_id)
        last_done = runtime.get("item_index") or 0

        items = _load_items(job.items_json_path)

        if last_done:
            logger.info(
                "Resuming job %s from item %s (total %s)",
                job_id,
                last_done + 1,
                len(items),
            )
            items = items[last_done:]  # skip already done
        else:
            logger.info("Starting job %s from beginning", job_id)

        params = job.params

        from .pipeline import ingest_pipeline

        progress = _progress_fn(db, job_id, wid)

        result = ingest_pipeline(
            items=items,
            targets=params.get("targets"),
            ocr=bool(params.get("ocr")),
            transformer=bool(params.get("transformer")),
            vector=bool(params.get("vector", True)),
            ingest=bool(params.get("ingest", True)),
            iter_pages_kwargs=params.get("iter_pages_kwargs") or {},
            page_to_text_kwargs=params.get("page_to_text_kwargs") or {},
            text_image_file_kwargs=params.get("text_image_file_kwargs") or {},
            config_path=params.get("config_path"),
            progress=progress,
            job_id=job_id,
        )

        update_progress(db, job_id, phase="done")
        set_state(db, job_id, "DONE")

        try:
            job_dir = Path(job.items_json_path).parent
            with open(job_dir / "result.json", "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to write result.json for job %s", job_id)

    except RuntimeError as e:
        # cancel
        if "canceled" in str(e).lower():
            set_state(db, job_id, "CANCELED")
        else:
            set_state(db, job_id, "FAILED", error=str(e))
        logger.exception("Job %s stopped: %s", job_id, e)
    except Exception as e:
        set_state(db, job_id, "FAILED", error=str(e))
        logger.exception("Job %s failed", job_id)
    finally:
        release_lease(db, job_id, worker_id=wid)
