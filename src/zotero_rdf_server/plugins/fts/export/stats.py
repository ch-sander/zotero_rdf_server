"""Per-item OpenSearch term statistics with JSON-LD output.

The public pipeline hook is :func:`attach_item_stats`. Configuration can live
at ``stats`` or ``open-search.stats`` in the main configuration file. Values in
``overrides`` take precedence. Significant term keys can optionally be written
back to every indexed page belonging to the analyzed item.
"""

from datetime import datetime, timezone
from collections import Counter
from functools import lru_cache
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, Mapping

import json
import re

from ..db import safe_doc_id
from ..helpers import plugin_logger
from zotero_rdf_server.utils import load_dict_like


logger = plugin_logger()
_MISSING = object()
_DEFAULT_JSONLD_CONTEXT = {
    "@vocab": "urn:opensearch:stats:",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "item": {"@type": "@id"},
    "generatedAt": {"@type": "xsd:dateTime"},
}


@lru_cache(maxsize=16)
def _load_jsonld_context_cached(source: str | None) -> Any:
    """Load a path-based JSON-LD context once per process and source."""
    loaded = load_dict_like(
        source,
        _DEFAULT_JSONLD_CONTEXT,
        label="Load Context for Stats",
    )
    if not isinstance(loaded, Mapping):
        raise TypeError("stats JSON-LD context must resolve to a mapping")
    return loaded.get("@context", loaded)


