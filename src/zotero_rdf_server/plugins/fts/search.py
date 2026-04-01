from typing import Any, Dict, List, Optional, Annotated
import html
from pydantic import BaseModel, Field

# --- OpenSearch client --------------------------------------------------------

from .db import make_client, resolve_config_path, get_os_config
from .helpers import plugin_logger
logger=plugin_logger()

cfg_path = resolve_config_path()
logger.debug(f"Loading config from {cfg_path}")
oscfg = get_os_config(cfg_path)
logger.debug(f"{oscfg}")
client = make_client(oscfg)
logger.info(f"Client config loaded from {cfg_path}")
DEFAULT_ALIAS = oscfg.get("meta", {}).get("default_alias", "ocr")
logger.info(f"DEFAULT_ALIAS: {DEFAULT_ALIAS}")

# --- Helpers -----------------------------------------------------------------

DEFAULT_SIZE = 10
from .endpoints import MAX_SIZE

def apply_paging(
    body: Dict[str, Any],
    *,
    size: Optional[int] = None,
    offset: int = 0,
    max_size: int = MAX_SIZE,
) -> Dict[str, Any]:
    """
    Apply pagination controls to an OpenSearch search body.
    - size: number of hits to return
    - offset: starting hit (from)
    - max_size: hard cap to protect the cluster
    """
    if size is None:
        size = DEFAULT_SIZE

    # Hard cap
    size = min(int(size), int(max_size))
    offset = max(int(offset), 0)

    body["size"] = size
    if offset:
        body["from"] = offset

    return body

def parse_csv(raw: str) -> List[str]:
    """Parse comma-separated terms, trimming whitespace and dropping empties."""
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    if not terms:
        raise ValueError("No terms provided.")
    return terms

def maybe_guard_prefix(term: str, min_len: int = 3) -> bool:
    """Return True if the term is long enough to use prefix matching."""
    return len(term) >= min_len

def effective_fuzzy_edits(term: str, requested_edits: int) -> int:
    """
    Guard fuzzy expansions on short terms (common OCR scenario).
    - Very short tokens explode combinatorially with fuzziness 2.
    """
    if requested_edits <= 0:
        return 0
    if len(term) < 5:
        return min(requested_edits, 1)
    return min(requested_edits, 2)

def get_doc_vector(index: str, os_id: str, vector_field: str = "vector") -> List[float]:
    """Fetch a document and return its vector from _source."""
    if not index: index = DEFAULT_ALIAS
    doc = client.get(index=index, id=os_id)
    src = doc.get("_source", {})
    vec = src.get(vector_field)
    if vec is None:
        raise KeyError(f"Document has no '{vector_field}' in _source.")
    return vec

def build_terms_should_queries(
    terms: List[str],
    field: str = "text",
    exact: bool = True,
    truncated: bool = True,
    fuzzy: bool = True,
    phrase_slop: int = 2,
    prefix_max_expansions: int = 50,
    fuzzy_max_expansions: int = 50,
    fuzzy_prefix_length: int = 1,
    fuzzy_edits: int = 2,
    min_prefix_len: int = 3,
) -> List[Dict[str, Any]]:
    """
    Build a list of should-clauses for:
    - "exact"  => match_phrase (analyzed phrase match)
    - "truncated" => match_phrase_prefix (last token treated as prefix)
    - "fuzzy"  => match with fuzziness (OCR-robust)
    Any single clause matching is enough when minimum_should_match=1.
    """
    should: List[Dict[str, Any]] = []

    for t in terms:
        if exact:
            should.append({"match_phrase": {field: {"query": t, "slop": phrase_slop}}})

        if truncated and maybe_guard_prefix(t, min_len=min_prefix_len):
            should.append({"match_phrase_prefix": {field: {"query": t, "max_expansions": prefix_max_expansions}}})

        if fuzzy:
            edits = effective_fuzzy_edits(t, fuzzy_edits)
            if edits > 0:
                should.append(
                    {
                        "match": {
                            field: {
                                "query": t,
                                "fuzziness": edits,
                                "prefix_length": fuzzy_prefix_length,
                                "max_expansions": fuzzy_max_expansions,
                            }
                        }
                    }
                )

    return should

def intervals_term_rule(
    term: str,
    allow_match: bool,
    allow_prefix: bool,
    allow_fuzzy: bool,
    fuzzy_edits: int = 1,
    min_prefix_len: int = 3,
) -> Dict[str, Any]:
    """
    Build a single intervals rule for one term as an any_of across allowed modes.
    intervals supports: match, prefix, fuzzy (and wildcard).
    """
    rules: List[Dict[str, Any]] = []

    if allow_match:
        rules.append({"match": {"query": term}})

    if allow_prefix and maybe_guard_prefix(term, min_len=min_prefix_len):
        rules.append({"prefix": {"prefix": term}})

    if allow_fuzzy:
        edits = effective_fuzzy_edits(term, fuzzy_edits)
        if edits > 0:
            rules.append({"fuzzy": {"term": term, "fuzziness": edits}})

    if not rules:
        # Caller ensured at least one mode; this can happen if term is too short for prefix and fuzziness=0.
        # Fall back to match to avoid empty intervals.
        rules.append({"match": {"query": term}})

    if len(rules) == 1:
        return rules[0]

    return {"any_of": {"intervals": rules}}

