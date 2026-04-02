import json
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional


def write_analysis_search_explorer_html(
    output_path: str,
    *,
    openapi_spec: Dict[str, Any],
    endpoint_path: str = "/plugin/fts/search/terms",
    api_base_url: str = "",
    initial_hits: Optional[List[Dict[str, Any]]] = None,
    title: str = "Analysis Search Explorer",
    max_term_badges_per_section: int = 20,
    max_source_fields_preview: int = 12,
    preferred_title_fields: Optional[List[str]] = None,
    preferred_snippet_fields: Optional[List[str]] = None,
    include_global_terms: bool = True,
    include_local_terms: bool = True,
    include_cluster_info: bool = True,
    page_size: int = 25,
) -> str:
    """
    Writes a self-contained HTML explorer with:
      - API search form derived from OpenAPI query parameters
      - client-side rendering of hits
      - client-side filtering
      - client-side pagination
      - preserved cluster-oriented UI

    Notes:
      - Pagination is applied AFTER filtering.
      - Sidebar and summary are calculated from all filtered hits.
      - Main content only renders the current page.
    """

    ui = {
        "lang": "en",
        "subtitle": "Interactive search, clustering, and analysis explorer.",
        "search": "Search",
        "searching": "Searching...",
        "reset_form": "Reset form",
        "reset_filters": "Reset filters",
        "toggle_all_details": "Toggle all details",
        "documents": "Documents",
        "clusters": "Clusters",
        "unclustered": "Unclustered",
        "cluster_terms_count": "Cluster terms",
        "top_cluster_terms": "Top Cluster Terms",
        "all_clusters": "All clusters",
        "search_placeholder": "Search title, snippet, ID, terms ...",
        "no_content": "No content",
        "no_clusters": "No clusters",
        "no_snippet": "No snippet",
        "no_details": "No details",
        "no_fields": "No fields",
        "cluster_terms": "Cluster terms",
        "local_terms": "Local terms",
        "global_terms": "Global terms",
        "analysis_details": "Analysis details",
        "cluster_info": "Cluster",
        "local_analysis": "Local analysis",
        "global_analysis": "Global analysis",
        "source_preview": "Source preview",
        "field": "field",
        "value": "value",
        "term": "term",
        "score": "score",
        "doc_freq": "doc_freq",
        "term_freq": "term_freq",
        "id": "ID",
        "cluster": "Cluster",
        "size": "size",
        "source": "source",
        "label_source": "label_source",
        "docs": "docs",
        "documents_visible": "documents visible",
        "clusters_visible": "clusters visible",
        "active_term": "active term",
        "api_search": "API Search",
        "search_options": "Search options",
        "analysis_options": "Analysis / cluster options",
        "request_url": "Request URL",
        "error": "Error",
        "status_ready": "Ready",
        "status_loaded_initial_hits": "Loaded initial hits",
        "status_no_initial_hits": "No initial hits loaded",
        "status_fetch_ok": "Search completed",
        "status_fetch_failed": "Search failed",
        "show_raw_response": "Show raw response",
        "hide_raw_response": "Hide raw response",
        "required": "required",
        "advanced": "Advanced",
        "submit_hint": "Searches automatically enable analysis and clustering.",
        "generated_without_dependencies": "Generated without external frontend dependencies.",
        "page": "Page",
        "prev": "Previous",
        "next": "Next",
        "of": "of",
        "page_size": "Page size",
        "showing_results": "Showing results",
    }

    preferred_title_fields = preferred_title_fields or [
        "title", "name", "label", "headline", "subject", "_id"
    ]
    preferred_snippet_fields = preferred_snippet_fields or [
        "snippet", "summary", "description", "content", "text", "body"
    ]

    def safe_str(value: Any) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    def resolve_endpoint_spec() -> Dict[str, Any]:
        paths = openapi_spec.get("paths", {}) if isinstance(openapi_spec, dict) else {}
        endpoint = paths.get(endpoint_path, {})
        spec = endpoint.get("get", {})
        if not isinstance(spec, dict):
            raise ValueError(f"Endpoint spec for {endpoint_path!r} not found in openapi_spec")
        return spec

    def extract_parameters(endpoint_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [p for p in endpoint_spec.get("parameters", []) if isinstance(p, dict)]

    def schema(param: Dict[str, Any]) -> Dict[str, Any]:
        return param.get("schema", {}) or {}

    def schema_default(param: Dict[str, Any], fallback: Any = "") -> Any:
        s = schema(param)
        return s["default"] if "default" in s else fallback

    def schema_type(param: Dict[str, Any]) -> str:
        s = schema(param)
        if "enum" in s:
            return "enum"
        if "anyOf" in s:
            for item in s["anyOf"]:
                if isinstance(item, dict) and item.get("type") and item.get("type") != "null":
                    return str(item["type"])
            return "string"
        return str(s.get("type", "string"))

    def schema_enum(param: Dict[str, Any]) -> Optional[List[str]]:
        s = schema(param)
        if "enum" in s and isinstance(s["enum"], list):
            return [str(v) for v in s["enum"]]
        return None

    def schema_description(param: Dict[str, Any]) -> str:
        return safe_str(param.get("description") or schema(param).get("description") or "")

    def render_label(text: str, required: bool = False) -> str:
        req = (
            f" <span class='required-pill'>{escape(ui['required'])}</span>"
            if required else ""
        )
        return f"{escape(text)}{req}"

    def group_parameter(name: str) -> str:
        if name == "q" or name in {
            "index", "field", "size", "offset", "exact", "truncated", "fuzzy", "lucene", "highlight"
        }:
            return "search"
        if name == "perform_analysis" or name.startswith("analyze_") or name.startswith("cluster_") or name == "analysis_mode":
            return "analysis"
        return "advanced"

    def render_param_control(param: Dict[str, Any]) -> str:
        name = safe_str(param.get("name"))
        if not name:
            return ""

        label = name.replace("_", " ")
        description = schema_description(param)
        required = bool(param.get("required", False))
        typ = schema_type(param)
        enum_values = schema_enum(param)
        default = schema_default(param, "")
        value_attr = escape(safe_str(default), quote=True)
        element_id = f"param-{escape(name, quote=True)}"
        data_attr = escape(name, quote=True)

        if enum_values:
            options = ["<option value=''></option>"]
            for item in enum_values:
                selected = " selected" if safe_str(default) == item else ""
                options.append(
                    f"<option value='{escape(item, quote=True)}'{selected}>{escape(item)}</option>"
                )
            return (
                "<div class='form-field'>"
                f"<label for='{element_id}'>{render_label(label, required)}</label>"
                f"<select id='{element_id}' data-param='{data_attr}' class='input'>"
                + "".join(options) +
                "</select>"
                f"<div class='hint'>{escape(description)}</div>"
                "</div>"
            )

        if typ == "boolean":
            checked = " checked" if bool(default) else ""
            return (
                "<div class='form-field checkbox-field'>"
                "<label class='checkbox-wrap'>"
                f"<input type='checkbox' id='{element_id}' data-param='{data_attr}'{checked}>"
                f"<span>{render_label(label, required)}</span>"
                "</label>"
                f"<div class='hint'>{escape(description)}</div>"
                "</div>"
            )

        input_type = "number" if typ == "integer" else "text"
        s = schema(param)
        min_attr = f" min='{escape(safe_str(s['minimum']), quote=True)}'" if s.get("minimum") is not None else ""
        max_attr = f" max='{escape(safe_str(s['maximum']), quote=True)}'" if s.get("maximum") is not None else ""
        placeholder = escape(description[:120], quote=True) if description else ""

        return (
            "<div class='form-field'>"
            f"<label for='{element_id}'>{render_label(label, required)}</label>"
            f"<input id='{element_id}' data-param='{data_attr}' class='input' "
            f"type='{input_type}' value='{value_attr}' placeholder='{placeholder}'{min_attr}{max_attr}>"
            f"<div class='hint'>{escape(description)}</div>"
            "</div>"
        )

    def render_form_section(title_text: str, params: List[Dict[str, Any]], *, collapsible: bool = False, open_by_default: bool = False) -> str:
        if not params:
            return ""
        content = "".join(render_param_control(p) for p in params)
        if not collapsible:
            return (
                f"<h3>{escape(title_text)}</h3>"
                "<div class='form-grid'>"
                f"{content}"
                "</div>"
            )
        open_attr = " open" if open_by_default else ""
        return (
            f"<details style='margin-top:16px;'{open_attr}>"
            f"<summary>{escape(title_text)}</summary>"
            "<div class='form-grid' style='margin-top:12px;'>"
            f"{content}"
            "</div>"
            "</details>"
        )

    endpoint_spec = resolve_endpoint_spec()
    parameters = extract_parameters(endpoint_spec)

    grouped_params = {"search": [], "analysis": [], "advanced": []}
    for p in parameters:
        name = safe_str(p.get("name"))
        if name:
            grouped_params[group_parameter(name)].append(p)

    main_form_html = render_form_section(ui["search_options"], grouped_params["search"])
    analysis_form_html = render_form_section(
        ui["analysis_options"], grouped_params["analysis"], collapsible=True, open_by_default=True
    )
    advanced_form_html = render_form_section(
        ui["advanced"], grouped_params["advanced"], collapsible=True, open_by_default=False
    )

    explorer_config = {
        "endpointPath": endpoint_path,
        "apiBaseUrl": api_base_url,
        "maxTermBadgesPerSection": max_term_badges_per_section,
        "maxSourceFieldsPreview": max_source_fields_preview,
        "includeGlobalTerms": include_global_terms,
        "includeLocalTerms": include_local_terms,
        "includeClusterInfo": include_cluster_info,
        "pageSize": page_size,
    }

    config_js = "\n".join([
        f"const EXPLORER_CONFIG = {json.dumps(explorer_config, ensure_ascii=False)};",
        f"const ENDPOINT_PARAMETERS = {json.dumps(parameters, ensure_ascii=False)};",
        f"const INITIAL_HITS = {json.dumps(initial_hits or [], ensure_ascii=False)};",
        f"const PREFERRED_TITLE_FIELDS = {json.dumps(preferred_title_fields, ensure_ascii=False)};",
        f"const PREFERRED_SNIPPET_FIELDS = {json.dumps(preferred_snippet_fields, ensure_ascii=False)};",
        f"const UI_TEXT = {json.dumps(ui, ensure_ascii=False)};",
    ])

    css = r"""
:root {
  --bg: #0b1020;
  --panel: #121933;
  --panel-2: #182140;
  --panel-3: #101936;
  --text: #e8ecf8;
  --muted: #9aa7c7;
  --line: #2a3561;
  --accent: #7aa2ff;
  --accent-2: #8ef0c8;
  --danger: #ff9b9b;
  --warning: #ffd98f;
  --badge: #24345f;
  --badge-hover: #30457f;
  --shadow: 0 8px 28px rgba(0,0,0,0.28);
  --radius: 14px;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
}

body { min-height: 100vh; }
a { color: inherit; text-decoration: none; }
button { font: inherit; }

.layout {
  display: grid;
  grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);
  max-width: 1600px;
  margin: 0 auto;
  min-height: 100vh;
}

.sidebar {
  border-right: 1px solid var(--line);
  background: linear-gradient(180deg, #0f1730 0%, #0d1430 100%);
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: auto;
  padding: 18px;
}

.main {
  padding: 20px;
  min-width: 0;
}

.panel,
.header-card,
.search-panel,
.cluster-section,
.doc-card {
  min-width: 0;
}

.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

.header-card { padding: 18px; margin-bottom: 18px; }
.header-card h1 { margin: 0 0 10px 0; font-size: 24px; }

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}

.summary-item {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px;
}

.summary-item .label {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 6px;
}

.summary-item .value {
  font-size: 20px;
  font-weight: 700;
}

.controls {
  display: grid;
  grid-template-columns: 1.5fr 1fr auto auto;
  gap: 10px;
  margin-top: 16px;
}

.input, .select, textarea.input {
  width: 100%;
  padding: 11px 12px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: #0d1530;
  color: var(--text);
}

.input:focus, .select:focus, textarea.input:focus {
  outline: 2px solid rgba(122, 162, 255, 0.25);
  border-color: var(--accent);
}

.btn {
  padding: 11px 14px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--text);
  cursor: pointer;
}

.btn:hover { background: #22315e; }

.btn-primary {
  background: var(--accent);
  color: #09101f;
  border-color: var(--accent);
  font-weight: 700;
}

.btn-primary:hover { filter: brightness(1.03); }
.btn-secondary { background: var(--panel-2); }

.muted { color: var(--muted); }

.status-line {
  margin-top: 10px;
  font-size: 13px;
  color: var(--muted);
}

.status-line.error { color: var(--danger); }
.status-line.warning { color: var(--warning); }

.sidebar-title {
  font-size: 18px;
  font-weight: 700;
  margin-bottom: 10px;
}

.sidebar-section { margin-bottom: 18px; }

.sidebar-cluster-link {
  display: block;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-radius: 12px;
  background: rgba(255,255,255,0.03);
  border: 1px solid transparent;
}

.sidebar-cluster-link:hover {
  border-color: var(--line);
  background: rgba(255,255,255,0.06);
}

.sidebar-cluster-title {
  display: block;
  font-weight: 600;
  margin-bottom: 4px;
}

.sidebar-cluster-meta {
  display: block;
  color: var(--muted);
  font-size: 12px;
}

.section-title {
  font-size: 13px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 10px;
}

.term-cloud,
.cluster-terms-wrap,
.doc-chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.term-badge {
  border: 1px solid var(--line);
  background: var(--badge);
  color: var(--text);
  border-radius: 999px;
  padding: 6px 10px;
  cursor: pointer;
  font-size: 12px;
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease, transform 120ms ease;
}

.term-badge:hover {
  background: var(--badge-hover);
  transform: translateY(-1px);
}

.term-badge.active-term {
  background: var(--accent);
  color: #081120;
  border-color: var(--accent);
  font-weight: 700;
}

.cluster-list-info {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 12px;
}

.search-panel {
  padding: 18px;
  margin-bottom: 18px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}

.form-field {
  display: grid;
  gap: 6px;
}

.form-field > label {
  font-size: 13px;
  font-weight: 600;
}

.hint {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
}

.checkbox-field {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel-3);
}

.checkbox-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}

.required-pill {
  display: inline-block;
  margin-left: 6px;
  font-size: 10px;
  color: #081120;
  background: var(--accent-2);
  border-radius: 999px;
  padding: 2px 7px;
  vertical-align: middle;
}

.form-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}

.cluster-section {
  margin-bottom: 22px;
  padding: 18px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

.cluster-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 10px;
}

.cluster-header h2 {
  margin: 0;
  font-size: 20px;
}

.cluster-meta {
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}

.cluster-docs {
  display: grid;
  gap: 14px;
}

.doc-card {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 14px;
}

.doc-card-header { margin-bottom: 10px; }

.doc-card-title {
  font-size: 17px;
  font-weight: 700;
  margin-bottom: 8px;
  word-break: break-word;
}

.doc-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  color: var(--muted);
  font-size: 12px;
}

.doc-snippet {
  color: #d7def5;
  line-height: 1.45;
  margin-bottom: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.chip-label {
  font-size: 12px;
  color: var(--muted);
  min-width: 96px;
}

.details-block {
  margin-top: 12px;
  border-top: 1px solid var(--line);
  padding-top: 12px;
}

.details-block summary {
  cursor: pointer;
  color: var(--accent-2);
  margin-bottom: 10px;
}

.details-section { margin-top: 12px; }

.details-section h4 {
  margin: 0 0 8px 0;
  font-size: 14px;
}

.kv-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.kv-grid > div {
  background: #101936;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px;
}

.mini-table {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 12px;
}

.mini-table th,
.mini-table td {
  border-bottom: 1px solid var(--line);
  text-align: left;
  padding: 8px 10px;
  vertical-align: top;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.mini-table th {
  color: var(--muted);
  font-weight: 600;
}

.hidden { display: none !important; }

.result-info {
  color: var(--muted);
  margin: 8px 0 20px 0;
}

.footer-note {
  color: var(--muted);
  font-size: 12px;
  margin-top: 22px;
}

.raw-response-wrap { margin-top: 14px; }

.raw-response {
  display: none;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--panel-3);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px;
  max-height: 360px;
  overflow: auto;
  font-size: 12px;
}

.raw-response.visible { display: block; }

.note-box {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel-3);
  color: var(--muted);
  font-size: 13px;
}

.pagination-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 18px;
  padding: 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: var(--shadow);
}

.pagination-info {
  color: var(--muted);
  font-size: 13px;
}

.pagination-spacer {
  flex: 1 1 auto;
}

.page-indicator {
  min-width: 110px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}

.search-panel details > summary {
  cursor: pointer;
  font-weight: 700;
  font-size: 18px;
  margin-bottom: 12px;
}

.search-panel details[open] > summary {
  margin-bottom: 16px;
}

@media (max-width: 1200px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .sidebar {
    position: relative;
    height: auto;
    border-right: none;
    border-bottom: 1px solid var(--line);
  }

  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .controls {
    grid-template-columns: 1fr;
  }

  .kv-grid {
    grid-template-columns: 1fr;
  }

  .doc-snippet mark.search-hit,
.doc-snippet mark.search-hit.strong-hit {
  background: rgba(122, 162, 255, 0.28);
  color: #f3f6ff;
  padding: 0 0.18em;
  border-radius: 0.28em;
  font-weight: 700;
}
.snippet-list li {
  margin-bottom: 4px;
}

.request-url-block {
  margin-top: 8px;
  padding: 10px 12px;
  background: var(--panel-3);
  border: 1px solid var(--line);
  border-radius: 12px;
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

#aggregationsContainer .cluster-section {
  margin-top: 18px;
}
.mini-table td,
.mini-table th {
  padding: 8px 10px;
}
.mini-table tbody tr:nth-child(even) {
  background: rgba(255,255,255,0.03);
}


}
"""

    js = r"""
(function () {
  const contentEl = document.getElementById("content");
  const resultInfoEl = document.getElementById("resultInfo");
  const sidebarClustersEl = document.getElementById("sidebarClusters");
  const sidebarClusterInfoEl = document.getElementById("sidebarClusterInfo");
  const topClusterTermsEl = document.getElementById("topClusterTerms");

  const clientSearchInput = document.getElementById("clientSearchInput");
  const clientClusterSelect = document.getElementById("clientClusterSelect");
  const resetClientFiltersBtn = document.getElementById("resetClientFiltersBtn");
  const expandAllBtn = document.getElementById("expandAllBtn");

  const apiSearchForm = document.getElementById("apiSearchForm");
  const submitApiSearchBtn = document.getElementById("submitApiSearchBtn");
  const resetApiFormBtn = document.getElementById("resetApiFormBtn");
  const apiStatusEl = document.getElementById("apiStatus");
  const requestUrlBoxEl = document.getElementById("requestUrlBox");
  const toggleRawResponseBtn = document.getElementById("toggleRawResponseBtn");
  const rawResponseEl = document.getElementById("rawResponse");

  const summaryDocumentsEl = document.getElementById("summaryDocuments");
  const summaryClustersEl = document.getElementById("summaryClusters");
  const summaryUnclusteredEl = document.getElementById("summaryUnclustered");
  const summaryClusterTermsEl = document.getElementById("summaryClusterTerms");

  let allPreparedHits = [];
  let activeTerm = "";
  let rawResponseVisible = false;
  let currentPage = 1;
  let pageSize = Number(EXPLORER_CONFIG.pageSize || 25);

  function normalize(value) {
    return (value || "").toString().toLowerCase().trim();
  }

  function safeStr(value) {
    if (value === null || value === undefined) return "";
    return String(value);
  }

  function escapeHtml(value) {
    return safeStr(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtScore(value) {
    const n = Number(value || 0);
    return Number.isFinite(n) ? n.toFixed(4) : "0.0000";
  }

  function jsonLike(value, depth = 0) {
    if (depth > 2) return "...";

    if (Array.isArray(value)) {
      const parts = value.slice(0, 10).map(v => jsonLike(v, depth + 1));
      if (value.length > 10) parts.push("...");
      return "[ " + parts.join(", ") + " ]";
    }

    if (value && typeof value === "object") {
      const entries = Object.entries(value)
        .slice(0, 20)
        .map(([k, v]) => `${k}: ${jsonLike(v, depth + 1)}`);
      if (Object.keys(value).length > 20) entries.push("...");
      return "{ " + entries.join(", ") + " }";
    }

    return safeStr(value);
  }

  function getSource(hit) {
    if (!hit || typeof hit !== "object") return {};
    if (hit._source && typeof hit._source === "object") return hit._source;
    return hit;
  }

  function getAnalysis(hit) {
    const source = getSource(hit);
    return source.analysis && typeof source.analysis === "object" ? source.analysis : {};
  }

  function getCluster(hit) {
    const analysis = getAnalysis(hit);
    return analysis.cluster && typeof analysis.cluster === "object" ? analysis.cluster : {};
  }

  function getLocal(hit) {
    const analysis = getAnalysis(hit);
    return analysis.local && typeof analysis.local === "object" ? analysis.local : {};
  }

  function getGlobal(hit) {
    const analysis = getAnalysis(hit);
    return analysis.global && typeof analysis.global === "object" ? analysis.global : {};
  }

  function pickTitle(hit) {
    const src = getSource(hit);
    for (const field of PREFERRED_TITLE_FIELDS) {
      if (field === "_id") break;
      const value = src[field];
      if (value !== null && value !== undefined && value !== "" && (!Array.isArray(value) || value.length > 0)) {
        if (Array.isArray(value)) return value.slice(0, 3).map(safeStr).join(" | ");
        return safeStr(value);
      }
    }
    return safeStr(hit && hit._id ? hit._id : "untitled");
  }

function normalizeSnippetText(text) {
  return safeStr(text)
    .replace(/ſ/g, "s")
    .replace(/æ/g, "ae")
    .replace(/œ/g, "oe")
    .replace(/\s*¬\s*/g, "")
    .replace(/-\s*\r?\n\s*/g, "")
    .replace(/\s*\r?\n+\s*/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function pickSnippet(hit) {
  const highlight = hit && hit.highlight && typeof hit.highlight === "object" ? hit.highlight : null;

  if (highlight) {
    const firstKey = Object.keys(highlight)[0];
    if (firstKey && Array.isArray(highlight[firstKey]) && highlight[firstKey].length) {
      return {
        fragments: highlight[firstKey].map(t => normalizeSnippetText(t)),
        isHighlighted: true,
      };
    }
  }

  const src = getSource(hit);
  for (const field of PREFERRED_SNIPPET_FIELDS) {
    const value = src[field];
    if (value) {
      const text = Array.isArray(value)
        ? value.slice(0, 5).map(safeStr).join(" ")
        : safeStr(value);

      return {
        fragments: [normalizeSnippetText(text)],
        isHighlighted: false,
      };
    }
  }

  return {
    fragments: [],
    isHighlighted: false,
  };
}

  function extractKeyTerms(section) {
    const terms = section && Array.isArray(section.key_terms) ? section.key_terms : [];
    return terms.map(safeStr).map(s => s.trim()).filter(Boolean);
  }

  function extractKeyTermDetails(section) {
    const details = section && Array.isArray(section.key_terms_details) ? section.key_terms_details : [];
    return details.filter(item => item && typeof item === "object" && item.term);
  }

  function extractClusterLabelTerms(cluster) {
    const terms = cluster && Array.isArray(cluster.label_terms) ? cluster.label_terms : [];
    return terms.map(safeStr).map(s => s.trim()).filter(Boolean);
  }

  function uniqueTerms(items) {
    const out = [];
    const seen = new Set();

    for (const item of items) {
      const s = safeStr(item).trim();
      if (!s || seen.has(s)) continue;
      seen.add(s);
      out.push(s);
    }

    return out;
  }

  function renderTermBadges(terms, cssClass = "term-badge", limit = EXPLORER_CONFIG.maxTermBadgesPerSection) {
    const unique = uniqueTerms(terms).slice(0, limit);
    if (!unique.length) return "<span class='muted'>–</span>";

    return unique.map(term =>
      `<button class="${cssClass}" data-term="${escapeHtml(term)}" type="button">${escapeHtml(term)}</button>`
    ).join("");
  }

  function renderTermDetails(details) {
    if (!details.length) {
      return `<div class="muted">${escapeHtml(UI_TEXT.no_details)}</div>`;
    }

    const rows = details.slice(0, EXPLORER_CONFIG.maxTermBadgesPerSection).map(item => (
      "<tr>" +
        `<td>${escapeHtml(item.term)}</td>` +
        `<td>${escapeHtml(fmtScore(item.score))}</td>` +
        `<td>${escapeHtml(safeStr(item.doc_freq ?? ""))}</td>` +
        `<td>${escapeHtml(safeStr(item.term_freq ?? ""))}</td>` +
      "</tr>"
    )).join("");

    return (
      `<table class="mini-table">` +
      `<thead><tr>` +
      `<th>${escapeHtml(UI_TEXT.term)}</th>` +
      `<th>${escapeHtml(UI_TEXT.score)}</th>` +
      `<th>${escapeHtml(UI_TEXT.doc_freq)}</th>` +
      `<th>${escapeHtml(UI_TEXT.term_freq)}</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table>`
    );
  }

  function renderSourcePreview(src) {
    const keys = Object.keys(src || {}).filter(k => k !== "analysis");
    if (!keys.length) {
      return `<div class="muted">${escapeHtml(UI_TEXT.no_fields)}</div>`;
    }

    const rows = keys.slice(0, EXPLORER_CONFIG.maxSourceFieldsPreview).map(key => (
      "<tr>" +
        `<td>${escapeHtml(key)}</td>` +
        `<td>${escapeHtml(jsonLike(src[key]))}</td>` +
      "</tr>"
    )).join("");

    return (
      `<table class="mini-table">` +
      `<thead><tr><th>${escapeHtml(UI_TEXT.field)}</th><th>${escapeHtml(UI_TEXT.value)}</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table>`
    );
  }

  function normalizeHitsFromResponse(data) {
    if (!data) return [];
    if (Array.isArray(data)) return data;
    if (Array.isArray(data.hits)) return data.hits;
    if (data.hits && Array.isArray(data.hits.hits)) return data.hits.hits;
    if (Array.isArray(data.items)) return data.items;
    return [];
  }

function stripHtmlTags(text) {
  const div = document.createElement("div");
  div.innerHTML = safeStr(text);
  return (div.textContent || div.innerText || "").trim();
}

function snippetToSearchText(snippet) {
  if (!snippet || !Array.isArray(snippet.fragments)) return "";
  return snippet.fragments
    .map(fragment => stripHtmlTags(fragment))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

  function prepareHits(hits) {
    return (hits || []).map((hit, idx) => {
      const cluster = getCluster(hit);
      const local = getLocal(hit);
      const global = getGlobal(hit);

      const hitId = safeStr(hit && hit._id ? hit._id : `hit-${idx}`);
      const score = hit && hit._score !== undefined ? hit._score : 0;
      const title = pickTitle(hit);
      const snippet = pickSnippet(hit);

      const clusterId = cluster.id === null || cluster.id === undefined ? "unclustered" : safeStr(cluster.id);
      const clusterLabel = safeStr(cluster.label || UI_TEXT.unclustered) || UI_TEXT.unclustered;
      const clusterLabelTerms = extractClusterLabelTerms(cluster);
      const localTerms = extractKeyTerms(local);
      const globalTerms = extractKeyTerms(global);
      const searchableTerms = uniqueTerms([...clusterLabelTerms, ...localTerms, ...globalTerms]);

    const snippetSearchText = snippetToSearchText(snippet);

    return {
    raw: hit,
    idx,
    id: hitId,
    score,
    title,
    snippet,
    source: getSource(hit),
    cluster,
    local,
    global,
    clusterId,
    clusterLabel,
    clusterLabelTerms,
    localTerms,
    globalTerms,
    searchBlob: `${hitId} ${title} ${snippetSearchText} ${clusterLabel} ${searchableTerms.join(" ")}`
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim(),
    };
    });
  }

  function groupPreparedHits(preparedHits) {
    const clusters = new Map();

    for (const item of preparedHits) {
      if (!clusters.has(item.clusterId)) {
        clusters.set(item.clusterId, {
          id: item.clusterId,
          label: item.clusterLabel,
          labelTerms: item.clusterLabelTerms.slice(),
          docs: [],
        });
      }
      clusters.get(item.clusterId).docs.push(item);
    }

    for (const cluster of clusters.values()) {
      cluster.docs.sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
    }

    return Array.from(clusters.values()).sort((a, b) => {
      const aUn = a.id === "unclustered" ? 1 : 0;
      const bUn = b.id === "unclustered" ? 1 : 0;
      if (aUn !== bUn) return aUn - bUn;

      const aTop = a.docs.length ? Number(a.docs[0].score || 0) : 0;
      const bTop = b.docs.length ? Number(b.docs[0].score || 0) : 0;
      if (aTop !== bTop) return bTop - aTop;

      return safeStr(a.id).localeCompare(safeStr(b.id));
    });
  }

  function filterPreparedHits(preparedHits) {
    const search = normalize(clientSearchInput.value);
    const selectedCluster = normalize(clientClusterSelect.value);

    return preparedHits.filter(item => {
      const clusterOk = !selectedCluster || normalize(item.clusterId) === selectedCluster;
      const searchOk = !search || item.searchBlob.includes(search);

      const termSpace = normalize([
        ...item.clusterLabelTerms,
        ...item.localTerms,
        ...item.globalTerms,
      ].join(" "));

      const termOk = !activeTerm || termSpace.includes(activeTerm);

      return clusterOk && searchOk && termOk;
    });
  }

  function paginateItems(items, page, size) {
    const total = items.length;
    const totalPages = Math.max(1, Math.ceil(total / size));
    const safePage = Math.min(Math.max(1, page), totalPages);
    const start = (safePage - 1) * size;
    const end = start + size;

    return {
      pageItems: items.slice(start, end),
      page: safePage,
      pageSize: size,
      totalItems: total,
      totalPages,
      startIndex: total ? start + 1 : 0,
      endIndex: Math.min(end, total),
    };
  }

  function rebuildClientClusterSelect(filteredFromSearchOnlyHits) {
    const currentValue = clientClusterSelect.value || "";
    const grouped = groupPreparedHits(filteredFromSearchOnlyHits);
    const options = [`<option value="">${escapeHtml(UI_TEXT.all_clusters)}</option>`];

    for (const cluster of grouped) {
      options.push(
        `<option value="${escapeHtml(cluster.id)}">${escapeHtml(cluster.label)} (${cluster.docs.length})</option>`
      );
    }

    clientClusterSelect.innerHTML = options.join("");
    const stillExists = Array.from(clientClusterSelect.options).some(opt => opt.value === currentValue);
    clientClusterSelect.value = stillExists ? currentValue : "";
  }

  function renderSidebar(clusters, preparedHits) {
    const unclusteredCount = preparedHits.filter(h => h.clusterId === "unclustered").length;
    const visibleClusterCount = clusters.filter(c => c.id !== "unclustered").length;

    sidebarClusterInfoEl.textContent =
      `${preparedHits.length} ${UI_TEXT.documents.toLowerCase()}, ${visibleClusterCount} ${UI_TEXT.clusters.toLowerCase()}, ${unclusteredCount} ${UI_TEXT.unclustered.toLowerCase()}`;

    const allClusterTerms = [];
    for (const cluster of clusters) {
      for (const term of cluster.labelTerms || []) allClusterTerms.push(term);
    }

    const counts = new Map();
    for (const term of allClusterTerms) {
      counts.set(term, (counts.get(term) || 0) + 1);
    }

    const topTerms = Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 40)
      .map(([term]) => term);

    topClusterTermsEl.innerHTML = renderTermBadges(topTerms, "term-badge top-term", 40);

    if (!clusters.length) {
      sidebarClustersEl.innerHTML = `<div class="muted">${escapeHtml(UI_TEXT.no_clusters)}</div>`;
      return;
    }

    sidebarClustersEl.innerHTML = clusters.map(cluster => (
      `<a class="sidebar-cluster-link" href="#cluster-${escapeHtml(cluster.id)}" data-cluster="${escapeHtml(cluster.id)}">` +
        `<span class="sidebar-cluster-title">${escapeHtml(cluster.label)}</span>` +
        `<span class="sidebar-cluster-meta">${cluster.docs.length} ${escapeHtml(UI_TEXT.docs)}</span>` +
      `</a>`
    )).join("");
  }

  function renderSummary(clusters, preparedHits) {
    const unclusteredCount = preparedHits.filter(h => h.clusterId === "unclustered").length;
    const visibleClusterCount = clusters.filter(c => c.id !== "unclustered").length;

    const clusterTerms = new Set();
    for (const cluster of clusters) {
      for (const term of cluster.labelTerms || []) clusterTerms.add(term);
    }

    summaryDocumentsEl.textContent = String(preparedHits.length);
    summaryClustersEl.textContent = String(visibleClusterCount);
    summaryUnclusteredEl.textContent = String(unclusteredCount);
    summaryClusterTermsEl.textContent = String(clusterTerms.size);
  }

  function renderPagination(pager) {
    return (
      `<div class="pagination-bar">` +
        `<div class="pagination-info">` +
          `${escapeHtml(UI_TEXT.showing_results)}: ${pager.startIndex}-${pager.endIndex} / ${pager.totalItems}` +
        `</div>` +
        `<div class="pagination-spacer"></div>` +
        `<label class="pagination-info" for="pageSizeSelect">${escapeHtml(UI_TEXT.page_size)}</label>` +
        `<select id="pageSizeSelect" class="select" style="width:auto;">` +
          [10, 25, 50, 100, 200, 500].map(size =>
            `<option value="${size}"${size === pager.pageSize ? " selected" : ""}>${size}</option>`
          ).join("") +
        `</select>` +
        `<button id="prevPageBtn" class="btn btn-secondary" type="button"${pager.page <= 1 ? " disabled" : ""}>${escapeHtml(UI_TEXT.prev)}</button>` +
        `<div class="page-indicator">${escapeHtml(UI_TEXT.page)} ${pager.page} ${escapeHtml(UI_TEXT.of)} ${pager.totalPages}</div>` +
        `<button id="nextPageBtn" class="btn btn-secondary" type="button"${pager.page >= pager.totalPages ? " disabled" : ""}>${escapeHtml(UI_TEXT.next)}</button>` +
      `</div>`
    );
  }

function truncateHighlightedHtml(html, maxLength) {
  const template = document.createElement("template");
  template.innerHTML = html;

  let remaining = maxLength;
  let done = false;

  function truncateNode(node) {
    if (done) {
      node.remove();
      return;
    }

    const children = Array.from(node.childNodes);

    for (const child of children) {
      if (done) {
        child.remove();
        continue;
      }

      if (child.nodeType === Node.TEXT_NODE) {
        const text = child.textContent || "";
        if (text.length <= remaining) {
          remaining -= text.length;
        } else {
          child.textContent = text.slice(0, Math.max(0, remaining)) + "…";
          remaining = 0;
          done = true;
        }
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        truncateNode(child);
      } else {
        child.remove();
      }
    }
  }

  truncateNode(template.content);
  return template.innerHTML;
}

function sanitizeHighlightHtml(html) {
  const template = document.createElement("template");
  template.innerHTML = safeStr(html);

  const allowedTags = new Set(["EM", "STRONG", "MARK"]);

  function clean(node) {
    const children = Array.from(node.childNodes);

    for (const child of children) {
      if (child.nodeType === Node.ELEMENT_NODE) {
        if (!allowedTags.has(child.tagName)) {
          const textNode = document.createTextNode(child.textContent || "");
          child.replaceWith(textNode);
          continue;
        }

        const attrs = Array.from(child.attributes);
        for (const attr of attrs) {
          child.removeAttribute(attr.name);
        }

        clean(child);
      } else if (child.nodeType === Node.TEXT_NODE) {
        // ok
      } else {
        child.remove();
      }
    }
  }

  clean(template.content);

  return template.innerHTML
    .replace(/<em>/gi, '<mark class="search-hit">')
    .replace(/<\/em>/gi, '</mark>')
    .replace(/<strong>/gi, '<mark class="search-hit strong-hit">')
    .replace(/<\/strong>/gi, '</mark>');
}

  function renderContentFromClusters(clusters, pager) {
    if (!clusters.length) {
      contentEl.innerHTML = (
        `<div class="muted">${escapeHtml(UI_TEXT.no_content)}</div>` +
        renderPagination(pager)
      );
      wirePaginationControls(pager);
      return;
    }

    const sectionsHtml = clusters.map(cluster => {
      const docsHtml = cluster.docs.map(item => {
        const allTerms = uniqueTerms([
          ...item.clusterLabelTerms,
          ...item.localTerms,
          ...item.globalTerms,
        ]);

function renderSnippet(snippet) {
  if (!snippet || !snippet.fragments || !snippet.fragments.length) {
    return `<span class="muted">${escapeHtml(UI_TEXT.no_snippet)}</span>`;
  }

  const items = snippet.fragments.map(fragment => {
    if (snippet.isHighlighted) {
      return `<li>${sanitizeHighlightHtml(fragment)}</li>`;
    } else {
      return `<li>${escapeHtml(fragment)}</li>`;
    }
  }).join("");

  return `<ul class="snippet-list">${items}</ul>`;
}

        const snippetHtml = renderSnippet(item.snippet);

        const clusterInfoHtml = EXPLORER_CONFIG.includeClusterInfo ? (
          `<section class="details-section">` +
            `<h4>${escapeHtml(UI_TEXT.cluster_info)}</h4>` +
            `<div class="kv-grid">` +
              `<div><strong>id</strong><div>${escapeHtml(safeStr(item.cluster.id ?? ""))}</div></div>` +
              `<div><strong>label</strong><div>${escapeHtml(safeStr(item.cluster.label ?? ""))}</div></div>` +
              `<div><strong>${escapeHtml(UI_TEXT.size)}</strong><div>${escapeHtml(safeStr(item.cluster.size ?? ""))}</div></div>` +
              `<div><strong>${escapeHtml(UI_TEXT.source)}</strong><div>${escapeHtml(safeStr(item.cluster.source ?? ""))}</div></div>` +
              `<div><strong>${escapeHtml(UI_TEXT.label_source)}</strong><div>${escapeHtml(safeStr(item.cluster.label_source ?? ""))}</div></div>` +
            `</div>` +
          `</section>`
        ) : "";

        const localHtml = EXPLORER_CONFIG.includeLocalTerms ? (
          `<div class="doc-chip-row">` +
            `<span class="chip-label">${escapeHtml(UI_TEXT.local_terms)}</span>` +
            renderTermBadges(item.localTerms, "term-badge local-term") +
          `</div>`
        ) : "";

        const localDetailsHtml = EXPLORER_CONFIG.includeLocalTerms ? (
          `<section class="details-section">` +
            `<h4>${escapeHtml(UI_TEXT.local_analysis)}</h4>` +
            renderTermDetails(extractKeyTermDetails(item.local)) +
          `</section>`
        ) : "";

        const globalHtml = EXPLORER_CONFIG.includeGlobalTerms ? (
          `<div class="doc-chip-row">` +
            `<span class="chip-label">${escapeHtml(UI_TEXT.global_terms)}</span>` +
            renderTermBadges(item.globalTerms, "term-badge global-term") +
          `</div>`
        ) : "";

        const globalDetailsHtml = EXPLORER_CONFIG.includeGlobalTerms ? (
          `<section class="details-section">` +
            `<h4>${escapeHtml(UI_TEXT.global_analysis)}</h4>` +
            renderTermDetails(extractKeyTermDetails(item.global)) +
          `</section>`
        ) : "";

        return (
          `<article class="doc-card" id="doc-${escapeHtml(item.id)}">` +
            `<div class="doc-card-header">` +
              `<div class="doc-card-title">${escapeHtml(item.title)}</div>` +
              `<div class="doc-card-meta">` +
                `<span><strong>${escapeHtml(UI_TEXT.id)}:</strong> ${escapeHtml(item.id)}</span>` +
                `<span><strong>${escapeHtml(UI_TEXT.score)}:</strong> ${escapeHtml(fmtScore(item.score))}</span>` +
                `<span><strong>${escapeHtml(UI_TEXT.cluster)}:</strong> ${escapeHtml(item.clusterLabel)}</span>` +
              `</div>` +
            `</div>` +

            `<div class="doc-snippet">${snippetHtml}</div>` +

            `<div class="doc-chip-row">` +
              `<span class="chip-label">${escapeHtml(UI_TEXT.cluster_terms)}</span>` +
              renderTermBadges(item.clusterLabelTerms, "term-badge cluster-term") +
            `</div>` +

            localHtml +
            globalHtml +

            `<details class="details-block">` +
              `<summary>${escapeHtml(UI_TEXT.analysis_details)}</summary>` +
              clusterInfoHtml +
              localDetailsHtml +
              globalDetailsHtml +
              `<section class="details-section">` +
                `<h4>${escapeHtml(UI_TEXT.source_preview)}</h4>` +
                renderSourcePreview(item.source) +
              `</section>` +
            `</details>` +
          `</article>`
        );
      }).join("");

      return (
        `<section class="cluster-section" id="cluster-${escapeHtml(cluster.id)}" data-cluster="${escapeHtml(cluster.id)}">` +
          `<div class="cluster-header">` +
            `<h2>${escapeHtml(cluster.label)}</h2>` +
            `<div class="cluster-meta">${cluster.docs.length} ${escapeHtml(UI_TEXT.documents.toLowerCase())}</div>` +
          `</div>` +
          `<div class="cluster-terms-wrap">${renderTermBadges(cluster.labelTerms, "term-badge cluster-term")}</div>` +
          `<div class="cluster-docs">${docsHtml}</div>` +
        `</section>`
      );
    }).join("");

    contentEl.innerHTML = sectionsHtml + renderPagination(pager);
    wirePaginationControls(pager);
  }

  function wirePaginationControls(pager) {
    const prevBtn = document.getElementById("prevPageBtn");
    const nextBtn = document.getElementById("nextPageBtn");
    const pageSizeSelect = document.getElementById("pageSizeSelect");

    if (prevBtn) {
      prevBtn.addEventListener("click", function () {
        if (pager.page > 1) {
          currentPage = pager.page - 1;
          refreshView();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener("click", function () {
        if (pager.page < pager.totalPages) {
          currentPage = pager.page + 1;
          refreshView();
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      });
    }

    if (pageSizeSelect) {
      pageSizeSelect.addEventListener("change", function () {
        const nextSize = Number(pageSizeSelect.value || EXPLORER_CONFIG.pageSize || 25);
        pageSize = Number.isFinite(nextSize) && nextSize > 0 ? nextSize : 25;
        currentPage = 1;
        refreshView();
      });
    }
  }

  function syncActiveTermUI() {
    const buttons = Array.from(document.querySelectorAll(".term-badge"));
    buttons.forEach(btn => {
      const term = normalize(btn.dataset.term || btn.textContent || "");
      btn.classList.toggle("active-term", !!activeTerm && term === activeTerm);
    });
  }

  function refreshView() {
    const searchOnlyHits = allPreparedHits.filter(item => {
      const search = normalize(clientSearchInput.value);
      if (!search) return true;
      return item.searchBlob.includes(search);
    });

    rebuildClientClusterSelect(searchOnlyHits);

    const filteredHits = filterPreparedHits(allPreparedHits);
    const filteredClusters = groupPreparedHits(filteredHits);

    renderSidebar(filteredClusters, filteredHits);
    renderSummary(filteredClusters, filteredHits);

    const pager = paginateItems(filteredHits, currentPage, pageSize);
    currentPage = pager.page;

    const pageClusters = groupPreparedHits(pager.pageItems);
    renderContentFromClusters(pageClusters, pager);

    const visibleClusters = filteredClusters.filter(c => c.id !== "unclustered").length;
    let info = `${filteredHits.length} ${UI_TEXT.documents_visible}, ${visibleClusters} ${UI_TEXT.clusters_visible}`;
    if (activeTerm) info += `, ${UI_TEXT.active_term}: "${activeTerm}"`;
    resultInfoEl.textContent = info;

    syncActiveTermUI();
  }

  function setActiveTerm(term) {
    activeTerm = normalize(term);
    currentPage = 1;
    refreshView();
  }

  function setApiStatus(text, kind = "") {
    apiStatusEl.textContent = text;
    apiStatusEl.className = "status-line" + (kind ? ` ${kind}` : "");
  }

  function setRawResponse(data) {
    try {
      rawResponseEl.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
      rawResponseEl.textContent = safeStr(data);
    }
  }

  function collectFormValues() {
    const params = new URLSearchParams();

    for (const param of ENDPOINT_PARAMETERS) {
      const name = safeStr(param.name);
      if (!name) continue;

      const el = document.querySelector(`[data-param="${CSS.escape(name)}"]`);
      if (!el) continue;

      const required = !!param.required;
      let value = "";

      if (el.type === "checkbox") {
        value = el.checked ? "true" : "false";
      } else {
        value = safeStr(el.value || "").trim();
      }

      if (!value && !required) continue;
      if (!value && required) {
        throw new Error(`Missing required parameter: ${name}`);
      }

      params.set(name, value);
    }

    params.set("perform_analysis", "true");
    params.set("cluster_enabled", "true");
    params.set("format", "json");

    if (!params.get("analysis_mode")) params.set("analysis_mode", "both");
    if (!params.get("cluster_source")) params.set("cluster_source", "local");
    if (!params.get("cluster_label_source")) params.set("cluster_label_source", "local");

    return params;
  }

  function resetApiForm() {
    for (const param of ENDPOINT_PARAMETERS) {
      const name = safeStr(param.name);
      if (!name) continue;

      const el = document.querySelector(`[data-param="${CSS.escape(name)}"]`);
      if (!el) continue;

      const schema = param.schema || {};
      const defaultValue = schema.default;

      if (el.type === "checkbox") {
        el.checked = !!defaultValue;
      } else {
        el.value = defaultValue !== undefined && defaultValue !== null ? String(defaultValue) : "";
      }
    }
  }

  function setHits(hits) {
    allPreparedHits = prepareHits(hits);
    currentPage = 1;
    refreshView();
  }

  
function getAggregations(data) {
  if (!data || typeof data !== "object") return {};
  if (data.aggregations && typeof data.aggregations === "object") return data.aggregations;
  if (data.aggs && typeof data.aggs === "object") return data.aggs;
  return {};
    }

function getAggregationBuckets(agg) {
  if (!agg || typeof agg !== "object") return [];

  if (Array.isArray(agg.buckets)) {
    return agg.buckets;
  }

  if (agg.values && typeof agg.values === "object" && Array.isArray(agg.values.buckets)) {
    return agg.values.buckets;
  }

  return [];
}

function renderAggregationTable(name, agg) {
  const buckets = getAggregationBuckets(agg);
  if (!buckets.length) return "";

  const rows = buckets.map(bucket => (
    "<tr>" +
      `<td>${escapeHtml(safeStr(bucket.key_as_string ?? bucket.key ?? ""))}</td>` +
      `<td>${escapeHtml(safeStr(bucket.doc_count ?? ""))}</td>` +
    "</tr>"
  )).join("");

  const totalDocCount = safeStr(
    agg.doc_count ??
    (agg.values && agg.values.doc_count != null ? agg.values.doc_count : "")
  );

  return (
    `<section class="cluster-section">` +
      `<div class="cluster-header">` +
        `<h2>Aggregations</h2>` +
        `<div class="cluster-meta">${escapeHtml(totalDocCount)} docs, ${buckets.length} buckets</div>` +
      `</div>` +
      `<table class="mini-table">` +
        `<thead>` +
          `<tr><th>key</th><th>doc_count</th></tr>` +
        `</thead>` +
        `<tbody>${rows}</tbody>` +
      `</table>` +
    `</section>`
  );
}

function renderAggregations(data) {
  const container = document.getElementById("aggregationsContainer");
  if (!container) return;

  const aggregations = getAggregations(data);
  const entries = Object.entries(aggregations);

  if (!entries.length) {
    container.innerHTML = "";
    return;
  }

  const html = entries
    .map(([name, agg]) => renderAggregationTable(name, agg))
    .filter(Boolean)
    .join("");

  container.innerHTML = html;
}

  async function runApiSearch(event) {
    if (event) event.preventDefault();

    let params;
    try {
      params = collectFormValues();
    } catch (err) {
      setApiStatus(`${UI_TEXT.error}: ${err.message}`, "error");
      return;
    }

    const url = `${EXPLORER_CONFIG.apiBaseUrl || ""}${EXPLORER_CONFIG.endpointPath}?${params.toString()}`;

    const requestUrlPreEl = document.getElementById("requestUrlPre");

    requestUrlBoxEl.innerHTML = `<strong>${escapeHtml(UI_TEXT.request_url)}:</strong>`;
    requestUrlPreEl.textContent = url;
    requestUrlPreEl.classList.remove("hidden");

    setApiStatus(UI_TEXT.searching);
    submitApiSearchBtn.disabled = true;

    try {
      const response = await fetch(url, {
        method: "GET",
        headers: { "Accept": "application/json" }
      });

      const text = await response.text();
      let data = null;

      try {
        data = text ? JSON.parse(text) : null;
      } catch (err) {
        throw new Error(`Response was not valid JSON (HTTP ${response.status})`);
      }

      setRawResponse(data);
      renderAggregations(data);

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const hits = normalizeHitsFromResponse(data);
      setHits(hits);
      setApiStatus(`${UI_TEXT.status_fetch_ok}: ${hits.length} hits`);
    } catch (err) {
      setApiStatus(`${UI_TEXT.status_fetch_failed}: ${err.message}`, "error");
    } finally {
      submitApiSearchBtn.disabled = false;
    }
  }

  let filterDebounceTimer = null;

  function scheduleRefresh() {
    if (filterDebounceTimer) window.clearTimeout(filterDebounceTimer);
    filterDebounceTimer = window.setTimeout(function () {
      currentPage = 1;
      refreshView();
    }, 120);
  }

  document.addEventListener("click", function (event) {
    const btn = event.target.closest(".term-badge");
    if (!btn) return;

    const clickedTerm = normalize(btn.dataset.term || btn.textContent || "");
    if (!clickedTerm) return;

    if (activeTerm === clickedTerm) {
      setActiveTerm("");
    } else {
      setActiveTerm(clickedTerm);
    }
  });

  clientSearchInput.addEventListener("input", scheduleRefresh);
  clientClusterSelect.addEventListener("change", function () {
    currentPage = 1;
    refreshView();
  });

  resetClientFiltersBtn.addEventListener("click", function () {
    clientSearchInput.value = "";
    clientClusterSelect.value = "";
    setActiveTerm("");
  });

  expandAllBtn.addEventListener("click", function () {
    const details = Array.from(document.querySelectorAll(".details-block"));
    const shouldOpen = details.some(d => !d.open);
    details.forEach(d => {
      d.open = shouldOpen;
    });
  });

  apiSearchForm.addEventListener("submit", runApiSearch);

  resetApiFormBtn.addEventListener("click", function () {
    resetApiForm();
    setApiStatus(UI_TEXT.status_ready);
  });

  toggleRawResponseBtn.addEventListener("click", function () {
    rawResponseVisible = !rawResponseVisible;
    rawResponseEl.classList.toggle("visible", rawResponseVisible);
    toggleRawResponseBtn.textContent = rawResponseVisible
      ? UI_TEXT.hide_raw_response
      : UI_TEXT.show_raw_response;
  });

  resetApiForm();

  if (Array.isArray(INITIAL_HITS) && INITIAL_HITS.length) {
    setHits(INITIAL_HITS);
    setApiStatus(`${UI_TEXT.status_loaded_initial_hits}: ${INITIAL_HITS.length} hits`);
  } else {
    setHits([]);
    renderAggregations({});
    setApiStatus(UI_TEXT.status_no_initial_hits, "warning");
  }
})();
"""

    html = f"""<!DOCTYPE html>
<html lang="{escape(ui["lang"], quote=True)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
{css}
  </style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <div class="sidebar-section">
        <div class="sidebar-title">{escape(title)}</div>
        <div class="cluster-list-info" id="sidebarClusterInfo">{escape(ui["status_ready"])}</div>
      </div>

      <div class="sidebar-section">
        <div class="section-title">{escape(ui["top_cluster_terms"])}</div>
        <div class="term-cloud" id="topClusterTerms"></div>
      </div>

      <div class="sidebar-section">
        <div class="section-title">{escape(ui["clusters"])}</div>
        <div id="sidebarClusters"></div>
      </div>
    </aside>

    <main class="main">
      <section class="panel search-panel">
        <details open id="apiSearchDetails">
          <summary>{escape(ui["api_search"])}</summary>

          <div class="muted">{escape(ui["submit_hint"])}</div>
          <div class="note-box">
            The API request automatically adds <code>perform_analysis=true</code> and
            <code>cluster_enabled=true</code>.
          </div>

          <form id="apiSearchForm">
            {main_form_html}
            {analysis_form_html}
            {advanced_form_html}

            <div class="form-actions">
              <button id="submitApiSearchBtn" class="btn btn-primary" type="submit">{escape(ui["search"])}</button>
              <button id="resetApiFormBtn" class="btn btn-secondary" type="button">{escape(ui["reset_form"])}</button>
            </div>

            <div id="apiStatus" class="status-line">{escape(ui["status_ready"])}</div>
            <div id="requestUrlBox" class="status-line"></div>
            <pre id="requestUrlPre" class="request-url-block hidden"></pre>

            <div class="raw-response-wrap">
              <button id="toggleRawResponseBtn" class="btn btn-secondary" type="button">{escape(ui["show_raw_response"])}</button>
              <pre id="rawResponse" class="raw-response"></pre>
            </div>
          </form>
        </details>
      </section>

      <section class="panel header-card">
        <h1>{escape(title)}</h1>
        <div class="muted">{escape(ui["subtitle"])}</div>

        <div class="summary-grid">
          <div class="summary-item">
            <div class="label">{escape(ui["documents"])}</div>
            <div class="value" id="summaryDocuments">0</div>
          </div>
          <div class="summary-item">
            <div class="label">{escape(ui["clusters"])}</div>
            <div class="value" id="summaryClusters">0</div>
          </div>
          <div class="summary-item">
            <div class="label">{escape(ui["unclustered"])}</div>
            <div class="value" id="summaryUnclustered">0</div>
          </div>
          <div class="summary-item">
            <div class="label">{escape(ui["cluster_terms_count"])}</div>
            <div class="value" id="summaryClusterTerms">0</div>
          </div>
        </div>

        <div id="aggregationsContainer"></div>

        <div class="controls">
          <input
            id="clientSearchInput"
            class="input"
            type="text"
            placeholder="{escape(ui['search_placeholder'], quote=True)}"
          >
          <select id="clientClusterSelect" class="select">
            <option value="">{escape(ui["all_clusters"])}</option>
          </select>
          <button id="resetClientFiltersBtn" class="btn btn-secondary" type="button">{escape(ui["reset_filters"])}</button>
          <button id="expandAllBtn" class="btn btn-secondary" type="button">{escape(ui["toggle_all_details"])}</button>
        </div>

        <div id="resultInfo" class="result-info"></div>
      </section>

      <div id="content"></div>

      <div class="footer-note">{escape(ui["generated_without_dependencies"])}</div>
    </main>
  </div>

  <script>
{config_js}
  </script>

  <script>
{js}
  </script>
</body>
</html>
"""

    output = Path(output_path).expanduser().resolve()
    output.write_text(html, encoding="utf-8")
    return str(output)