def _merge_dicts(
    base: Mapping[str, Any] | None,
    override: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Recursively merge mappings without mutating either input."""
    result = dict(base or {})
    for key, value in dict(override or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return dict(value)


def resolve_stats_config(
    *,
    config_path: str | Path | None = None,
    oscfg: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Resolve and merge root, OpenSearch and call-site stats configuration."""
    full_cfg: Dict[str, Any] = {}
    if config_path is not None:
        from zotero_rdf_server.utils import load_dict_like

        loaded = load_dict_like(
            config_path,
            label="Pipeline Config",
            verbose=False,
        ) or {}
        full_cfg = _require_mapping(loaded, "pipeline configuration")

    root_stats = _require_mapping(
        full_cfg.get("stats"),
        "root 'stats' configuration",
    )
    root_opensearch = _require_mapping(
        full_cfg.get("open-search"),
        "'open-search' configuration",
    )
    nested_stats = _require_mapping(
        root_opensearch.get("stats"),
        "'open-search.stats' configuration",
    )
    supplied_oscfg = _require_mapping(oscfg, "oscfg")
    os_stats = _require_mapping(
        supplied_oscfg.get("stats"),
        "oscfg 'stats' configuration",
    )
    call_overrides = _require_mapping(overrides, "stats overrides")

    resolved = _merge_dicts(root_stats, nested_stats)
    resolved = _merge_dicts(resolved, os_stats)
    return _merge_dicts(resolved, call_overrides)


def _render_templates(value: Any, variables: Mapping[str, Any]) -> Any:
    """Render known ${name} and {{name}} placeholders in nested values."""
    if isinstance(value, Mapping):
        rendered: Dict[Any, Any] = {}
        for key, item in value.items():
            rendered_key = _render_templates(key, variables)
            try:
                hash(rendered_key)
            except TypeError as exc:
                raise TypeError("a rendered configuration key is not hashable") from exc
            rendered[rendered_key] = _render_templates(item, variables)
        return rendered
    if isinstance(value, list):
        return [_render_templates(item, variables) for item in value]
    if isinstance(value, tuple):
        return tuple(_render_templates(item, variables) for item in value)
    if not isinstance(value, str):
        return value

    rendered_value: Any = value
    for name, replacement in variables.items():
        tokens = (f"${{{name}}}", "{{" + name + "}}")
        if rendered_value in tokens:
            return replacement
        replacement_text = "" if replacement is None else str(replacement)
        for token in tokens:
            rendered_value = rendered_value.replace(token, replacement_text)
    return rendered_value


def _value_at_path(
    value: Any,
    path: str,
    default: Any = _MISSING,
) -> Any:
    """Resolve a dotted response path, including numeric list components."""
    current = value
    for part in str(path).split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def _output_directory(output: str | Path) -> Path:
    from zotero_rdf_server.config import EXPORT_DIRECTORY

    output_dir = Path(output).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path(EXPORT_DIRECTORY).expanduser() / output_dir
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _write_json_atomic(path: Path, value: Dict[str, Any], *, indent: int) -> None:
    temporary_name = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(
                value,
                temporary,
                ensure_ascii=False,
                indent=indent,
                default=str,
            )
            temporary.write("\n")
        replace(temporary_name, path)
    finally:
        if temporary_name:
            temporary_path = Path(temporary_name)
            if temporary_path.exists():
                temporary_path.unlink()


def _targets_list(targets: str | Iterable[str]) -> list[str]:
    if isinstance(targets, str):
        result = [targets]
    else:
        result = list(targets)
    if not result:
        raise ValueError("stats require at least one OpenSearch target")
    return result


def _frequent_terms_config(stats_cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize the optional term-vector frequency analysis setting."""
    configured = stats_cfg.get("frequent_terms", False)
    if configured is None:
        return {"enabled": False}
    if isinstance(configured, bool):
        return {"enabled": configured}
    if not isinstance(configured, Mapping):
        raise TypeError("stats 'frequent_terms' must be a boolean or mapping")
    result = dict(configured)
    result.setdefault("enabled", True)
    return result


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _most_frequent_terms(
    *,
    client: Any,
    index: Any,
    item_query: Mapping[str, Any],
    frequent_cfg: Mapping[str, Any],
    variables: Mapping[str, Any],
    top_n: int,
) -> list[Dict[str, Any]]:
    """Sum analyzed ``term_freq`` values across all pages of one item."""
    from opensearchpy.helpers import scan

    field = str(frequent_cfg.get("field", "text")).strip()
    if not field:
        raise ValueError("stats 'frequent_terms.field' must not be empty")

    batch_size = int(frequent_cfg.get("batch_size", 100))
    page_size = int(frequent_cfg.get("page_size", 500))
    configured_top_n = int(
        _render_templates(frequent_cfg.get("top_n", top_n), variables)
    )
    if batch_size < 1 or page_size < 1 or configured_top_n < 1:
        raise ValueError(
            "frequent_terms batch_size, page_size and top_n must be >= 1"
        )

    query = _render_templates(
        frequent_cfg.get("query", item_query),
        variables,
    )
    if not isinstance(query, Mapping) or not query:
        raise ValueError("frequent_terms requires an explicit item query")

    raw_scan_params = _require_mapping(
        frequent_cfg.get("scan_params"),
        "stats 'frequent_terms.scan_params'",
    )
    scan_params: Dict[str, Any] = {
        "size": page_size,
        "scroll": "2m",
        "preserve_order": False,
    }
    scan_params.update(_render_templates(raw_scan_params, variables))

    pages = [
        {"_index": hit.get("_index"), "_id": hit.get("_id")}
        for hit in scan(
            client,
            index=index,
            query={"_source": False, "query": dict(query)},
            **scan_params,
        )
        if hit.get("_index") and hit.get("_id")
    ]
    if not pages:
        logger.warning(
            "No indexed pages matched frequent_terms query for doc_id=%s "
            "in index=%s",
            variables.get("doc_id"),
            index,
        )
        return []

    excluded = frequent_cfg.get("exclude")
    exclude_pattern = re.compile(str(excluded)) if excluded else None
    raw_stopwords = frequent_cfg.get("stopwords", [])
    if isinstance(raw_stopwords, str):
        stopwords = {raw_stopwords.casefold()}
    elif isinstance(raw_stopwords, Iterable):
        stopwords = {str(word).casefold() for word in raw_stopwords}
    else:
        raise TypeError("stats 'frequent_terms.stopwords' must be a list or string")

    min_word_length = int(frequent_cfg.get("min_word_length", 1))
    if min_word_length < 1:
        raise ValueError("frequent_terms min_word_length must be >= 1")

    raw_mtv_params = _require_mapping(
        frequent_cfg.get("params"),
        "stats 'frequent_terms.params'",
    )
    mtv_params = _render_templates(raw_mtv_params, variables)
    frequencies: Counter[str] = Counter()
    document_frequencies: Counter[str] = Counter()

    for batch in _chunks(pages, batch_size):
        response = client.mtermvectors(
            body={
                "docs": [
                    {
                        "_index": page["_index"],
                        "_id": page["_id"],
                        "fields": [field],
                        "offsets": False,
                        "positions": False,
                        "payloads": False,
                        "field_statistics": False,
                        "term_statistics": False,
                    }
                    for page in batch
                ]
            },
            **mtv_params,
        )
        response = _require_mapping(response, "OpenSearch multi term-vector response")
        docs = response.get("docs", [])
        if not isinstance(docs, list):
            raise TypeError("OpenSearch multi term-vector 'docs' must be a list")

        for document in docs:
            if not isinstance(document, Mapping):
                continue
            if document.get("error"):
                raise RuntimeError(
                    "OpenSearch term-vector request failed for "
                    f"{document.get('_id')}: {document['error']!r}"
                )
            term_vectors = document.get("term_vectors", {})
            field_vector = (
                term_vectors.get(field, {})
                if isinstance(term_vectors, Mapping)
                else {}
            )
            terms = (
                field_vector.get("terms", {})
                if isinstance(field_vector, Mapping)
                else {}
            )
            if not isinstance(terms, Mapping):
                continue
            for term, term_data in terms.items():
                token = str(term)
                if (
                    len(token) < min_word_length
                    or token.casefold() in stopwords
                    or (exclude_pattern and exclude_pattern.fullmatch(token))
                    or not isinstance(term_data, Mapping)
                ):
                    continue
                term_freq = int(term_data.get("term_freq", 0))
                if term_freq > 0:
                    frequencies[token] += term_freq
                    document_frequencies[token] += 1

    ordered = sorted(
        frequencies.items(),
        key=lambda item: (-item[1], item[0]),
    )[:configured_top_n]
    logger.info(
        "Calculated %s frequent terms from %s pages for doc_id=%s",
        len(ordered),
        len(pages),
        variables.get("doc_id"),
    )
    return [
        {
            "key": term,
            "term_freq": frequency,
            "doc_count": document_frequencies[term],
        }
        for term, frequency in ordered
    ]


def _persist_terms_config(
    stats_cfg: Mapping[str, Any],
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    """Normalize term persistence entries and shared update options."""
    configured = stats_cfg.get("persist_terms")
    options = _require_mapping(
        stats_cfg.get("persist_terms_options"),
        "stats 'persist_terms_options'",
    )

    if configured is None or configured is False:
        return [], options
    if isinstance(configured, Mapping):
        wrapper = dict(configured)
        if not bool(wrapper.pop("enabled", True)):
            return [], options
        entries = wrapper.pop("entries", None)
        if entries is None:
            entries = [wrapper]
        else:
            options = _merge_dicts(wrapper, options)
    elif isinstance(configured, list):
        entries = configured
    else:
        raise TypeError("stats 'persist_terms' must be a list or mapping")

    normalized: list[Dict[str, Any]] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TypeError(f"stats persist_terms[{position}] must be a mapping")
        item = dict(entry)
        if not bool(item.pop("enabled", True)):
            continue
        field = item.get("field")
        source = item.get("source")
        if not isinstance(field, str) or not field.strip() or "." in field:
            raise ValueError(
                f"stats persist_terms[{position}].field must be a top-level field"
            )
        if not isinstance(source, str) or not source.strip():
            raise ValueError(
                f"stats persist_terms[{position}].source must be a statistics key"
            )
        normalized.append({"field": field.strip(), "source": source.strip()})
    return normalized, options


def _bucket_term_keys(value: Any, *, source: str) -> list[str]:
    """Extract unique keys from a statistics bucket list."""
    if not isinstance(value, list):
        raise TypeError(f"statistics source {source!r} must be a bucket list")
    terms: list[str] = []
    seen: set[str] = set()
    for bucket in value:
        if not isinstance(bucket, Mapping) or bucket.get("key") is None:
            continue
        term = str(bucket["key"])
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _persist_terms(
    *,
    client: Any,
    index: Any,
    statistics: Mapping[str, Any],
    search_body: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    options: Mapping[str, Any],
    variables: Mapping[str, Any],
) -> Dict[str, Any]:
    """Write configured item-level term lists to all matching pages."""
    updates: list[Dict[str, Any]] = []
    fields: set[str] = set()
    for entry in entries:
        field = str(entry["field"])
        source = str(entry["source"])
        if field in fields:
            raise ValueError(f"duplicate persist_terms field: {field!r}")
        if source not in statistics:
            raise KeyError(f"persist_terms source {source!r} is not available")
        fields.add(field)
        updates.append(
            {
                "field": field,
                "source": source,
                "terms": _bucket_term_keys(statistics[source], source=source),
            }
        )

    configured_query = options.get("query", search_body.get("query"))
    query = _render_templates(configured_query, variables)
    if not isinstance(query, Mapping) or not query:
        raise ValueError("term persistence requires an explicit item query")

    update_params: Dict[str, Any] = {
        "conflicts": options.get("conflicts", "proceed"),
        "refresh": bool(options.get("refresh", False)),
    }
    pipeline = options.get("pipeline", "_none")
    if pipeline is not None:
        update_params["pipeline"] = pipeline
    update_params.update(
        _render_templates(
            _require_mapping(options.get("params"), "stats persist_terms params"),
            variables,
        )
    )

    update_response = client.update_by_query(
        index=index,
        body={
            "query": dict(query),
            "script": {
                "lang": "painless",
                "source": (
                    "for (int i = 0; i < params.fields.size(); ++i) { "
                    "ctx._source[params.fields.get(i)] = params.values.get(i); }"
                ),
                "params": {
                    "fields": [update["field"] for update in updates],
                    "values": [update["terms"] for update in updates],
                },
            },
        },
        **update_params,
    )
    update_response = _require_mapping(
        update_response,
        "OpenSearch term persistence response",
    )
    failures = update_response.get("failures") or []
    if failures:
        raise RuntimeError(f"OpenSearch term persistence failed: {failures[0]!r}")

    result = {
        "fields": [
            {
                "field": update["field"],
                "source": update["source"],
                "term_count": len(update["terms"]),
            }
            for update in updates
        ],
        "updated": update_response.get("updated", 0),
        "version_conflicts": update_response.get("version_conflicts", 0),
    }
    logger.info(
        "Persisted term fields %s on %s pages for doc_id=%s",
        [update["field"] for update in updates],
        result["updated"],
        variables.get("doc_id"),
    )
    return result


def run_item_stats(
    *,
    client: Any,
    stats_cfg: Mapping[str, Any],
    targets: str | Iterable[str],
    doc_id: str,
    item_iri: str | None = None,
) -> Dict[str, Any]:
    """Run the configured OpenSearch aggregation and write one JSON-LD file."""
    if doc_id is None:
        raise ValueError("stats require an item '_id'")

    cfg = _require_mapping(stats_cfg, "stats configuration")
    target_list = _targets_list(targets)
    top_n = int(cfg.get("top_n", cfg.get("size", 25)))
    if top_n < 1:
        raise ValueError("stats 'top_n' must be >= 1")

    safe_id = str(safe_doc_id(doc_id))
    if not safe_id:
        raise ValueError("safe_doc_id returned an empty value")

    variables: Dict[str, Any] = {
        "doc_id": doc_id,
        "safe_doc_id": safe_id,
        "item_iri": item_iri or "",
        "target": target_list[0],
        "targets": target_list,
        "top_n": top_n,
    }

    search_cfg = _require_mapping(cfg.get("search"), "stats 'search'")
    configured_index = search_cfg.get("index", cfg.get("index"))
    search_index = _render_templates(
        configured_index if configured_index is not None else "${target}",
        variables,
    )

    body = search_cfg.get("body", cfg.get("body"))
    if body is None:
        parent_cfg = _require_mapping(cfg.get("parent"), "stats 'parent'")
        parent_field = parent_cfg.get(
            "field",
            cfg.get("parent_field", "doc_id.keyword"),
        )
        parent_value = parent_cfg.get("value", "${doc_id}")
        query = cfg.get("query") or {
            "term": {parent_field: parent_value},
        }

        token_field = cfg.get("field", "text")
        significant_field = cfg.get("significant_field", token_field)
        aggregations = (
            cfg.get("aggregations")
            or cfg.get("aggs")
            or {
                "significant_tokens": {
                    "significant_text": {
                        "field": significant_field,
                        "size": top_n,
                        "min_doc_count": 1,
                    },
                },
            }
        )
        body = {
            "size": 0,
            "query": query,
            "aggs": aggregations,
        }

    rendered_body = _render_templates(body, variables)
    if not isinstance(rendered_body, Mapping):
        raise TypeError("stats search body must be a mapping")

    search_params = _merge_dicts(
        _require_mapping(cfg.get("params"), "stats 'params'"),
        _require_mapping(search_cfg.get("params"), "stats search 'params'"),
    )
    response = client.search(
        index=search_index,
        body=dict(rendered_body),
        **_render_templates(search_params, variables),
    )
    response = _require_mapping(response, "OpenSearch stats response")

    result_paths = cfg.get("result_paths")
    if result_paths is None:
        result_paths = {
            "frequentTokens": "aggregations.frequent_tokens.buckets",
            "significantTokens": "aggregations.significant_tokens.buckets",
        }
    result_paths = _require_mapping(result_paths, "stats 'result_paths'")

    statistics: Dict[str, Any] = {}
    for name, path in result_paths.items():
        result = _value_at_path(response, str(path))
        if result is not _MISSING:
            statistics[str(name)] = result

    significant_tokens = statistics.get("significantTokens")
    if isinstance(significant_tokens, list):
        significant_doc_count = _value_at_path(
            response,
            "aggregations.significant_tokens.doc_count",
            default=0,
        )
        significant_keys = [
            str(bucket["key"])
            for bucket in significant_tokens
            if isinstance(bucket, Mapping) and bucket.get("key") is not None
        ]
        logger.info(
            "Calculated %s significant terms from %s pages for doc_id=%s",
            len(significant_keys),
            significant_doc_count,
            doc_id,
        )
        logger.debug(
            "Significant terms for doc_id=%s: %s",
            doc_id,
            significant_keys,
        )

    frequent_cfg = _frequent_terms_config(cfg)
    if bool(frequent_cfg.get("enabled", False)):
        item_query = rendered_body.get("query")
        if not isinstance(item_query, Mapping):
            raise ValueError(
                "frequent_terms requires a mapping in stats.search.body.query"
            )
        statistics["frequentTokens"] = _most_frequent_terms(
            client=client,
            index=search_index,
            item_query=item_query,
            frequent_cfg=frequent_cfg,
            variables=variables,
            top_n=top_n,
        )

    persistence = None
    persist_entries, persist_options = _persist_terms_config(cfg)
    if persist_entries:
        persistence = _persist_terms(
            client=client,
            index=search_index,
            statistics=statistics,
            search_body=rendered_body,
            entries=persist_entries,
            options=persist_options,
            variables=variables,
        )

    jsonld_cfg = _require_mapping(cfg.get("jsonld"), "stats 'jsonld'")
    jsonld_enabled = bool(jsonld_cfg.get("enabled", True))
    destination: Path | None = None

    if jsonld_enabled:
        context_source = jsonld_cfg.get("context", cfg.get("context"))
        if isinstance(context_source, Mapping):
            context = context_source.get("@context", context_source)
        elif context_source is None or isinstance(context_source, (str, Path)):
            context = _load_jsonld_context_cached(
                None if context_source is None else str(context_source)
            )
        else:
            raise TypeError(
                "stats 'jsonld.context' must be a mapping, path string or null"
            )

        default_stats_iri = (
            f"{item_iri}/term-statistics"
            if item_iri
            else f"urn:opensearch:term-statistics:{safe_id}"
        )
        stats_iri = _render_templates(
            jsonld_cfg.get("id", default_stats_iri),
            variables,
        )
        jsonld: Dict[str, Any] = {
            "@context": context,
            "@id": stats_iri,
            "@type": jsonld_cfg.get("type", "TermStatistics"),
            "item": item_iri or f"urn:item:{safe_id}",
            "docId": doc_id,
            "index": search_index,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "statistics": statistics,
        }

        if jsonld_cfg.get("include_aggregations", False):
            jsonld["aggregations"] = response.get("aggregations", {})
        if jsonld_cfg.get("include_request", False):
            jsonld["request"] = {
                "index": search_index,
                "body": rendered_body,
            }
        if jsonld_cfg.get("include_hits", False):
            jsonld["hits"] = response.get("hits", {})
        if "took" in response:
            jsonld["took"] = response["took"]
        if "timed_out" in response:
            jsonld["timedOut"] = response["timed_out"]

        output = cfg.get("output", cfg.get("output_dir"))
        if not output:
            raise ValueError(
                "stats require 'output' or 'output_dir' when JSON-LD is enabled"
            )
        output_dir = _output_directory(_render_templates(output, variables))
        filename = _render_templates(
            cfg.get("filename", "${safe_doc_id}.jsonld"),
            variables,
        )
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("stats 'filename' must be a plain filename")

        destination = output_dir / filename
        _write_json_atomic(
            destination,
            jsonld,
            indent=int(jsonld_cfg.get("indent", 2)),
        )

    result = {
        "status": (
            "written"
            if destination is not None
            else "persisted"
            if persistence is not None
            else "analyzed"
        ),
        "index": search_index,
        "took": response.get("took"),
    }
    if destination is not None:
        result["output"] = str(destination)
    if persistence is not None:
        result["persistence"] = persistence
    return result


def analyze_item_stats(
    *,
    client: Any,
    targets: str | Iterable[str],
    doc_id: str,
    item_iri: str | None = None,
    ingest_digest: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    oscfg: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """Resolve configuration and run stats; return ``None`` when disabled."""
    cfg = resolve_stats_config(
        config_path=config_path,
        oscfg=oscfg,
        overrides=overrides,
    )
    if not cfg or not bool(cfg.get("enabled", True)):
        return None

    digest = dict(ingest_digest or {})
    incomplete_ingest = bool(digest.get("error")) or bool(
        digest.get("failed", 0)
    )
    if incomplete_ingest and not cfg.get("run_on_partial", False):
        return {
            "status": "skipped",
            "reason": "item indexing was incomplete",
        }

    try:
        return run_item_stats(
            client=client,
            stats_cfg=cfg,
            targets=targets,
            doc_id=doc_id,
            item_iri=item_iri,
        )
    except Exception as exc:
        logger.exception("OpenSearch statistics failed for doc_id=%s", doc_id)
        if cfg.get("strict", False):
            raise
        return {
            "status": "failed",
            "error": str(exc),
        }


def attach_item_stats(
    *,
    digest: Dict[str, Any],
    client: Any,
    targets: str | Iterable[str],
    doc_id: str,
    item_iri: str | None = None,
    config_path: str | Path | None = None,
    oscfg: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Pipeline hook: attach enabled stats output metadata to ``digest``."""
    result = analyze_item_stats(
        client=client,
        targets=targets,
        doc_id=doc_id,
        item_iri=item_iri,
        ingest_digest=digest,
        config_path=config_path,
        oscfg=oscfg,
        overrides=overrides,
    )
    if result is not None:
        digest["stats"] = result
    return digest


def run_stats_for_items(
    *,
    client,
    items,
    targets,
    stats_cfg,
    base_iri,
):
    from .export_data import make_item_data

    if not stats_cfg or not stats_cfg.get("enabled", False):
        return []

    item_list = list(items or [])
    total_items = len(item_list)
    persist_entries, _ = _persist_terms_config(stats_cfg)

    target_list = (
        [targets]
        if isinstance(targets, str)
        else list(targets)
    )

    if not target_list:
        raise ValueError("Stats require at least one target")

    missing_indices = [
        target
        for target in target_list
        if not client.indices.exists(index=target)
    ]

    if missing_indices:
        raise RuntimeError(
            f"Stats indices do not exist: {missing_indices}"
        )

    client.indices.refresh(index=target_list)

    results = []

    for position, obj in enumerate(item_list, start=1):
        doc_id = obj.get("_id")

        if not doc_id:
            logger.warning(
                "Stats item %s/%s skipped: missing _id",
                position,
                total_items,
            )
            results.append({
                "status": "skipped",
                "reason": "missing _id",
            })
            continue

        logger.info(
            "Stats item %s/%s started for doc_id=%s",
            position,
            total_items,
            doc_id,
        )

        export_item_data = make_item_data(
            obj,
            base_iri=base_iri,
        )

        try:
            result = run_item_stats(
                client=client,
                stats_cfg=stats_cfg,
                targets=target_list,
                doc_id=doc_id,
                item_iri=export_item_data.get("item_iri"),
            )

            logger.info(
                "Stats item %s/%s finished for doc_id=%s: %s",
                position,
                total_items,
                doc_id,
                result.get("output", result.get("status")),
            )

        except Exception as exc:
            logger.exception(
                "Stats item %s/%s failed for doc_id=%s",
                position,
                total_items,
                doc_id,
            )

            if stats_cfg.get("strict", False):
                raise

            result = {
                "status": "failed",
                "doc_id": doc_id,
                "error": str(exc),
            }

        results.append(result)

    if persist_entries:
        client.indices.refresh(index=target_list)

    return results


__all__ = [
    "analyze_item_stats",
    "attach_item_stats",
    "resolve_stats_config",
    "run_item_stats",
    "run_stats_for_items",
]