def build_proximity_intervals_query(
    list_a: List[str],
    list_b: List[str],
    field: str = "text",
    proximity: int = 5,
    ordered: bool = False,
    allow_match: bool = True,
    allow_prefix: bool = True,
    allow_fuzzy: bool = True,
    fuzzy_edits: int = 1,
) -> Dict[str, Any]:
    """
    Build a query that:
    - if proximity == -1: requires both terms to occur somewhere in the document
    - if proximity >= 0: requires both terms to occur within `proximity` token gaps
    """
    # --- Case 1: proximity == 0 → simple AND in the same document ------------
    if proximity == -1:
        should_pairs: List[Dict[str, Any]] = []

        for a in list_a:
            a_rule = intervals_term_rule(a, allow_match, allow_prefix, allow_fuzzy, fuzzy_edits=fuzzy_edits)
            for b in list_b:
                b_rule = intervals_term_rule(b, allow_match, allow_prefix, allow_fuzzy, fuzzy_edits=fuzzy_edits)

                should_pairs.append(
                    {
                        "bool": {
                            "must": [
                                {"intervals": {field: a_rule}},
                                {"intervals": {field: b_rule}},
                            ]
                        }
                    }
                )

        return {
            "query": {
                "bool": {
                    "should": should_pairs,
                    "minimum_should_match": 1,
                }
            }
        }

    # --- Case 2: proximity >= 0 → true proximity via intervals -----------------
    pair_intervals: List[Dict[str, Any]] = []

    for a in list_a:
        a_rule = intervals_term_rule(a, allow_match, allow_prefix, allow_fuzzy, fuzzy_edits=fuzzy_edits)
        for b in list_b:
            b_rule = intervals_term_rule(b, allow_match, allow_prefix, allow_fuzzy, fuzzy_edits=fuzzy_edits)

            pair_intervals.append(
                {
                    "all_of": {
                        "intervals": [a_rule, b_rule],
                        "max_gaps": proximity,
                        "ordered": ordered,
                    }
                }
            )

    return {
        "query": {
            "intervals": {
                field: {
                    "any_of": {
                        "intervals": pair_intervals
                    }
                }
            }
        }
    }

import csv, io, datetime, json

def _all_highlight_fragments(
    h: Dict[str, Any],
    preferred_field: Optional[str] = None,
    sep: str = " … "
) -> Optional[str]:
    hl = h.get("highlight") or {}
    if not hl:
        return None

    if preferred_field and preferred_field in hl and hl[preferred_field]:
        return sep.join(hl[preferred_field])

    for _, frags in hl.items():
        if frags:
            return sep.join(frags)
    return None

def _truncate_text(s: str, n: int) -> str:
    if n <= 0 or len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


from typing import Dict, Any, List, Optional

