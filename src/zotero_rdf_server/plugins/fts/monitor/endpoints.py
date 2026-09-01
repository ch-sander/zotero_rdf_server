import csv
import html
import io
import json
import os
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from zotero_rdf_server.logging_config import logger

from zotero_rdf_server.config import EXPORT_DIRECTORY as OCR_EXPORT_ROOT

router = APIRouter(tags=["Monitor", "Admin"])


SORTABLE_COLUMNS = frozenset(
    {
        "source_folder",
        "file",
        "doc_id",
        "input",
        "text_count",
        "image_count",
        "total_source",
        "ts_out",
    }
)
COLUMNS = (
    "source_folder",
    "file",
    "doc_id",
    "input",
    "text_count",
    "image_count",
    "total_source",
    "ts_out",
    "error",
)
REQUIRED_OCR_META_KEYS = frozenset(
    {
        "call",
        "text_count",
        "image_count",
        "total_source",
        "ts_out",
    }
)
MAX_LIMIT = 10_000


class NotOCRMetadataError(ValueError):
    pass


class OCRMetaQuery:
    def __init__(
        self,
        folders: list[str] | None = Query(
            default=None,
            description=(
                "Relative folders below EXPORT_DIRECTORY. "
                "If omitted, all folders containing JSON files are used."
            ),
        ),
        ts_out_after: str | None = Query(
            default=None,
            description=(
                "Include records with ts_out at or after this "
                "ISO-8601 timestamp."
            ),
        ),
        ts_out_before: str | None = Query(
            default=None,
            description=(
                "Include records with ts_out at or before this "
                "ISO-8601 timestamp."
            ),
        ),
        sort_by: str = Query(default="text_count"),
        sort_order: Literal["asc", "desc"] = Query(default="asc"),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=MAX_LIMIT, ge=1, le=MAX_LIMIT),
    ) -> None:
        self.folders = (
            list(dict.fromkeys(folders))
            if folders
            else None
        )
        self.ts_out_after = _parse_query_datetime(
            ts_out_after,
            "ts_out_after",
        )
        self.ts_out_before = _parse_query_datetime(
            ts_out_before,
            "ts_out_before",
        )
        self.sort_by = _parse_sort_column(sort_by)
        self.sort_order = sort_order
        self.offset = offset
        self.limit = limit

        if (
            self.ts_out_after is not None
            and self.ts_out_before is not None
            and self.ts_out_after > self.ts_out_before
        ):
            raise HTTPException(
                422,
                "ts_out_after must not be greater than ts_out_before",
            )


@router.get(
    "/OCR-meta",
    summary="Analyze OCR metadata",
    description=(
        "Read OCR metadata JSON files, optionally filter them by ts_out, "
        "and render them as an interactive HTML table. Date limits are "
        "inclusive. Timestamps without a UTC offset are interpreted as UTC."
    ),
    response_class=HTMLResponse,
)
async def analyze_OCR_meta_html(
    query: OCRMetaQuery = Depends(),
) -> HTMLResponse:
    result, page = await _run_analysis(query)
    return HTMLResponse(
        content=_render_html(
            result=result,
            records=page,
            query=query,
        )
    )


@router.get(
    "/OCR-meta.csv",
    summary="Export OCR metadata as CSV",
    description=(
        "Read and filter OCR metadata JSON files and return the current "
        "result page as UTF-8 CSV."
    ),
    response_class=Response,
)
async def analyze_OCR_meta_csv(
    query: OCRMetaQuery = Depends(),
) -> Response:
    _, page = await _run_analysis(query)
    return Response(
        content=_render_csv(page),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                'attachment; filename="OCR_meta_table.csv"'
            )
        },
    )


