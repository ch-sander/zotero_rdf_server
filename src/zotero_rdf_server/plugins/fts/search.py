from typing import Any, Dict, List, Optional, Annotated
import html, json
from pydantic import BaseModel, Field
from copy import deepcopy
import csv, io, datetime, json
from io import StringIO

# --- OpenSearch client --------------------------------------------------------

from .db import make_client, resolve_config_path, get_os_config
from .helpers import plugin_logger
logger=plugin_logger()

cfg_path = resolve_config_path()
logger.debug(f"Loading config from {cfg_path}")
oscfg = get_os_config(cfg_path)
logger.debug(f"{oscfg}")
client = make_client(oscfg)
OS_META = oscfg.get("meta", {})
DEFAULT_ALIAS = OS_META.get("default_alias", "ocr")

from .endpoints import MAX_SIZE

MAX_SIZE = OS_META.get("max_size", MAX_SIZE)
logger.info(f"DEFAULT_ALIAS: {DEFAULT_ALIAS}")

# --- Helpers -----------------------------------------------------------------

DEFAULT_SIZE = 10

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
    """
    Parse comma/semicolon separated terms while respecting quoted values.
    """

    if not raw or not raw.strip():
        raise ValueError("No terms provided.")

    # Normalize semicolons to commas outside quotes.
    normalized = []
    in_quotes = False

    for char in raw:
        if char == '"':
            in_quotes = not in_quotes

        if char == ";" and not in_quotes:
            normalized.append(",")
        else:
            normalized.append(char)

    reader = csv.reader(StringIO("".join(normalized)))

    terms = [term.strip() for term in next(reader) if term.strip()]

    if not terms:
        # raise ValueError("No terms provided.")
        logger.warning(f"No terms to return from {raw}")
        return []
    
    return terms

def maybe_guard_prefix(term: str, min_len: int = 3) -> bool:
    return len(term) >= min_len

def effective_fuzzy_edits(term: str, requested_edits: int) -> int:
    if requested_edits <= 0:
        return 0
    if len(term) < 5:
        return min(requested_edits, 1)
    return min(requested_edits, 2)

def get_doc_vector(index: str, os_id: str, vector_field: str = "vector") -> List[float]:
    if not index: index = DEFAULT_ALIAS
    doc = client.get(index=index, id=os_id)
    src = doc.get("_source", {})
    vec = src.get(vector_field)
    if vec is None:
        raise KeyError(f"Document has no '{vector_field}' in _source.")
    return vec