def flatten_dicts(
    row: Dict[str, Any],
    keys: List[str] = ("meta",),
    prefix_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:

    def walk(d: Dict[str, Any], path: List[str], out: Dict[str, Any], prefix: str):
        for k, v in d.items():
            new_path = path + [str(k)]
            if isinstance(v, dict):
                walk(v, new_path, out, prefix)
            else:
                out[prefix + "_".join(new_path)] = v

    out = dict(row)

    for key in keys:
        val = row.get(key)
        if not isinstance(val, dict):
            continue

        prefix = (
            prefix_map[key]
            if prefix_map and key in prefix_map
            else f"{key}_"
        )

        walk(val, [], out, prefix)

    return out

def normalize_hits(
    resp: Dict[str, Any],
    flatten_dict: Optional[List[str]] = None,
    keep_dict: Optional[List[str]] = None,
    *,
    keep_highlight: bool = True,
    make_snippet: bool = True,
    highlight_field: Optional[str] = None,
    truncate_chars: int = 0,
    truncate_field: str = "text",   # fallback field name in _source
) -> Dict[str, Any]:
    """Normalize OpenSearch response to a stable wrapper structure; optionally flatten 'meta'."""
    hits = resp.get("hits", {}).get("hits", [])

    rows: List[Dict[str, Any]] = []
    for h in hits:
        row: Dict[str, Any] = {"_id": h.get("_id"), "_score": h.get("_score")}
        src = h.get("_source") or {}
        row.update(src)

        if keep_highlight and "highlight" in h:
            row["highlight"] = h.get("highlight") or {}

        if make_snippet:
            frag = _all_highlight_fragments(h, preferred_field=highlight_field)
            if frag:
                row["snippet"] = frag
            elif truncate_chars > 0:
                txt = row.get(truncate_field)
                if isinstance(txt, str) and txt:
                    row["snippet"] = _truncate_text(txt, truncate_chars)

        if flatten_dict:
            row = flatten_dicts(row, keys=flatten_dict)
            for k in flatten_dict:
                if k not in keep_dict:
                    row.pop(k, None)

        rows.append(row)

    return {
        "total": resp.get("hits", {}).get("total"),
        "hits": rows,
        "aggregations": resp.get("aggregations"),
    }


def flatten_value(v: Any) -> str:
    """Convert nested values to a stable string representation for CSV/Markdown."""
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return str(v)

def collect_columns(rows: List[Dict[str, Any]], preferred: Optional[List[str]] = None) -> List[str]:
    keys = set()
    for r in rows:
        keys.update(r.keys())

    preferred = preferred or []
    cols = [c for c in preferred if c in keys]
    rest = sorted(k for k in keys if k not in cols)
    return cols + rest

def render_csv(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([flatten_value(r.get(c)) for c in columns])
    return buf.getvalue()

# TODO currently not used
def render_markdown_table(rows: List[Dict[str, Any]], columns: List[str], max_rows: int = 150) -> str:
    rows = rows[:max_rows]
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, sep]
    for r in rows:
        line = "| " + " | ".join(flatten_value(r.get(c)).replace("\n", " ") for c in columns) + " |"
        lines.append(line)
    return "\n".join(lines)

import re

_EM_RE = re.compile(r"<em>(.*?)</em>", flags=re.DOTALL)

def highlight_html_to_markdown(text: str, pre: str = "**", post: str = "**") -> str:
    # Convert <em>...</em> fragments from OpenSearch highlight to Markdown emphasis
    return _EM_RE.sub(lambda m: f"{pre}{m.group(1)}{post}", text)

def highlight_html_to_html(text: str, pre: str = "<strong>", post: str = "</strong>") -> str:
    """
    Convert OpenSearch highlight fragments (<em>...</em>) to desired HTML tags.
    """
    return _EM_RE.sub(lambda m: f"{pre}{m.group(1)}{post}", text)

def normalize_md_block(text: str) -> str:
    """
    Keep your 'sheet style' printable blocks readable:
    - replace CRLF/LF with spaces (or <br> if you prefer)
    - avoid accidental markdown code fences etc (optional)
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ")
    return text

def normalize_html_block(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", " ")

def extract_buckets(aggs: dict, agg_name: str = "by_field"):
    """
    Normalizes aggregation response and returns buckets list.
    Works with and without sampler.
    """
    if not aggs or agg_name not in aggs:
        return []

    node = aggs[agg_name]

    # sampler case
    if "values" in node:
        return node["values"].get("buckets", [])

    # normal case
    return node.get("buckets", [])

def is_significant(bucket: dict) -> bool:
    return "score" in bucket

def render_markdown_table(buckets: list[dict]) -> str:
    if not buckets:
        return "_No aggregation results_\n\n"

    has_score = is_significant(buckets[0])

    header = "| term | doc_count |"
    if has_score:
        header += " score | bg_count |"
    header += "\n"

    separator = "|------|-----------|"
    if has_score:
        separator += "-------|----------|"
    separator += "\n"

    rows = ""
    for b in buckets:
        rows += f"| {b.get('key')} | {b.get('doc_count')} |"
        if has_score:
            rows += f" {round(b.get('score', 0), 3)} | {b.get('bg_count')} |"
        rows += "\n"

    return header + separator + rows + "\n"
import html

def render_html_table(buckets: list[dict]) -> str:
    if not buckets:
        return "<p><em>No aggregation results</em></p>"

    has_score = is_significant(buckets[0])

    html_rows = ""

    for b in buckets:
        html_rows += "<tr>"
        html_rows += f"<td>{html.escape(str(b.get('key')))}</td>"
        html_rows += f"<td>{b.get('doc_count')}</td>"

        if has_score:
            html_rows += f"<td>{round(b.get('score', 0), 3)}</td>"
            html_rows += f"<td>{b.get('bg_count')}</td>"

        html_rows += "</tr>"

    header = "<tr><th>term</th><th>doc_count</th>"
    if has_score:
        header += "<th>score</th><th>bg_count</th>"
    header += "</tr>"

    return f"<table><thead>{header}</thead><tbody>{html_rows}</tbody></table>"

def render_markdown(
    rows: List[Dict[str, Any]],
    columns: List[str],
    *,
    max_rows: int = 150,
    title: str = "Search Results",
    highlight_pre: str = "**",
    highlight_post: str = "**",
    verbose: bool = True,
) -> str:
    
    try:
        from .viewer import BASE_URL
    except:
        BASE_URL = "/plugin/fts/view"

    lines: List[str] = []
    rows = rows[:max_rows]

    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"Total documents shown: **{len(rows)}**")
    lines.append("")

    for idx, row in enumerate(rows, start=1):
        doc_id = str(row.get("_id"))

        lines.append("---")
        if doc_id:
            if verbose:
                doc_value = f"[{doc_id}]({BASE_URL}/{doc_id})"
            else:
                doc_value = doc_id
            lines.append(f"## Document {idx} ({doc_value})")
        else:
            lines.append(f"## Document {idx}")

        lines.append("")

        for col in columns:
            if col not in row:
                continue

            raw_value = row.get(col)
            value = flatten_value(raw_value)
            if value == "" or col == "_id":
                continue

            # if col == "_id" and verbose:
            #     value = f"[{doc_id}]({BASE_URL}/{value})"

            if str(raw_value).startswith("http"):
                safe_url = html.escape(raw_value)
                if verbose:
                    value = f"[{safe_url}](safe_url)"
                else:
                    lines.append(f"[**{col}**](safe_url)")
                    lines.append("")
                    continue

            elif isinstance(value, str):
                value = highlight_html_to_markdown(value, pre=highlight_pre, post=highlight_post)
                value = normalize_md_block(value)

            lines.append(f"**{col}**")
            lines.append("")
            lines.append(f"{value}")
            lines.append("")

    return "\n".join(lines)

def render_html(
    rows: List[Dict[str, Any]],
    columns: List[str],
    *,
    max_rows: int = 150,
    title: str = "Search Results",
    highlight_pre: str = "<strong>",
    highlight_post: str = "</strong>",
    verbose: bool = True,
) -> str:
    parts: List[str] = []
    rows = rows[:max_rows]

    try:
        from .viewer import BASE_URL
    except:
        BASE_URL = "/plugin/fts/view"

    parts.append(f"<h1>{html.escape(title)}</h1>")
    parts.append(f"<p>Total documents shown: <strong>{len(rows)}</strong></p>")

    for idx, row in enumerate(rows, start=1):
        doc_id = str(row.get("_id"))

        parts.append("<hr>")
        if doc_id:
            if verbose:
                doc_value = f'<a href="{BASE_URL}/{doc_id}" target="_blank">{doc_id}</a>'
            else:
                doc_value = doc_id

            parts.append(f"<h2>Document {idx} ({doc_value})</h2>")
        else:
            parts.append(f"<h2>Document {idx}</h2>")

        analysis_fields = []
        normal_fields = []

        for col in columns:
            if col not in row or col == "_id":
                continue

            if str(col).startswith("analysis_"):
                analysis_fields.append(col)
            else:
                normal_fields.append(col)

        for col in normal_fields:
            raw_value = row.get(col)
            value = flatten_value(raw_value)

            if value == "":
                continue

            if str(raw_value).startswith("http"):
                safe_url = html.escape(raw_value)
                if verbose:
                    value_html = f'<a href="{safe_url}" target="_blank">{safe_url}</a>'
                else:
                    parts.append(f'<p><strong><a href="{safe_url}" target="_blank">{html.escape(col)}</a></strong></p>')
                    continue
            else:
                if isinstance(value, str):
                    value = highlight_html_to_html(value, pre=highlight_pre, post=highlight_post)

                    value = value.replace(highlight_pre, "___H_PRE___").replace(highlight_post, "___H_POST___")
                    value = html.escape(value)
                    value = value.replace("___H_PRE___", highlight_pre).replace("___H_POST___", highlight_post)

                    value = normalize_html_block(value)
                else:
                    value = html.escape(str(value))

                value_html = value

            parts.append(f"<p><strong>{html.escape(col)}</strong></p>")
            parts.append(f"<p>{value_html}</p>")

        if analysis_fields:
            parts.append("<h3>Analysis</h3>")

            for col in analysis_fields:
                raw_value = row.get(col)
                value = flatten_value(raw_value)

                if value == "":
                    continue

                value_html = html.escape(str(value))

                parts.append(f"<p><i>{html.escape(col)}</i></p>")
                parts.append(f"<p>{value_html}</p>")

    return "\n".join(parts)

def render_html_query_header(context_query: Dict[str, Any]) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pretty = html.escape(json.dumps(context_query, indent=2, ensure_ascii=False))

    return (
        "<h1>Search</h1>\n"
        f"<p><strong>Generated:</strong> {ts}</p>\n"
        "<p><strong>Query:</strong></p>\n"
        f"<pre><code>{pretty}</code></pre>\n"
    )

def render_markdown_query_header(context_query: Dict[str, Any]) -> str:
    """Render the OpenSearch query as a markdown code block."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pretty = json.dumps(context_query, indent=2, ensure_ascii=False)

    return (
        "# Search\n\n"
        f"**Generated:** {ts}\n\n"
        "**Query:**\n\n"
        "```json\n"
        f"{pretty}\n"
        "```\n\n"
    )

def normalize_output_column(
    col: str,
    flatten_keys: Optional[List[str]] = ["meta", "analysis"],
) -> str:   
    for key in flatten_keys:
        prefix = f"{key}."
        if col.startswith(prefix):
            return col.replace(".", "_")
    return col

def apply_source_includes(body: Dict[str, Any], columns: Optional[str]) -> None:
    """Mutate body to include _source filtering based on columns."""
    if not columns:
        return

    requested = [c.strip() for c in columns.split(",") if c.strip()]

    def to_source_field(col: str) -> Optional[str]:
        # not part of _source
        if col in ("_id", "_score", "snippet", "highlight"):
            return None

        # already source-style path
        if "." in col:
            return col

        # backward compatibility for flattened meta_* input
        if col.startswith("meta_"):
            return "meta." + col[len("meta_"):]

        return col

    includes = []
    for col in requested:
        source_field = to_source_field(col)
        if source_field:
            includes.append(source_field)

    includes = list(dict.fromkeys(includes))

    if includes:
        body["_source"] = {"includes": includes}

def os_search(index: str, body: Dict[str, Any], columns: Optional[str]) -> Dict[str, Any]:
    """Central OpenSearch search that applies _source includes if columns is set."""    
    apply_source_includes(body, columns)
    if not index: index = DEFAULT_ALIAS
    return client.search(index=index, body=body)

def os_mtermvectors(
    *,
    index: str,
    doc_ids: List[str],
    field: str,
) -> Dict[str, Any]:
    if not doc_ids:
        return {"docs": []}
    
    if not index: index = DEFAULT_ALIAS
    return client.mtermvectors(
        index=index,
        body={
            "ids": doc_ids,
        },
        params={
            "fields": field,
            "term_statistics": "true",
            "field_statistics": "true",
            "positions": "false",
            "offsets": "false",
            "payloads": "false",
        },
    )

from .endpoints import KeywordFilter

def apply_keyword_filter(body: dict, filters: KeywordFilter):
    if not filters.filter_field:
        return

    if not filters.filter_value and not filters.filter_values:
        return

    if "query" not in body:
        body["query"] = {"match_all": {}}

    base_query = body["query"]

    if filters.filter_values:
        clause = {"terms": {filters.filter_field: filters.filter_values}}
    else:
        clause = {"term": {filters.filter_field: filters.filter_value}}

    if isinstance(base_query, dict) and "bool" in base_query:
        base_query["bool"].setdefault("filter", []).append(clause)
    else:
        body["query"] = {
            "bool": {
                "must": [base_query],
                "filter": [clause],
            }
        }

from .endpoints import IngestTsRangeFilter

def apply_ingest_ts_range_filter(body: dict, ingest_filter: IngestTsRangeFilter):
    if not ingest_filter.ingest_from and not ingest_filter.ingest_to:
        return

    if "query" not in body:
        body["query"] = {"match_all": {}}

    base_query = body["query"]

    range_spec = {}

    if ingest_filter.ingest_from is not None:
        range_spec["gte"] = ingest_filter.ingest_from.isoformat()

    if ingest_filter.ingest_to is not None:
        range_spec["lte"] = ingest_filter.ingest_to.isoformat()

    clause = {
        "range": {
            ingest_filter.ingest_field: range_spec
        }
    }

    if isinstance(base_query, dict) and "bool" in base_query:
        base_query["bool"].setdefault("filter", []).append(clause)
    else:
        body["query"] = {
            "bool": {
                "must": [base_query],
                "filter": [clause],
            }
        }

# NLP


from .endpoints import ResultAnalysisParams
from .helpers import ensure_import
from copy import deepcopy
import math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple


try:
    ensure_import("sklearn")
    ensure_import("rapidfuzz")
    from rapidfuzz import fuzz
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
except Exception as e:
    logger.error(f"Failed to import ML packages: {e}")

def _terms_from_tv_doc(
    tv_doc: Dict[str, Any],
    *,
    field: str,
    min_token_length: int,
    max_tokens_per_doc: int,
) -> List[Tuple[str, Dict[str, Any]]]:
    field_data = tv_doc.get("term_vectors", {}).get(field, {})
    terms = field_data.get("terms", {})

    items = [
        (term, stats)
        for term, stats in terms.items()
        if len(term) >= min_token_length
    ]

    if max_tokens_per_doc > 0:
        items = sorted(
            items,
            key=lambda item: item[1].get("term_freq", 0),
            reverse=True,
        )[:max_tokens_per_doc]

    return items

def _pseudo_doc_from_terms(term_items: List[Tuple[str, Dict[str, Any]]]) -> str:
    """
    Baut aus mtermvectors-Terms ein Pseudo-Dokument für TfidfVectorizer.
    Jeder Term wird entsprechend seiner term_freq wiederholt.
    """
    tokens: List[str] = []
    for term, stats in term_items:
        tf = int(stats.get("term_freq", 0) or 0)
        if tf > 0:
            tokens.extend([term] * tf)
    return " ".join(tokens)

def _build_readable_local_terms(
    *,
    doc_ids: List[str],
    tv_by_id: Dict[str, Dict[str, Any]],
    field: str,
    analysis: ResultAnalysisParams,
) -> Dict[str, List[Dict[str, Any]]]:
    local_doc_freq = Counter()
    usable_docs = []

    for doc_id in doc_ids:
        tv_doc = tv_by_id.get(doc_id)
        if not tv_doc or not tv_doc.get("found"):
            continue

        term_items = _terms_from_tv_doc(
            tv_doc,
            field=field,
            min_token_length=analysis.analyze_min_token_length,
            max_tokens_per_doc=analysis.analyze_max_tokens_per_doc,
        )
        usable_docs.append((doc_id, term_items))

        for term, _ in term_items:
            local_doc_freq[term] += 1

    n_docs = max(1, len(usable_docs))
    out: Dict[str, List[Dict[str, Any]]] = {}

    for doc_id, term_items in usable_docs:
        total_terms = sum(stats.get("term_freq", 0) for _, stats in term_items) or 1
        scored = []

        for term, stats in term_items:
            tf = stats.get("term_freq", 0)
            df = local_doc_freq.get(term, 0)
            tf_norm = tf / total_terms
            idf = math.log((1 + n_docs) / (1 + df)) + 1.0
            score = tf_norm * idf

            scored.append({
                "term": term,
                "score": round(score, 6),
                "doc_freq": df,
                "term_freq": tf,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        out[doc_id] = scored[:analysis.analyze_top_terms]

    return out

def _build_local_tfidf_from_vectorizer(
    *,
    doc_ids: List[str],
    tv_by_id: Dict[str, Dict[str, Any]],
    field: str,
    analysis: ResultAnalysisParams,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, float]]]:
    pseudo_docs: List[str] = []
    ordered_doc_ids: List[str] = []

    for doc_id in doc_ids:
        tv_doc = tv_by_id.get(doc_id)
        if not tv_doc or not tv_doc.get("found"):
            continue

        term_items = _terms_from_tv_doc(
            tv_doc,
            field=field,
            min_token_length=analysis.analyze_min_token_length,
            max_tokens_per_doc=analysis.analyze_max_tokens_per_doc,
        )

        pseudo_doc = _pseudo_doc_from_terms(term_items)
        ordered_doc_ids.append(doc_id)
        pseudo_docs.append(pseudo_doc)

    if not pseudo_docs:
        return {}, {}

    use_char_ngrams = getattr(analysis, "analyze_use_char_ngrams", True)
    char_ngram_range = getattr(analysis, "analyze_char_ngram_range", (3, 5))

    if use_char_ngrams:
        vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=char_ngram_range,
            lowercase=False,
            norm="l2",
            use_idf=True,
            smooth_idf=True,
            sublinear_tf=False,
        )
    else:
        vectorizer = TfidfVectorizer(
            analyzer="word",
            token_pattern=r"(?u)\b\w+\b",
            lowercase=False,
            norm="l2",
            use_idf=True,
            smooth_idf=True,
            sublinear_tf=False,
        )

    matrix = vectorizer.fit_transform(pseudo_docs)
    features = vectorizer.get_feature_names_out()

    doc_top_terms: Dict[str, List[Dict[str, Any]]] = {}
    doc_vectors: Dict[str, Dict[str, float]] = {}

    for row_idx, doc_id in enumerate(ordered_doc_ids):
        row = matrix.getrow(row_idx)
        indices = row.indices
        data = row.data

        vector = {
            features[col_idx]: round(float(score), 6)
            for col_idx, score in zip(indices, data)
        }
        doc_vectors[doc_id] = vector

        top = sorted(
            vector.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:analysis.analyze_top_terms]

        doc_top_terms[doc_id] = [
            {
                "term": term,
                "score": score,
            }
            for term, score in top
        ]

    return doc_top_terms, doc_vectors

