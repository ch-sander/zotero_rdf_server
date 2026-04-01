from typing import Any, Dict, List, Optional
import html

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

def flatten_meta_fields(row: Dict[str, Any], meta_key: str = "meta", prefix: str = "meta_") -> Dict[str, Any]:
    """
    Flatten row['meta'] dict into top-level keys like meta_<path>.
    Example: meta = {"a": {"b": 1}, "x": "y"} -> meta_a_b=1, meta_x="y"
    Keeps original 'meta' field (optional); you can drop it if you prefer.
    """
    meta = row.get(meta_key)
    if not isinstance(meta, dict):
        return row

    def walk(d: Dict[str, Any], path: List[str], out: Dict[str, Any]) -> None:
        for k, v in d.items():
            new_path = path + [str(k)]
            if isinstance(v, dict):
                walk(v, new_path, out)
            else:
                out[prefix + "_".join(new_path)] = v

    out = dict(row)
    walk(meta, [], out)
    return out

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

def normalize_hits(
    resp: Dict[str, Any],
    flatten_meta: bool = True,
    keep_meta: bool = False,
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

        if flatten_meta:
            row = flatten_meta_fields(row)
            if not keep_meta:
                row.pop("meta", None)

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

        for col in columns:
            if col not in row:
                continue

            raw_value = row.get(col)
            value = flatten_value(raw_value)
            if value == "" or col == "_id":
                continue

            # if col == "_id" and verbose:
            #     safe_id = html.escape(doc_id)
            #     value_html = f'<a href="{BASE_URL}/{safe_id}" target="_blank">{safe_id}</a>'

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

def apply_source_includes(body: Dict[str, Any], columns: Optional[str]) -> None:
    """Mutate body to include _source filtering based on columns."""
    if not columns:
        return
    
    requested = [c.strip() for c in columns.split(",") if c.strip()]
    # _id/_score are not in _source
    includes = [c for c in requested if c not in ("_id", "_score")]

    # meta_* columns are derived from meta.* (flatten step)
    if any(c.startswith("meta_") for c in requested) and "meta.*" not in includes:
        includes.append("meta.*")

    body["_source"] = {"includes": includes}

def os_search(index: str, body: Dict[str, Any], columns: Optional[str]) -> Dict[str, Any]:
    """Central OpenSearch search that applies _source includes if columns is set."""    
    apply_source_includes(body, columns)
    if not index: index = DEFAULT_ALIAS
    return client.search(index=index, body=body)

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