async def _run_analysis(
    query: OCRMetaQuery,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    call = partial(
        _analyze_export,
        root=Path(OCR_EXPORT_ROOT),
        folders=query.folders,
        ts_out_after=query.ts_out_after,
        ts_out_before=query.ts_out_before,
        sort_by=query.sort_by,
        descending=query.sort_order == "desc",
    )

    try:
        result = await run_in_threadpool(call)
    except OSError as exc:
        logger.exception("OCR metadata directory could not be read")
        raise HTTPException(
            503,
            "OCR metadata is not available",
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("OCR metadata analysis failed")
        raise HTTPException(
            500,
            "OCR metadata analysis failed",
        ) from exc

    records = result["records"]
    page = records[query.offset : query.offset + query.limit]
    return result, page


def _render_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {
            column: _display_value(record.get(column))
            for column in COLUMNS
        }
        for record in records
    )
    return output.getvalue()


def _render_html(
    *,
    result: dict[str, Any],
    records: list[dict[str, Any]],
    query: OCRMetaQuery,
) -> str:
    headers = "".join(f"<th>{html.escape(column)}</th>" for column in COLUMNS)
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(_display_value(record.get(column)))}</td>"
            for column in COLUMNS
        )
        + "</tr>"
        for record in records
    )
    order_column = COLUMNS.index(query.sort_by)
    csv_query = _query_string(query)
    csv_url = f"OCR-meta.csv?{csv_query}"
    selected_folders = set(result["selected_folders"])
    folder_options = "".join(
        _folder_checkbox(
            folder,
            checked=folder in selected_folders,
        )
        for folder in result["available_folders"]
    )
    if not folder_options:
        folder_options = "<em>No folders containing JSON files found.</em>"
    after_value = _form_timestamp(query.ts_out_after)
    before_value = _form_timestamp(query.ts_out_before)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>OCR Meta</title>
    <link rel="stylesheet"
          href="https://cdn.datatables.net/1.13.8/css/jquery.dataTables.min.css">
    <script src="https://code.jquery.com/jquery-3.7.0.js"></script>
    <script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>
    <style>
        body {{ font-family: sans-serif; margin: 2rem; }}
        form {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }}
        fieldset {{ border: 1px solid #ccc; }}
        label {{ display: inline-flex; align-items: center; gap: .35rem; }}
        .summary {{ margin-bottom: 1rem; }}
        .summary span {{ margin-right: 1.25rem; }}
        table.dataTable td {{ vertical-align: top; }}
    </style>
</head>
<body>
    <h1>OCR Meta</h1>
    <form method="get">
        <fieldset>
            <legend>Folders</legend>
            {folder_options}
        </fieldset>
        <label>
            ts_out after (UTC)
            <input type="datetime-local" name="ts_out_after"
                   value="{after_value}" step="1">
        </label>
        <label>
            ts_out before (UTC)
            <input type="datetime-local" name="ts_out_before"
                   value="{before_value}" step="1">
        </label>
        <input type="hidden" name="sort_by" value="{query.sort_by}">
        <input type="hidden" name="sort_order" value="{query.sort_order}">
        <input type="hidden" name="limit" value="{query.limit}">
        <button type="submit">Apply filters</button>
    </form>
    <div class="summary">
        <span>Scanned: {result["scanned"]}</span>
        <span>Matched: {len(result["records"])}</span>
        <span>Shown: {len(records)}</span>
        <span>Errors: {result["errors"]}</span>
        <a href="{html.escape(csv_url, quote=True)}">Download CSV</a>
    </div>
    <table id="meta" class="display">
        <thead><tr>{headers}</tr></thead>
        <tbody>{rows}</tbody>
    </table>
    <script>
    $(document).ready(function () {{
        $('#meta').DataTable({{
            pageLength: 50,
            order: [[{order_column}, '{query.sort_order}']],
            deferRender: true
        }});
    }});
    </script>
</body>
</html>
"""


def _query_string(query: OCRMetaQuery) -> str:
    values: list[tuple[str, str | int]] = [
        *(("folders", folder) for folder in (query.folders or [])),
        ("sort_by", query.sort_by),
        ("sort_order", query.sort_order),
        ("offset", query.offset),
        ("limit", query.limit),
    ]

    if query.ts_out_after is not None:
        values.append(("ts_out_after", query.ts_out_after.isoformat()))
    if query.ts_out_before is not None:
        values.append(("ts_out_before", query.ts_out_before.isoformat()))

    return urlencode(values)


def _folder_checkbox(folder: str, *, checked: bool) -> str:
    escaped_folder = html.escape(folder, quote=True)
    checked_attribute = " checked" if checked else ""
    return (
        "<label>"
        f'<input type="checkbox" name="folders" value="{escaped_folder}"'
        f"{checked_attribute}>"
        f"{escaped_folder}"
        "</label>"
    )


def _form_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _analyze_export(
    *,
    root: Path,
    folders: list[str] | None,
    ts_out_after: datetime | None,
    ts_out_before: datetime | None,
    sort_by: str,
    descending: bool,
) -> dict[str, Any]:
    available_folders = _discover_json_folders(root)
    selected_folders = _select_folders(
        folders,
        available_folders=available_folders,
    )
    result = _collect_records(
        root=root,
        folders=selected_folders,
        ts_out_after=ts_out_after,
        ts_out_before=ts_out_before,
        sort_by=sort_by,
        descending=descending,
    )
    result["available_folders"] = available_folders
    result["selected_folders"] = selected_folders
    return result


def _discover_json_folders(root: Path) -> list[str]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise OSError(f"Export directory does not exist: {root}")

    folders: list[str] = []

    def raise_walk_error(exc: OSError) -> None:
        raise exc

    for current, directory_names, file_names in os.walk(
        root,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        current_path = Path(current)
        directory_names[:] = [
            name
            for name in directory_names
            if not (current_path / name).is_symlink()
        ]

        json_paths = (
            current_path / name
            for name in file_names
            if name.endswith(".json")
            and not (current_path / name).is_symlink()
        )
        if not any(_is_ocr_metadata_file(path) for path in json_paths):
            continue

        relative = current_path.relative_to(root).as_posix()
        folders.append(relative or ".")

    return sorted(folders, key=str.casefold)


def _select_folders(
    requested: list[str] | None,
    *,
    available_folders: list[str],
) -> list[str]:
    if not requested:
        return available_folders

    available = set(available_folders)
    invalid = sorted(set(requested) - available)
    if invalid:
        raise ValueError(
            "Folders do not exist below EXPORT_DIRECTORY or contain no JSON "
            f"files: {', '.join(invalid)}"
        )

    return list(dict.fromkeys(requested))


def _collect_records(
    *,
    root: Path,
    folders: list[str],
    ts_out_after: datetime | None,
    ts_out_before: datetime | None,
    sort_by: str,
    descending: bool,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    scanned = 0
    errors = 0
    ts_out_after = _as_utc(ts_out_after)
    ts_out_before = _as_utc(ts_out_before)

    for folder_name in folders:
        folder = _safe_export_folder(root, folder_name)

        if not folder.is_dir():
            raise OSError(f"Metadata directory does not exist: {folder}")

        for path in sorted(folder.glob("*.json")):
            if path.is_symlink():
                continue

            try:
                record, parsed_ts = _read_record(path, folder_name)
            except NotOCRMetadataError:
                continue
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
                scanned += 1
                errors += 1
                logger.warning(
                    "Could not read OCR metadata file %s: %s",
                    path,
                    exc,
                )
                record = _error_record(path, folder_name, exc)
                parsed_ts = None
            else:
                scanned += 1

            if not _timestamp_matches(
                parsed_ts,
                ts_out_after=ts_out_after,
                ts_out_before=ts_out_before,
            ):
                continue

            records.append(record)

    records_with_value = [
        record for record in records if _sort_value(record, sort_by) is not None
    ]
    records_without_value = [
        record for record in records if _sort_value(record, sort_by) is None
    ]
    records_with_value.sort(
        key=lambda record: _sort_value(record, sort_by),
        reverse=descending,
    )
    records = records_with_value + records_without_value

    return {
        "records": records,
        "scanned": scanned,
        "errors": errors,
    }


def _safe_export_folder(root: Path, folder_name: str) -> Path:
    resolved_root = root.resolve(strict=True)
    folder = (resolved_root / folder_name).resolve(strict=True)

    try:
        folder.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"Folder is outside EXPORT_DIRECTORY: {folder_name}"
        ) from exc

    if not folder.is_dir():
        raise OSError(f"Metadata directory does not exist: {folder}")

    return folder


def _read_record(
    path: Path,
    folder_name: str,
) -> tuple[dict[str, Any], datetime | None]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not _is_ocr_metadata(data):
        raise NotOCRMetadataError("JSON is not an OCR metadata record")

    call = data.get("call") or {}
    if not isinstance(call, dict):
        raise TypeError("call must be an object")

    raw_ts = data.get("ts_out")
    parsed_ts = _parse_file_timestamp(raw_ts)

    return (
        {
            "source_folder": folder_name,
            "file": path.name,
            "doc_id": call.get("doc_id"),
            "input": call.get("input"),
            "text_count": _as_int(data.get("text_count")),
            "image_count": _as_int(data.get("image_count")),
            "total_source": _as_int(data.get("total_source")),
            "ts_out": raw_ts,
            "error": None,
        },
        parsed_ts,
    )


def _is_ocr_metadata_file(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False

    return _is_ocr_metadata(data)


def _is_ocr_metadata(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if not REQUIRED_OCR_META_KEYS.issubset(data):
        return False

    call = data.get("call")
    return isinstance(call, dict) and (
        "doc_id" in call
        or "input" in call
    )


def _error_record(
    path: Path,
    folder_name: str,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "source_folder": folder_name,
        "file": path.name,
        "doc_id": None,
        "input": None,
        "text_count": 0,
        "image_count": 0,
        "total_source": 0,
        "ts_out": None,
        "error": str(exc),
    }


def _parse_sort_column(value: str) -> str:
    if value not in SORTABLE_COLUMNS:
        allowed = ", ".join(sorted(SORTABLE_COLUMNS))
        raise HTTPException(422, f"sort_by must be one of: {allowed}")
    return value


def _parse_file_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None

    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    return _as_utc(parsed)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_matches(
    value: datetime | None,
    *,
    ts_out_after: datetime | None,
    ts_out_before: datetime | None,
) -> bool:
    if ts_out_after is None and ts_out_before is None:
        return True
    if value is None:
        return False
    if ts_out_after is not None and value < ts_out_after:
        return False
    if ts_out_before is not None and value > ts_out_before:
        return False
    return True


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _sort_value(record: dict[str, Any], column: str) -> Any:
    value = record.get(column)

    if column == "ts_out":
        value = _parse_file_timestamp(value)

    if isinstance(value, str):
        value = value.casefold()

    if value is not None and column in {"doc_id", "input"}:
        value = str(value).casefold()

    return value


def _parse_query_datetime(
    value: Any,
    name: str,
) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return _as_utc(value)

    if not isinstance(value, str):
        raise HTTPException(
            422,
            f"{name} must be an ISO-8601 timestamp",
        )

    value = value.strip()

    if not value:
        return None

    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            422,
            f"{name} must be an ISO-8601 timestamp",
        ) from exc

    return _as_utc(parsed)