def analysis_from_mtermvectors(
    *,
    hits: List[Dict[str, Any]],
    tv_resp: Dict[str, Any],
    analysis: ResultAnalysisParams,
    field_fallback: str,
) -> List[Dict[str, Any]]:
    analysis_field = analysis.analyze_field or field_fallback
    docs = tv_resp.get("docs", [])

    tv_by_id: Dict[str, Dict[str, Any]] = {
        d.get("_id"): d for d in docs if d.get("_id")
    }

    def wants_global() -> bool:
        return analysis.analysis_mode in ("global", "both")

    def wants_local() -> bool:
        return analysis.analysis_mode in ("local", "both")

    def build_global_scored_terms(
        *,
        filtered_items: List[Tuple[str, Dict[str, Any]]],
        n_docs: int,
    ) -> List[Dict[str, Any]]:
        total_terms = sum(stats.get("term_freq", 0) for _, stats in filtered_items) or 1

        scored: List[Dict[str, Any]] = []
        for term, stats in filtered_items:
            term_freq = stats.get("term_freq", 0)
            doc_freq = stats.get("doc_freq", 0)

            tf_norm = term_freq / total_terms
            idf = math.log((1 + n_docs) / (1 + doc_freq)) + 1.0
            score = tf_norm * idf

            scored.append(
                {
                    "term": term,
                    "score": round(score, 6),
                    "doc_freq": doc_freq,
                    "term_freq": term_freq,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:analysis.analyze_top_terms]

    doc_ids = [h.get("_id") for h in hits if h.get("_id")]

    local_top_terms_by_id = {}
    local_vectors_by_id = {}

    if wants_local():
        local_top_terms_by_id = _build_readable_local_terms(
            doc_ids=doc_ids,
            tv_by_id=tv_by_id,
            field=analysis_field,
            analysis=analysis,
        )

        _, local_vectors_by_id = _build_local_tfidf_from_vectorizer(
            doc_ids=doc_ids,
            tv_by_id=tv_by_id,
            field=analysis_field,
            analysis=analysis,
        )

    enriched: List[Dict[str, Any]] = []

    for hit in hits:
        hit_copy = dict(hit)
        doc_id = hit.get("_id")
        tv_doc = tv_by_id.get(doc_id)

        derived: Dict[str, Any] = {
            "field": analysis_field,
            "mode": analysis.analysis_mode,
            "top_terms": analysis.analyze_top_terms,
            "min_token_length": analysis.analyze_min_token_length,
            "max_tokens_per_doc": analysis.analyze_max_tokens_per_doc,
            "terms_total": 0,
        }

        if tv_doc and tv_doc.get("found"):
            term_items = _terms_from_tv_doc(
                tv_doc,
                field=analysis_field,
                min_token_length=analysis.analyze_min_token_length,
                max_tokens_per_doc=analysis.analyze_max_tokens_per_doc,
            )
            derived["terms_total"] = len(term_items)

            if wants_global():
                field_data = tv_doc.get("term_vectors", {}).get(analysis_field, {})
                field_stats = field_data.get("field_statistics", {})
                global_n_docs = field_stats.get("doc_count", 1) or 1

                global_top = build_global_scored_terms(
                    filtered_items=term_items,
                    n_docs=global_n_docs,
                )
                derived["global"] = {
                    "key_terms": [item["term"] for item in global_top],
                    "key_terms_details": global_top,
                }

            if wants_local():
                local_top = local_top_terms_by_id.get(doc_id, [])
                derived["local"] = {
                    "key_terms": [item["term"] for item in local_top],
                    "key_terms_details": local_top,
                    "vector": local_vectors_by_id.get(doc_id, {}),
                }

        hit_copy.setdefault("_source", {})
        hit_copy["_source"].setdefault("analysis", {})
        hit_copy["_source"]["analysis"].update(derived)
        enriched.append(hit_copy)

    return enriched

### Cluster

def _l2_normalize(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]

def _extract_term_scores(
    hit: Dict[str, Any],
    *,
    analysis_branch: str,
    prefer_vector: bool = False,
) -> Dict[str, float]:
    source = hit.get("_source", {})
    analysis = source.get("analysis", {})
    branch = analysis.get(analysis_branch, {})

    if prefer_vector:
        vector = branch.get("vector") or {}
        if vector:
            return {str(term): float(score) for term, score in vector.items()}

    details = branch.get("key_terms_details") or []
    if details:
        out: Dict[str, float] = {}
        for item in details:
            term = item.get("term")
            score = item.get("score", 0.0)
            if term:
                out[str(term)] = float(score)
        return out

    key_terms = branch.get("key_terms") or []
    if key_terms:
        return {str(term): 1.0 for term in key_terms if term}

    vector = branch.get("vector") or {}
    return {str(term): float(score) for term, score in vector.items()}

def _canonical_term(group: List[Tuple[str, float]]) -> str:
    terms = [term for term, _ in group]

    def centrality(term: str) -> float:
        return sum(fuzz.ratio(term, other) for other in terms)

    ranked = sorted(
        group,
        key=lambda x: (
            -x[1],
            -centrality(x[0]),
            len(x[0]),
            x[0],
        )
    )
    return ranked[0][0]

def _merge_similar_terms(
    term_scores: Dict[str, float],
    *,
    similarity_threshold: int = 90,
) -> Dict[str, float]:
    items = sorted(term_scores.items(), key=lambda x: x[1], reverse=True)
    groups: List[List[Tuple[str, float]]] = []

    for term, score in items:
        best_group_idx = None
        best_sim = -1

        for gi, group in enumerate(groups):
            sim = max(fuzz.ratio(term, existing_term) for existing_term, _ in group)
            if sim >= similarity_threshold and sim > best_sim:
                best_group_idx = gi
                best_sim = sim

        if best_group_idx is None:
            groups.append([(term, score)])
        else:
            groups[best_group_idx].append((term, score))

    merged: Dict[str, float] = {}
    for group in groups:
        canonical = _canonical_term(group)
        merged[canonical] = sum(score for _, score in group)

    return merged

def _build_cluster_label(
    *,
    cluster_term_scores: List[Dict[str, float]],
    all_term_scores: List[Dict[str, float]],
    top_label_terms: int = 10,
    fuzzy_merge: bool = True,
    similarity_threshold: int = 88,
) -> Tuple[str, List[str]]:
    cluster_agg = Counter()
    for term_scores in cluster_term_scores:
        cluster_agg.update(term_scores)

    hit_df = Counter()
    for term_scores in all_term_scores:
        for term in term_scores.keys():
            hit_df[term] += 1

    n_hits = max(1, len(all_term_scores))
    scored = {}

    for term, tf in cluster_agg.items():
        df = hit_df.get(term, 0)
        idf = math.log((1 + n_hits) / (1 + df)) + 1.0
        scored[term] = tf * idf

    if fuzzy_merge:
        scored = _merge_similar_terms(
            scored,
            similarity_threshold=similarity_threshold,
        )

    top_terms = [
        term for term, _ in sorted(
            scored.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:top_label_terms]
    ]

    label = ", ".join(top_terms) if top_terms else "unlabeled"
    return label, top_terms

def cluster_hits_by_analysis(
    hits: List[Dict[str, Any]],
    *,
    analysis: ResultAnalysisParams,
    random_state: int = 42,
    normalize_vectors: bool = True,
) -> List[Dict[str, Any]]:
    if not hits or not analysis.cluster_enabled:
        return hits

    hits_copy = [deepcopy(hit) for hit in hits]

    cluster_term_scores_per_hit: List[Dict[str, float]] = [
        _extract_term_scores(
            hit,
            analysis_branch=analysis.cluster_source,
            prefer_vector=True,   # Clustering
        )
        for hit in hits_copy
    ]

    label_term_scores_per_hit: List[Dict[str, float]] = [
        _extract_term_scores(
            hit,
            analysis_branch=analysis.cluster_label_source,
            prefer_vector=False,  # Labels
        )
        for hit in hits_copy
    ]

    usable_indices = [i for i, term_scores in enumerate(cluster_term_scores_per_hit) if term_scores]
    usable_index_set = set(usable_indices)

    if not usable_indices:
        for hit in hits_copy:
            hit.setdefault("_source", {})
            hit["_source"].setdefault("analysis", {})
            hit["_source"]["analysis"]["cluster"] = {
                "id": None,
                "label": "unclustered",
                "label_terms": [],
                "size": 0,
                "source": analysis.cluster_source,
                "label_source": analysis.cluster_label_source,
            }
        return hits_copy

    vocabulary = sorted(
        {
            term
            for i in usable_indices
            for term in cluster_term_scores_per_hit[i].keys()
        }
    )
    if not vocabulary:
        for hit in hits_copy:
            hit.setdefault("_source", {})
            hit["_source"].setdefault("analysis", {})
            hit["_source"]["analysis"]["cluster"] = {
                "id": None,
                "label": "unclustered",
                "label_terms": [],
                "size": 0,
                "source": analysis.cluster_source,
                "label_source": analysis.cluster_label_source,
            }
        return hits_copy

    term_to_col = {term: idx for idx, term in enumerate(vocabulary)}

    matrix: List[List[float]] = []
    usable_hit_refs: List[int] = []

    for i in usable_indices:
        row = [0.0] * len(vocabulary)
        for term, score in cluster_term_scores_per_hit[i].items():
            row[term_to_col[term]] = float(score)
        if normalize_vectors:
            row = _l2_normalize(row)
        matrix.append(row)
        usable_hit_refs.append(i)

    effective_k = max(1, min(analysis.cluster_count, len(matrix)))
    hit_index_to_row = {hit_idx: row_idx for row_idx, hit_idx in enumerate(usable_hit_refs)}

    if len(matrix) == 1:
        only_idx = usable_hit_refs[0]
        label, label_terms = _build_cluster_label(
            cluster_term_scores=[label_term_scores_per_hit[only_idx]],
            all_term_scores=label_term_scores_per_hit,
            top_label_terms=analysis.cluster_label_top_terms,
        )
        for i, hit in enumerate(hits_copy):
            hit.setdefault("_source", {})
            hit["_source"].setdefault("analysis", {})
            if i == only_idx:
                hit["_source"]["analysis"]["cluster"] = {
                    "id": 0,
                    "label": label,
                    "label_terms": label_terms,
                    "size": 1,
                    "source": analysis.cluster_source,
                    "label_source": analysis.cluster_label_source,
                }
            else:
                hit["_source"]["analysis"]["cluster"] = {
                    "id": None,
                    "label": "unclustered",
                    "label_terms": [],
                    "size": 0,
                    "source": analysis.cluster_source,
                    "label_source": analysis.cluster_label_source,
                }
        return hits_copy

    kmeans = KMeans(
        n_clusters=effective_k,
        random_state=random_state,
        n_init="auto",
    )
    labels = kmeans.fit_predict(matrix)

    cluster_docs: Dict[int, List[int]] = defaultdict(list)
    for row_idx, cluster_id in enumerate(labels):
        hit_idx = usable_hit_refs[row_idx]
        cluster_docs[int(cluster_id)].append(hit_idx)

    cluster_meta: Dict[int, Dict[str, Any]] = {}
    for cluster_id, hit_indices in cluster_docs.items():
        cluster_label_scores = [label_term_scores_per_hit[i] for i in hit_indices]
        label, label_terms = _build_cluster_label(
            cluster_term_scores=cluster_label_scores,
            all_term_scores=label_term_scores_per_hit,
            top_label_terms=analysis.cluster_label_top_terms,
        )
        cluster_meta[cluster_id] = {
            "id": cluster_id,
            "label": label,
            "label_terms": label_terms,
            "size": len(hit_indices),
            "source": analysis.cluster_source,
            "label_source": analysis.cluster_label_source,
        }

    for i, hit in enumerate(hits_copy):
        hit.setdefault("_source", {})
        hit["_source"].setdefault("analysis", {})

        if i not in usable_index_set:
            hit["_source"]["analysis"]["cluster"] = {
                "id": None,
                "label": "unclustered",
                "label_terms": [],
                "size": 0,
                "source": analysis.cluster_source,
                "label_source": analysis.cluster_label_source,
            }
            continue

        row_idx = hit_index_to_row[i]
        cluster_id = int(labels[row_idx])
        hit["_source"]["analysis"]["cluster"] = cluster_meta[cluster_id]

    return hits_copy

def sort_hits_by_cluster_and_score(hits):
    clusters = defaultdict(list)

    for h in hits:
        cid = h.get("_source", {}).get("analysis", {}).get("cluster", {}).get("id")
        clusters[cid].append(h)

    cluster_order = sorted(
        clusters.keys(),
        key=lambda cid: max(h.get("_score", 0) for h in clusters[cid]) if cid is not None else -1,
        reverse=True,
    )

    sorted_hits = []
    for cid in cluster_order:
        docs = clusters[cid]
        docs_sorted = sorted(docs, key=lambda h: h.get("_score", 0), reverse=True)
        sorted_hits.extend(docs_sorted)

    return sorted_hits