def build_terms_should_queries_legacy(
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
    should: List[Dict[str, Any]] = [] # TODO maybe add must, too!

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

from dataclasses import dataclass

@dataclass(frozen=True)
class TermQueryConfig:
    field: str = "text"
    exact: bool = True
    truncated: bool = True
    fuzzy: bool = True
    use_shingles: bool = True
    shingle_field: Optional[str] = None
    phrase_slop: int = 2
    shingle_boost: float = 3.0
    phrase_boost: float = 6.0
    prefix_boost: float = 2.0
    fuzzy_boost: float = 1.0
    prefix_max_expansions: int = 50
    fuzzy_max_expansions: int = 50
    fuzzy_prefix_length: int = 1
    fuzzy_edits: int = 2
    min_prefix_len: int = 3

    @property
    def resolved_shingle_field(self) -> str:
        return self.shingle_field or f"{self.field}.shingles"

def build_terms_should_queries_legacy(
    terms: List[str],
    field: str = "text",
    exact: bool = True,
    truncated: bool = True,
    fuzzy: bool = True,
    use_shingles: bool = True,
    shingle_field: Optional[str] = None,
    phrase_slop: int = 2,
    shingle_boost: float = 3.0,
    phrase_boost: float = 6.0,
    prefix_boost: float = 2.0,
    fuzzy_boost: float = 1.0,
    prefix_max_expansions: int = 50,
    fuzzy_max_expansions: int = 50,
    fuzzy_prefix_length: int = 1,
    fuzzy_edits: int = 2,
    min_prefix_len: int = 3,
) -> List[Dict[str, Any]]:

    should: List[Dict[str, Any]] = []
    shingle_field = shingle_field or f"{field}.shingles"

    for t in terms:
        if exact:
            should.append({
                "match_phrase": {
                    field: {
                        "query": t,
                        "slop": phrase_slop,
                        "boost": phrase_boost,
                    }
                }
            })

        if use_shingles:
            should.append({
                "match": {
                    shingle_field: {
                        "query": t,
                        "operator": "and",
                        "boost": shingle_boost,
                    }
                }
            })

        if truncated and maybe_guard_prefix(t, min_len=min_prefix_len):
            should.append({
                "match_phrase_prefix": {
                    field: {
                        "query": t,
                        "max_expansions": prefix_max_expansions,
                        "boost": prefix_boost,
                    }
                }
            })

        if fuzzy:
            edits = effective_fuzzy_edits(t, fuzzy_edits)
            if edits > 0:
                should.append({
                    "match": {
                        field: {
                            "query": t,
                            "fuzziness": edits,
                            "prefix_length": fuzzy_prefix_length,
                            "max_expansions": fuzzy_max_expansions,
                            "operator": "and",
                            "boost": fuzzy_boost,
                        }
                    }
                })

    return should

def _build_single_term_queries(
    term: str,
    config: TermQueryConfig,
) -> List[Dict[str, Any]]:
    should: List[Dict[str, Any]] = []

    t = term.strip()
    if not t:
        return should

    if config.exact:
        should.append({
            "match_phrase": {
                config.field: {
                    "query": t,
                    "slop": config.phrase_slop,
                    "boost": config.phrase_boost,
                }
            }
        })

    if config.use_shingles:
        should.append({
            "match": {
                config.resolved_shingle_field: {
                    "query": t,
                    "operator": "and",
                    "boost": config.shingle_boost,
                }
            }
        })

    if config.truncated and maybe_guard_prefix(t, min_len=config.min_prefix_len):
        should.append({
            "match_phrase_prefix": {
                config.field: {
                    "query": t,
                    "max_expansions": config.prefix_max_expansions,
                    "boost": config.prefix_boost,
                }
            }
        })

    if config.fuzzy:
        edits = effective_fuzzy_edits(t, config.fuzzy_edits)
        if edits > 0:
            should.append({
                "match": {
                    config.field: {
                        "query": t,
                        "fuzziness": edits,
                        "prefix_length": config.fuzzy_prefix_length,
                        "max_expansions": config.fuzzy_max_expansions,
                        "operator": "and",
                        "boost": config.fuzzy_boost,
                    }
                }
            })

    return should

def build_terms_should_queries(
    terms: List[str],
    config: TermQueryConfig = TermQueryConfig(),
) -> List[Dict[str, Any]]:
    should: List[Dict[str, Any]] = []

    for term in terms:
        and_terms = [x.strip() for x in term.split("+") if x.strip()] # A+B = A AND B
        if not and_terms:
            continue

        if len(and_terms) == 1:
            should.extend(_build_single_term_queries(and_terms[0], config))
            continue

        must = []
        for subterm in and_terms:
            sub_should = _build_single_term_queries(subterm, config)
            if sub_should:
                must.append({
                    "bool": {
                        "should": sub_should,
                        "minimum_should_match": 1,
                    }
                })

        if must:
            should.append({"bool": {"must": must}})

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
    
    
    from .viewer import app


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
                doc_value = f"[{doc_id}]({app.url_path_for('view', os_doc_id=doc_id)})"
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

    from .viewer import app


    parts.append(f"<h1>{html.escape(title)}</h1>")
    parts.append(f"<p>Total documents shown: <strong>{len(rows)}</strong></p>")

    for idx, row in enumerate(rows, start=1):
        doc_id = str(row.get("_id") or "").strip()
        label = str(row.get("label") or "").strip()

        parts.append("<hr>")

        if doc_id or label:
            d_text = f"{label} ({doc_id})" if label and doc_id else (label or doc_id)
            doc_value = (
                f'<a href="{app.url_path_for("view", os_doc_id=doc_id)}" target="_blank">{d_text}</a>'
                if verbose and doc_id else d_text
            )
            parts.append(f"<h2>Document {idx} ({doc_value})</h2>")
        else:
            parts.append(f"<h2>Document {idx}</h2>")

        analysis_fields = []
        normal_fields = []

        for col in columns:
            if col not in row or col == "_id":
                continue

            if str(col).startswith("analysis_") or str(col).startswith("analysis."):
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
    if not index: 
        index = DEFAULT_ALIAS
        logger.warning(f"index set to default: {index}")
    logger.info("Submitting search...")
    logger.info(json.dumps(body,indent=4))
    return client.search(index=index, body=body)

def build_scroll_body(
    body: Dict[str, Any],
    *,
    batch_size: int = 1000,
    drop_sort: bool = True,
) -> Dict[str, Any]:
    scroll_body = deepcopy(body)

    scroll_body.pop("from", None)

    scroll_body["size"] = batch_size

    if drop_sort:
        scroll_body.pop("sort", None)

    return scroll_body

def os_search_all_scroll(
    index: str,
    body: Dict[str, Any],
    columns: Optional[str],
    batch_size: int = 1000,
    scroll_ttl: str = "2m",
) -> Dict[str, Any]:
    """Fetch all hits via OpenSearch scroll and return one combined response."""
    scroll_body = build_scroll_body(body, batch_size=batch_size, drop_sort=True)

    apply_source_includes(scroll_body, columns)

    if not index:
        index = DEFAULT_ALIAS
        logger.warning(f"index set to default: {index}")

    search_body = dict(scroll_body)
    search_body["size"] = batch_size

    logger.info("Initial scroll search:\n%s", json.dumps(search_body, indent=4))

    resp = client.search(
        index=index,
        body=search_body,
        scroll=scroll_ttl,
    )

    all_hits: List[Dict[str, Any]] = []
    scroll_id = resp.get("_scroll_id")
    batch = 0
    try:
        while True:
            batch += 1
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break

            all_hits.extend(hits)

            if not scroll_id:
                break

            resp = client.scroll(
                body={
                    "scroll": scroll_ttl,
                    "scroll_id": scroll_id,
                }
            )
            logger.info("Following scroll search:%s", batch)

            # Always keep the latest scroll_id
            scroll_id = resp.get("_scroll_id", scroll_id)

    finally:
        if scroll_id:
            try:
                client.clear_scroll(body={"scroll_id": [scroll_id]})
                logger.info("Cleared scroll search:\n%s", scroll_id)
            except Exception as clear_err:
                logger.warning("Failed to clear scroll: %s", clear_err)

    return {
        "hits": {
            "total": {"value": len(all_hits), "relation": "eq"},
            "hits": all_hits,
        }
    }

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
from .endpoints import IngestTsRangeFilter

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

# region NLP
from collections import Counter, defaultdict

def enrich_hits_with_analysis(
    hits,
    *,
    index,
    analysis,
    field,
    return_analysis,
    batch_size=200,
    mlt_client=client,
    sort:bool=True
):
    from .analysis.ml import analysis_from_mtermvectors, cluster_hits_by_analysis

    if not analysis.perform_analysis:
        return hits

    analysis_field = analysis.analyze_field or field
    hit_ids = [h["_id"] for h in hits if h.get("_id")]
    if not hit_ids:
        return hits

    tv_docs = []
    for i in range(0, len(hit_ids), batch_size):
        batch_ids = hit_ids[i:i + batch_size]
        logger.info("Getting term vectors for batch %s", i)
        tv_resp = os_mtermvectors(
            index=index,
            doc_ids=batch_ids,
            field=analysis_field,
        )
        tv_docs.extend(tv_resp.get("docs", []))

    hits, cluster_vectors_by_id = analysis_from_mtermvectors(
        hits=hits,
        tv_resp={"docs": tv_docs},
        analysis=analysis,
        field_fallback=field,
    )
    logger.info("Got all analyses!")

    should_cluster_or_compute_neighbors = (
        bool(getattr(analysis, "cluster_enabled", False))
        or bool(getattr(analysis, "neighbors_enabled", False))
    )

    if should_cluster_or_compute_neighbors:
        hits = cluster_hits_by_analysis(
            hits,
            analysis=analysis,
            return_projection=return_analysis,
            return_neighbors=bool(getattr(analysis, "neighbors_enabled", False)),
            os_client=mlt_client,
            cluster_vectors_by_id=cluster_vectors_by_id,
        )

        if getattr(analysis, "cluster_enabled", False) and sort:
            hits = sort_hits_by_cluster_and_score(hits)
            logger.info("Got all clusters!")

        if getattr(analysis, "neighbors_enabled", False):
            logger.info("Got all neighbors!")

    return hits

def sort_hits_by_cluster_and_score(hits):
    clusters = defaultdict(list)

    for h in hits:
        cid = h.get("_source", {}).get("analysis", {}).get("cluster", {}).get("id")
        clusters[cid].append(h)

    cluster_order = sorted(
        clusters.keys(),
        key=lambda cid: max((h.get("_score") or 0) for h in clusters[cid]) if cid is not None else -1,
        reverse=True,
    )

    sorted_hits = []
    for cid in cluster_order:
        docs = clusters[cid]
        docs_sorted = sorted(docs, key=lambda h: h.get("_score") or 0, reverse=True)
        sorted_hits.extend(docs_sorted)

    return sorted_hits

def add_analysis_columns(
    row: Dict[str, Any],
    *,
    analysis_key: str = "analysis",
    d_prefix: str = "vector_",
    p_prefix: str = "projection_",
    p_cluster: str = "cluster_",
    p_neighbors: str = "neighbors",
) -> Dict[str, Any]:
    out = dict(row)

    analysis = row.get(analysis_key) or {}

    # --- CLUSTER ---
    cluster = analysis.get("cluster") or {}

    if isinstance(cluster, dict):
        for k, v in cluster.items():
            if k in {"vector", "source"}:
                continue
            out[f"{p_cluster}{k}"] = v


    # --- GLOBAL KEY TERMS ---
    global_terms = (analysis.get("index_documents") or {}).get("key_terms")
    if isinstance(global_terms, list):
        out["global_key_terms"] = global_terms

    # --- LOCAL KEY TERMS ---
    local_terms = (analysis.get("hits_documents") or {}).get("key_terms")
    if isinstance(local_terms, list):
        out["local_key_terms"] = local_terms

    # --- VECTOR ---
    vector = cluster.get("vector")

    if isinstance(vector, dict):
        for k, v in vector.items():
            out[f"{d_prefix}{k}"] = v
    elif isinstance(vector, list):
        for i, v in enumerate(vector):
            out[f"{d_prefix}{i}"] = v

    # --- CLUSTER TERMS ---
    # cluster_terms = cluster.get("label_terms")
    # if isinstance(cluster_terms, list) and all(isinstance(x, str) for x in cluster_terms):
    #     for i, v in enumerate(cluster_terms):
    #         out[f"{p_cluster}term_{i}"] = v
    # --- PROJECTION ---
    projection = analysis.get("projection")

    if isinstance(projection, dict):
        for k, v in projection.items():
            if k in {"method__"}:
                continue
            out[f"{p_prefix}{k}"] = v

    # --- NEIGHBORS ---
    neighbors = analysis.get("neighbors")
    if isinstance(neighbors, dict):
        out[p_neighbors] = neighbors

    out.pop(analysis_key, None)
    return out

# endregion

# end