import sqlite3
from functools import partial
from typing import Any

import requests
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from zotero_rdf_server.utils import ensure_import
from zotero_rdf_server.logging_config import logger


router = APIRouter(tags=["Harvester"])


MAX_TERMS = 20
MAX_TERM_LENGTH = 200

DEFAULT_YEAR_FROM = 1545
DEFAULT_YEAR_TO = 1790


@router.post(
    "/harvest",
    summary="Search bibliographic sources",
    description=(
        "Search STCV, BASE and/or Deutsche Digitale Bibliothek, "
        "normalize the results and return deduplicated HarvestRecord objects."
    ),
    response_class=JSONResponse,
)
async def harvest_records(
    payload: dict[str, Any] = Body(
        ...,
        openapi_examples={
            "basic": {
                "summary": "Search all sources",
                "value": {
                    "must": ["Aristoteles"],
                    "must_not": [],
                    "year_from": 1545,
                    "year_to": 1790,
                    "sources": {
                        "stcv": True,
                        "base": True,
                        "ddb": True,
                    },
                },
            },
            "stcv_only": {
                "summary": "Search STCV only",
                "value": {
                    "must": ["Aristoteles"],
                    "must_not": ["commentarius"],
                    "year_from": 1600,
                    "year_to": 1700,
                    "sources": {
                        "stcv": True,
                        "base": False,
                        "ddb": False,
                    },
                },
            },
        },
    ),
) -> JSONResponse:
    must = _parse_terms(payload.get("must"), "must", required=True)

    must_not = _parse_terms(
        payload.get("must_not", []),
        "must_not",
    )

    year_from = _parse_year(
        payload.get("year_from", DEFAULT_YEAR_FROM),
        "year_from",
    )

    year_to = _parse_year(
        payload.get("year_to", DEFAULT_YEAR_TO),
        "year_to",
    )

    if year_from > year_to:
        raise HTTPException(
            422,
            "year_from must not be greater than year_to",
        )

    sources = payload.get(
        "sources",
        {
            "stcv": True,
            "base": True,
            "ddb": True,
        },
    )

    if not isinstance(sources, dict):
        raise HTTPException(
            422,
            "sources must be an object",
        )

    include_stcv = _parse_bool(
        sources.get("stcv", True),
        "sources.stcv",
    )
    include_base = _parse_bool(
        sources.get("base", True),
        "sources.base",
    )
    include_ddb = _parse_bool(
        sources.get("ddb", True),
        "sources.ddb",
    )

    if not any(
        (
            include_stcv,
            include_base,
            include_ddb,
        )
    ):
        raise HTTPException(
            422,
            "At least one source must be enabled",
        )

    try:
        ensure_import("scigma_harvesting")
        from zotero_rdf_server.utils import load_dict_like
        from scigma_harvesting import load_config, harvest, activate_logging
        from dotenv import load_dotenv

        # TODO
        activate_logging()
        load_dotenv("/app/plugins/harvester/.env")
        config = load_config("/app/plugins/harvester/config.yml")

        call = partial(
            harvest,
            config=config,
            must=must,
            must_not=must_not,
            year_from=year_from,
            year_to=year_to,
            include_stcv=include_stcv,
            include_base=include_base,
            include_ddb=include_ddb,
        )

        records = await run_in_threadpool(call)

    except sqlite3.OperationalError as exc:
        logger.exception("STCV database error")

        raise HTTPException(
            503,
            "The STCV database is not available",
        ) from exc

    except requests.RequestException as exc:
        logger.exception("Harvesting upstream request failed")

        raise HTTPException(
            502,
            "A harvesting upstream service could not be reached",
        ) from exc

    except ValueError as exc:
        logger.exception("Invalid harvesting request")

        raise HTTPException(
            422,
            str(exc),
        ) from exc

    except Exception as exc:
        logger.exception("Harvesting failed")

        raise HTTPException(
            500,
            "Harvesting failed",
        ) from exc

    return JSONResponse(
        content={
            "count": len(records),
            "records": records,
        },
    )


def _parse_terms(
    value: Any,
    name: str,
    *,
    required: bool = False,
) -> list[str]:
    if value is None:
        value = []

    if not isinstance(value, list):
        raise HTTPException(
            422,
            f"{name} must be an array of strings",
        )

    if required and not value:
        raise HTTPException(
            422,
            f"{name} must contain at least one search term",
        )

    if len(value) > MAX_TERMS:
        raise HTTPException(
            422,
            f"{name} may contain at most {MAX_TERMS} terms",
        )

    result: list[str] = []

    for item in value:
        if not isinstance(item, str):
            raise HTTPException(
                422,
                f"{name} must contain strings only",
            )

        item = item.strip()

        if not item:
            raise HTTPException(
                422,
                f"{name} must not contain empty strings",
            )

        if len(item) > MAX_TERM_LENGTH:
            raise HTTPException(
                422,
                f"{name} terms may contain at most "
                f"{MAX_TERM_LENGTH} characters",
            )

        result.append(item)

    return result


def _parse_year(
    value: Any,
    name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            422,
            f"{name} must be an integer",
        )

    if not 1 <= value <= 9999:
        raise HTTPException(
            422,
            f"{name} must be between 1 and 9999",
        )

    return value


def _parse_bool(
    value: Any,
    name: str,
) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(
            422,
            f"{name} must be a boolean",
        )

    return value