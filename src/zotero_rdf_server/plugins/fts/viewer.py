from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .helpers import plugin_logger, resolve_config_path, safe_doc_id

logger = plugin_logger()

@lru_cache(maxsize=8)
def get_viewer_config(config_path: Path) -> dict[str, Any]:
    from zotero_rdf_server.utils import load_dict_like
    cfg = load_dict_like(config_path, label="Viewer Config", verbose=False)
    if not isinstance(cfg, dict):
        logger.warning("Viewer config is not a dict, using empty config")
        return {}

    viewer_cfg = cfg.get("viewer")
    if isinstance(viewer_cfg, dict):
        return viewer_cfg

    return cfg

cfg_path = resolve_config_path()
logger.debug(f"Loading config from {cfg_path}")
cfg = get_viewer_config(cfg_path)
logger.debug(f"Viewer config: {cfg}")

DEFAULT_ALIAS = str(cfg.get("default_alias") or "ocr")
logger.info(f"DEFAULT_ALIAS: {DEFAULT_ALIAS}")

IMAGE_ROOT_STR = cfg.get("image_root") or ""
TEXT_ROOT_STR = cfg.get("text_root") or ""
IMAGE_EXT = str(cfg.get("image_ext") or "jpg").lstrip(".")
TEXT_EXT = str(cfg.get("text_ext") or "txt").lstrip(".")
DASHBOARD_URL = cfg.get("dashboard_url") or None
BASE_URL = str(cfg.get("base_url", "/plugin/fts/view")).rstrip("/")

mount_path = "/image-files"

if not IMAGE_ROOT_STR:
    logger.warning("viewer.image_root is not configured")
if not TEXT_ROOT_STR:
    logger.warning("viewer.text_root is not configured")

image_root = Path(IMAGE_ROOT_STR) if IMAGE_ROOT_STR else Path(".")
text_root = Path(TEXT_ROOT_STR) if TEXT_ROOT_STR else Path(".")


def ensure_router_mount(open_router) -> None:
    """
    Mount static files once.
    Call this during router/app setup, not inside the route handler.
    """
    
    for route in getattr(open_router, "routes", []):
        if getattr(route, "path", None) == mount_path:
            return
    
    open_router.mount(
        mount_path,
        StaticFiles(directory=str(image_root)),
        name="image-files",
    )
    logger.info(f"Mounted static image path at {mount_path} -> {image_root}")

from zotero_rdf_server.main import app
ensure_router_mount(app)

def split_doc_id(os_doc_id: str) -> tuple[str, str]:
    """
    Input format: <doc_id>:<page>
    Only the last colon separates the page.
    """
    raw = (os_doc_id or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Missing document ID")

    if ":" not in raw:
        raise HTTPException(
            status_code=400,
            detail="Invalid document ID, expected '<doc_id>:<page>'",
        )

    doc_key_raw, page_raw = raw.rsplit(":", 1)

    doc_key = safe_doc_id(doc_key_raw)
    page_digits = "".join(ch for ch in page_raw if ch.isdigit())

    if not doc_key:
        raise HTTPException(status_code=400, detail="Invalid document key")

    if not page_digits:
        raise HTTPException(status_code=400, detail="Invalid page number")

    return doc_key, page_digits.zfill(4)


def image_file(doc_key: str, page: str) -> Path:
    return image_root / doc_key / f"{page}.{IMAGE_EXT}"


def text_file(doc_key: str, page: str) -> Path:
    return text_root / doc_key / f"{page}.{TEXT_EXT}"


def list_pages(doc_key: str) -> list[str]:
    pages: set[str] = set()

    img_dir = image_root / doc_key
    txt_dir = text_root / doc_key

    if img_dir.is_dir():
        pattern = f"*.{IMAGE_EXT}"
        for p in img_dir.glob(pattern):
            stem = p.stem
            if stem.isdigit() and len(stem) == 4:
                pages.add(stem)

    if txt_dir.is_dir():
        pattern = f"*.{TEXT_EXT}"
        for p in txt_dir.glob(pattern):
            stem = p.stem
            if stem.isdigit() and len(stem) == 4:
                pages.add(stem)

    return sorted(pages)

def discover_doc_url(original_os_doc_id: str) -> str | None:
    if not DASHBOARD_URL:
        return None
    from urllib.parse import quote
    encoded_doc_id = quote(original_os_doc_id, safe="")
    return f"{DASHBOARD_URL}{encoded_doc_id}"

def render_page(
    os_doc_id: str,
    page: str,
    pages: list[str],
    image_url: str | None,
    text: str,
    prev_page: str | None,
    next_page: str | None,
    discover_url: str | None,
) -> str:
    safe_os_doc_id = escape(os_doc_id)
    safe_page = escape(page)
    safe_text = escape(text or "")
    options = []
    for p in pages:
        selected = " selected" if p == page else ""
        label = str(int(p)) if p.isdigit() else p
        options.append(
            f'<option value="{BASE_URL}/{safe_os_doc_id}:{escape(p)}"{selected}>{escape(label)}</option>'
        )
    options_html = "\n".join(options)

    nav_parts = []
    if prev_page:
        nav_parts.append(
            f'<a href="{BASE_URL}/{safe_os_doc_id}:{escape(prev_page)}">Previous</a>'
        )
    if next_page:
        nav_parts.append(
            f'<a href="{BASE_URL}/{safe_os_doc_id}:{escape(next_page)}">Next</a>'
        )
    nav_html = "\n".join(nav_parts)

    discover_html = ""
    if discover_url:
        discover_html = (
            f'<a href="{escape(discover_url)}" target="_blank" '
            f'rel="noopener noreferrer">Open in Discover</a>'
        )

    if image_url:
        viewer_html = f"""
        <div id="osd"></div>
        <script src="https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/openseadragon.min.js"></script>
        <script>
          OpenSeadragon({{
            id: "osd",
            prefixUrl: "https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/images/",
            tileSources: {{
              type: "image",
              url: "{escape(image_url)}"
            }},
            showNavigator: true,
            maxZoomPixelRatio: 2,
            visibilityRatio: 1,
            constrainDuringPan: true
          }});
        </script>
        """
    else:
        viewer_html = '<div class="text-panel">No image available.</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_os_doc_id} : {safe_page}</title>
  <style>
    body {{
      font-family: sans-serif;
      margin: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      background: white;
      border-bottom: 1px solid #ccc;
      padding: 0.75rem 1rem;
      z-index: 10;
    }}
    nav {{
      display: flex;
      gap: 0.75rem;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 0.5rem;
    }}
    .layout {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 1rem;
      padding: 1rem;
    }}
    .panel {{
      border: 1px solid #ccc;
      background: #fafafa;
      min-height: 75vh;
    }}
    .viewer-panel {{
      padding: 0;
    }}
    #osd {{
      width: 100%;
      height: 80vh;
      background: #111;
    }}
    .text-panel {{
      padding: 1rem;
    }}
    .text {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: monospace;
    }}
    select, a {{
      font: inherit;
    }}
    @media (max-width: 900px) {{
      .layout {{
        grid-template-columns: 1fr;
      }}
      #osd {{
        height: 60vh;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div><strong>{safe_os_doc_id} : {safe_page}</strong></div>
    <nav>
      {nav_html}
      {"<label for='page-select'>Page</label>" if pages else ""}
      {"<select id='page-select' onchange='window.location.href=this.value'>" + options_html + "</select>" if pages else ""}
      {discover_html}
    </nav>
  </header>

  <div class="layout">
    <div class="panel viewer-panel">
      {viewer_html}
    </div>

    <div class="panel text-panel">
      <div class="text">{safe_text if safe_text else "[no text on this page]"}</div>
    </div>
  </div>
</body>
</html>
"""


# TODO deprecate as in /explorer
# region Analysis Explorer

from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

def write_analysis_explorer_html(
    hits: List[Dict[str, Any]],
    output_path: str,
    *,
    title: str = "Analysis Explorer",
    include_global_terms: bool = True,
    include_local_terms: bool = True,
    include_cluster_info: bool = True,
    max_term_badges_per_section: int = 20,
    max_source_fields_preview: int = 12,
    max_snippet_length: int = 400,
    max_docs_per_cluster: Optional[int] = None,
    max_total_docs: Optional[int] = None,
    preferred_title_fields: Optional[List[str]] = None,
    preferred_snippet_fields: Optional[List[str]] = None,
) -> str:
    """
    Write a self-contained HTML file for interactive exploration of analysis results.

    Expected hit structure:
      - _id
      - _score
      - _source
      - _source.analysis.cluster
      - _source.analysis.local
      - _source.analysis.global

    Main features:
      - English UI
      - Sidebar cluster navigation
      - Text search
      - Cluster filter
      - Clickable key-term filters with visible active state
      - Cluster / local / global analysis display
      - Streaming write to file (avoids building one huge HTML string)
      - No external JS/CSS dependencies

    Returns:
      Absolute path to the generated HTML file.
    """
    UI = {
        "lang": "en",
        "interactive_subtitle": "Interactive exploration for analysis, key terms, and cluster data.",
        "documents": "Documents",
        "clusters": "Clusters",
        "unclustered": "Unclustered",
        "cluster_terms_count": "Cluster terms",
        "top_cluster_terms": "Top Cluster Terms",
        "all_clusters": "All clusters",
        "reset_filters": "Reset filters",
        "toggle_all_details": "Toggle all details",
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
        "generated_without_dependencies": "Generated without external frontend dependencies.",
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
    }

    preferred_title_fields = preferred_title_fields or [
        "title", "name", "label", "headline", "subject", "_id"
    ]
    preferred_snippet_fields = preferred_snippet_fields or [
        "snippet", "summary", "description", "content", "text", "body"
    ]

    def _w(f, s: str = "") -> None:
        f.write(s)
        f.write("\n")

    def _safe_str(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _get_source(hit: Dict[str, Any]) -> Dict[str, Any]:
        src = hit.get("_source")
        return src if isinstance(src, dict) else {}

    def _get_analysis(hit: Dict[str, Any]) -> Dict[str, Any]:
        analysis = _get_source(hit).get("analysis")
        return analysis if isinstance(analysis, dict) else {}

    def _get_cluster(hit: Dict[str, Any]) -> Dict[str, Any]:
        cluster = _get_analysis(hit).get("cluster")
        return cluster if isinstance(cluster, dict) else {}

    def _get_local(hit: Dict[str, Any]) -> Dict[str, Any]:
        local = _get_analysis(hit).get("local")
        return local if isinstance(local, dict) else {}

    def _get_global(hit: Dict[str, Any]) -> Dict[str, Any]:
        glob = _get_analysis(hit).get("global")
        return glob if isinstance(glob, dict) else {}

    def _pick_title(hit: Dict[str, Any]) -> str:
        src = _get_source(hit)
        for field in preferred_title_fields:
            if field == "_id":
                break
            value = src.get(field)
            if value not in (None, "", []):
                if isinstance(value, (list, tuple)):
                    return " | ".join(_safe_str(v) for v in value[:3])
                return _safe_str(value)
        return _safe_str(hit.get("_id", "untitled"))

    def _pick_snippet(hit: Dict[str, Any]) -> str:
        src = _get_source(hit)
        for field in preferred_snippet_fields:
            value = src.get(field)
            if value not in (None, "", []):
                if isinstance(value, list):
                    text = " ".join(_safe_str(v) for v in value[:5])
                else:
                    text = _safe_str(value)
                text = " ".join(text.split())
                if len(text) > max_snippet_length:
                    return text[: max_snippet_length - 1] + "…"
                return text
        return ""

    def _extract_key_terms(section: Dict[str, Any]) -> List[str]:
        key_terms = section.get("key_terms") or []
        result: List[str] = []
        for term in key_terms:
            s = _safe_str(term).strip()
            if s:
                result.append(s)
        return result

    def _extract_key_term_details(section: Dict[str, Any]) -> List[Dict[str, Any]]:
        details = section.get("key_terms_details") or []
        result: List[Dict[str, Any]] = []
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and item.get("term"):
                    result.append(item)
        return result

    def _extract_cluster_label_terms(cluster: Dict[str, Any]) -> List[str]:
        terms = cluster.get("label_terms") or []
        result: List[str] = []
        for term in terms:
            s = _safe_str(term).strip()
            if s:
                result.append(s)
        return result

    def _fmt_score(value: Any) -> str:
        try:
            return f"{float(value):.4f}"
        except Exception:
            return "0.0000"

    def _json_like(value: Any, depth: int = 0) -> str:
        # This is intentionally compact. The goal is to preview source fields,
        # not to fully serialize arbitrary nested content.
        if depth > 2:
            return "..."
        if isinstance(value, dict):
            items = []
            for k, v in list(value.items())[:20]:
                items.append(f"{_safe_str(k)}: {_json_like(v, depth + 1)}")
            if len(value) > 20:
                items.append("...")
            return "{ " + ", ".join(items) + " }"
        if isinstance(value, list):
            parts = [_json_like(v, depth + 1) for v in value[:10]]
            if len(value) > 10:
                parts.append("...")
            return "[ " + ", ".join(parts) + " ]"
        return _safe_str(value)

    def _render_term_badges(
        terms: Iterable[str],
        *,
        css_class: str = "term-badge",
        limit: int = max_term_badges_per_section,
    ) -> str:
        badges: List[str] = []
        seen = set()

        for term in terms:
            term_str = _safe_str(term).strip()
            if not term_str or term_str in seen:
                continue
            seen.add(term_str)

            if len(badges) >= limit:
                break

            badges.append(
                f'<button class="{css_class}" '
                f'data-term="{escape(term_str, quote=True)}" '
                f'type="button">{escape(term_str)}</button>'
            )

        if not badges:
            return f'<span class="muted">–</span>'
        return "".join(badges)

    def _render_term_details(details: List[Dict[str, Any]]) -> str:
        if not details:
            return f'<div class="muted">{escape(UI["no_details"])}</div>'

        rows = []
        for item in details[:max_term_badges_per_section]:
            term = escape(_safe_str(item.get("term")))
            score = escape(_fmt_score(item.get("score", 0)))
            doc_freq = escape(_safe_str(item.get("doc_freq", "")))
            term_freq = escape(_safe_str(item.get("term_freq", "")))
            rows.append(
                "<tr>"
                f"<td>{term}</td>"
                f"<td>{score}</td>"
                f"<td>{doc_freq}</td>"
                f"<td>{term_freq}</td>"
                "</tr>"
            )

        return (
            '<table class="mini-table">'
            f"<thead><tr><th>{escape(UI['term'])}</th><th>{escape(UI['score'])}</th>"
            f"<th>{escape(UI['doc_freq'])}</th><th>{escape(UI['term_freq'])}</th></tr></thead>"
            "<tbody>"
            + "".join(rows) +
            "</tbody></table>"
        )

    def _render_source_preview(src: Dict[str, Any]) -> str:
        keys = [k for k in src.keys() if k != "analysis"]
        if not keys:
            return f'<div class="muted">{escape(UI["no_fields"])}</div>'

        rows = []
        for key in keys[:max_source_fields_preview]:
            val = src.get(key)
            rows.append(
                "<tr>"
                f"<td>{escape(_safe_str(key))}</td>"
                f"<td>{escape(_json_like(val))}</td>"
                "</tr>"
            )

        return (
            '<table class="mini-table">'
            f"<thead><tr><th>{escape(UI['field'])}</th><th>{escape(UI['value'])}</th></tr></thead>"
            "<tbody>"
            + "".join(rows) +
            "</tbody></table>"
        )

    # ------------------------------------------------------------------
    # Prepare normalized in-memory representation of the hits
    # ------------------------------------------------------------------
    prepared_hits: List[Dict[str, Any]] = []
    clusters = defaultdict(list)
    cluster_meta: Dict[str, Dict[str, Any]] = {}
    cluster_term_counter = Counter()

    for idx, hit in enumerate(hits):
        src = _get_source(hit)
        cluster = _get_cluster(hit)
        local = _get_local(hit)
        glob = _get_global(hit)

        hit_id = _safe_str(hit.get("_id", f"hit-{idx}"))
        score = hit.get("_score", 0)
        title_text = _pick_title(hit)
        snippet = _pick_snippet(hit)

        cluster_id = cluster.get("id")
        cluster_id_str = "unclustered" if cluster_id is None else _safe_str(cluster_id)
        cluster_label = _safe_str(cluster.get("label")) or UI["unclustered"]
        cluster_size = cluster.get("size", 0)
        cluster_label_terms = _extract_cluster_label_terms(cluster)

        local_terms = _extract_key_terms(local)
        global_terms = _extract_key_terms(glob)
        searchable_terms = list(dict.fromkeys(cluster_label_terms + local_terms + global_terms))

        search_blob = " ".join([
            hit_id,
            title_text,
            snippet,
            cluster_label,
            " ".join(searchable_terms),
        ]).lower()

        prepared = {
            "idx": idx,
            "hit": hit,
            "id": hit_id,
            "score": score,
            "title": title_text,
            "snippet": snippet,
            "source": src,
            "cluster": cluster,
            "local": local,
            "global": glob,
            "cluster_id_str": cluster_id_str,
            "cluster_label": cluster_label,
            "cluster_size": cluster_size,
            "cluster_label_terms": cluster_label_terms,
            "local_terms": local_terms,
            "global_terms": global_terms,
            "search_blob": search_blob,
        }

        prepared_hits.append(prepared)
        clusters[cluster_id_str].append(prepared)

        cluster_meta.setdefault(
            cluster_id_str,
            {
                "id": cluster_id,
                "id_str": cluster_id_str,
                "label": cluster_label,
                "size": cluster_size,
                "label_terms": cluster_label_terms,
            },
        )

        for term in cluster_label_terms:
            cluster_term_counter[term] += 1

    # Sort hits in each cluster by score descending
    for cluster_key in list(clusters.keys()):
        clusters[cluster_key].sort(key=lambda x: float(x.get("score") or 0), reverse=True)

    # Optional truncation
    if max_docs_per_cluster is not None and max_docs_per_cluster >= 0:
        for cluster_key in list(clusters.keys()):
            clusters[cluster_key] = clusters[cluster_key][:max_docs_per_cluster]

    if max_total_docs is not None and max_total_docs >= 0:
        kept = 0
        for cluster_key in sorted(
            clusters.keys(),
            key=lambda k: (
                1 if k == "unclustered" else 0,
                -max((float(d.get("score") or 0) for d in clusters[k]), default=0.0),
                str(k),
            ),
        ):
            remaining = max_total_docs - kept
            if remaining <= 0:
                clusters[cluster_key] = []
                continue
            if len(clusters[cluster_key]) > remaining:
                clusters[cluster_key] = clusters[cluster_key][:remaining]
            kept += len(clusters[cluster_key])

    def _cluster_sort_key(cluster_key: str) -> Any:
        docs = clusters[cluster_key]
        top_score = max((float(d.get("score") or 0) for d in docs), default=0.0)
        unclustered_flag = 1 if cluster_key == "unclustered" else 0
        return (unclustered_flag, -top_score, str(cluster_key))

    sorted_cluster_keys = sorted(clusters.keys(), key=_cluster_sort_key)

    visible_doc_count = sum(len(clusters[k]) for k in sorted_cluster_keys)
    visible_cluster_count = sum(1 for k in sorted_cluster_keys if clusters[k])
    non_unclustered_visible_cluster_count = sum(
        1 for k in sorted_cluster_keys if k != "unclustered" and clusters[k]
    )
    unclustered_count = len(clusters.get("unclustered", []))

    # ------------------------------------------------------------------
    # HTML generation
    # ------------------------------------------------------------------
    output = Path(output_path).expanduser().resolve()
    with output.open("w", encoding="utf-8") as f:
        _w(f, "<!DOCTYPE html>")
        _w(f, f"<html lang='{escape(UI['lang'], quote=True)}'>")
        _w(f, "<head>")
        _w(f, "<meta charset='utf-8'>")
        _w(f, "<meta name='viewport' content='width=device-width, initial-scale=1'>")
        _w(f, f"<title>{escape(title)}</title>")

        _w(f, "<style>")
        _w(f, r"""
:root {
  --bg: #0b1020;
  --panel: #121933;
  --panel-2: #182140;
  --text: #e8ecf8;
  --muted: #9aa7c7;
  --line: #2a3561;
  --accent: #7aa2ff;
  --accent-2: #8ef0c8;
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

a {
  color: inherit;
  text-decoration: none;
}

button {
  font: inherit;
}

.layout {
  display: grid;
  grid-template-columns: 320px 1fr;
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
  padding: 24px;
}

.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

.header-card {
  padding: 18px;
  margin-bottom: 18px;
}

.header-card h1 {
  margin: 0 0 10px 0;
  font-size: 24px;
}

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

.input, .select {
  width: 100%;
  padding: 11px 12px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: #0d1530;
  color: var(--text);
}

.btn {
  padding: 11px 14px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--text);
  cursor: pointer;
}

.btn:hover {
  background: #22315e;
}

.muted {
  color: var(--muted);
}

.sidebar-title {
  font-size: 18px;
  font-weight: 700;
  margin-bottom: 10px;
}

.sidebar-section {
  margin-bottom: 18px;
}

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

.term-cloud {
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

.cluster-terms-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
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

.doc-card-header {
  margin-bottom: 10px;
}

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

.doc-chip-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
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

.details-section {
  margin-top: 12px;
}

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
  border-collapse: collapse;
  font-size: 12px;
}

.mini-table th,
.mini-table td {
  border-bottom: 1px solid var(--line);
  text-align: left;
  padding: 8px 10px;
  vertical-align: top;
  word-break: break-word;
}

.mini-table th {
  color: var(--muted);
  font-weight: 600;
}

.hidden {
  display: none !important;
}

.result-info {
  color: var(--muted);
  margin: 8px 0 20px 0;
}

.footer-note {
  color: var(--muted);
  font-size: 12px;
  margin-top: 22px;
}

@media (max-width: 1100px) {
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
}
        """)
        _w(f, "</style>")
        _w(f, "</head>")
        _w(f, "<body>")
        _w(f, "<div class='layout'>")

        # Sidebar
        _w(f, "<aside class='sidebar'>")
        _w(f, "<div class='sidebar-section'>")
        _w(f, f"<div class='sidebar-title'>{escape(title)}</div>")
        _w(
            f,
            f"<div class='cluster-list-info'>{visible_doc_count} {escape(UI['documents']).lower()}, "
            f"{non_unclustered_visible_cluster_count} {escape(UI['clusters']).lower()}, "
            f"{unclustered_count} {escape(UI['unclustered']).lower()}</div>"
        )
        _w(f, "</div>")

        _w(f, "<div class='sidebar-section'>")
        _w(f, f"<div class='section-title'>{escape(UI['top_cluster_terms'])}</div>")
        _w(f, "<div class='term-cloud'>")
        _w(
            f,
            _render_term_badges(
                [term for term, _ in cluster_term_counter.most_common(40)],
                css_class="term-badge top-term",
                limit=40,
            ),
        )
        _w(f, "</div>")
        _w(f, "</div>")

        _w(f, "<div class='sidebar-section'>")
        _w(f, f"<div class='section-title'>{escape(UI['clusters'])}</div>")
        if any(clusters[k] for k in sorted_cluster_keys):
            for cluster_key in sorted_cluster_keys:
                docs = clusters[cluster_key]
                if not docs:
                    continue
                meta = cluster_meta.get(cluster_key, {})
                cluster_title = escape(_safe_str(meta.get("label", UI["unclustered"])))
                cluster_size_text = f"{len(docs)} {escape(UI['docs'])}"
                _w(
                    f,
                    "<a class='sidebar-cluster-link' "
                    f"href='#cluster-{escape(cluster_key, quote=True)}' "
                    f"data-cluster='{escape(cluster_key, quote=True)}'>"
                    f"<span class='sidebar-cluster-title'>{cluster_title}</span>"
                    f"<span class='sidebar-cluster-meta'>{cluster_size_text}</span>"
                    "</a>"
                )
        else:
            _w(f, f"<div class='muted'>{escape(UI['no_clusters'])}</div>")
        _w(f, "</div>")
        _w(f, "</aside>")

        # Main
        _w(f, "<main class='main'>")
        _w(f, "<section class='panel header-card'>")
        _w(f, f"<h1>{escape(title)}</h1>")
        _w(f, f"<div class='muted'>{escape(UI['interactive_subtitle'])}</div>")

        _w(f, "<div class='summary-grid'>")
        summary_items = [
            (UI["documents"], str(visible_doc_count)),
            (UI["clusters"], str(non_unclustered_visible_cluster_count)),
            (UI["unclustered"], str(unclustered_count)),
            (UI["cluster_terms_count"], str(len(cluster_term_counter))),
        ]
        for label, value in summary_items:
            _w(f, "<div class='summary-item'>")
            _w(f, f"<div class='label'>{escape(label)}</div>")
            _w(f, f"<div class='value'>{escape(value)}</div>")
            _w(f, "</div>")
        _w(f, "</div>")

        _w(f, "<div class='controls'>")
        _w(
            f,
            f"<input id='searchInput' class='input' type='text' "
            f"placeholder='{escape(UI['search_placeholder'], quote=True)}'>"
        )

        _w(f, "<select id='clusterSelect' class='select'>")
        _w(f, f"<option value=''>{escape(UI['all_clusters'])}</option>")
        for cluster_key in sorted(cluster_meta.keys(), key=lambda k: str(k)):
            docs = clusters.get(cluster_key, [])
            if not docs:
                continue
            label = _safe_str(cluster_meta[cluster_key].get("label", cluster_key))
            _w(
                f,
                f"<option value='{escape(cluster_key, quote=True)}'>"
                f"{escape(label)} ({len(docs)})"
                "</option>"
            )
        _w(f, "</select>")

        _w(f, f"<button id='resetFiltersBtn' class='btn' type='button'>{escape(UI['reset_filters'])}</button>")
        _w(f, f"<button id='expandAllBtn' class='btn' type='button'>{escape(UI['toggle_all_details'])}</button>")
        _w(f, "</div>")
        _w(f, "</section>")

        _w(f, "<div id='resultInfo' class='result-info'></div>")

        _w(f, "<div id='content'>")
        has_content = False

        for cluster_key in sorted_cluster_keys:
            docs = clusters[cluster_key]
            if not docs:
                continue

            has_content = True
            meta = cluster_meta.get(cluster_key, {})
            cluster_label = _safe_str(meta.get("label", UI["unclustered"]))
            cluster_terms_html = _render_term_badges(
                meta.get("label_terms", []),
                css_class="term-badge cluster-term",
            )

            _w(
                f,
                f"<section class='cluster-section' id='cluster-{escape(cluster_key, quote=True)}' "
                f"data-cluster='{escape(cluster_key, quote=True)}'>"
            )
            _w(f, "<div class='cluster-header'>")
            _w(f, f"<h2>{escape(cluster_label)}</h2>")
            _w(f, f"<div class='cluster-meta'>{len(docs)} {escape(UI['documents']).lower()}</div>")
            _w(f, "</div>")

            _w(f, "<div class='cluster-terms-wrap'>")
            _w(f, cluster_terms_html)
            _w(f, "</div>")

            _w(f, "<div class='cluster-docs'>")

            for p in docs:
                local_details = _extract_key_term_details(p["local"])
                global_details = _extract_key_term_details(p["global"])
                all_terms = list(dict.fromkeys(
                    p["cluster_label_terms"] + p["local_terms"] + p["global_terms"]
                ))

                snippet = p.get("snippet")
                snippet_html = (
                    escape(snippet)
                    if snippet
                    else f'<span class="muted">{escape(UI["no_snippet"])}</span>'
                )

                _w(
                    f,
                    f"<article class='doc-card' "
                    f"id='doc-{escape(p['id'], quote=True)}' "
                    f"data-cluster='{escape(p['cluster_id_str'], quote=True)}' "
                    f"data-terms='{escape(' '.join(all_terms).lower(), quote=True)}' "
                    f"data-search='{escape(p['search_blob'], quote=True)}'>"
                )

                _w(f, "<div class='doc-card-header'>")
                _w(f, f"<div class='doc-card-title'>{escape(p['title'])}</div>")
                _w(f, "<div class='doc-card-meta'>")
                _w(f, f"<span><strong>{escape(UI['id'])}:</strong> {escape(p['id'])}</span>")
                _w(f, f"<span><strong>{escape(UI['score'])}:</strong> {escape(_fmt_score(p['score']))}</span>")
                _w(f, f"<span><strong>{escape(UI['cluster'])}:</strong> {escape(p['cluster_label'])}</span>")
                _w(f, "</div>")
                _w(f, "</div>")

                _w(f, f"<div class='doc-snippet'>{snippet_html}</div>")

                _w(f, "<div class='doc-chip-row'>")
                _w(f, f"<span class='chip-label'>{escape(UI['cluster_terms'])}</span>")
                _w(f, _render_term_badges(p["cluster_label_terms"], css_class="term-badge cluster-term"))
                _w(f, "</div>")

                if include_local_terms:
                    _w(f, "<div class='doc-chip-row'>")
                    _w(f, f"<span class='chip-label'>{escape(UI['local_terms'])}</span>")
                    _w(f, _render_term_badges(p["local_terms"], css_class="term-badge local-term"))
                    _w(f, "</div>")

                if include_global_terms:
                    _w(f, "<div class='doc-chip-row'>")
                    _w(f, f"<span class='chip-label'>{escape(UI['global_terms'])}</span>")
                    _w(f, _render_term_badges(p["global_terms"], css_class="term-badge global-term"))
                    _w(f, "</div>")

                _w(f, "<details class='details-block'>")
                _w(f, f"<summary>{escape(UI['analysis_details'])}</summary>")

                if include_cluster_info:
                    cluster_obj = p["cluster"]
                    _w(f, "<section class='details-section'>")
                    _w(f, f"<h4>{escape(UI['cluster_info'])}</h4>")
                    _w(f, "<div class='kv-grid'>")
                    _w(f, f"<div><strong>id</strong><div>{escape(_safe_str(cluster_obj.get('id')))}</div></div>")
                    _w(f, f"<div><strong>label</strong><div>{escape(_safe_str(cluster_obj.get('label')))}</div></div>")
                    _w(f, f"<div><strong>{escape(UI['size'])}</strong><div>{escape(_safe_str(cluster_obj.get('size')))}</div></div>")
                    _w(f, f"<div><strong>{escape(UI['source'])}</strong><div>{escape(_safe_str(cluster_obj.get('source')))}</div></div>")
                    _w(f, f"<div><strong>{escape(UI['label_source'])}</strong><div>{escape(_safe_str(cluster_obj.get('label_source')))}</div></div>")
                    _w(f, "</div>")
                    _w(f, "</section>")

                if include_local_terms:
                    _w(f, "<section class='details-section'>")
                    _w(f, f"<h4>{escape(UI['local_analysis'])}</h4>")
                    _w(f, _render_term_details(local_details))
                    _w(f, "</section>")

                if include_global_terms:
                    _w(f, "<section class='details-section'>")
                    _w(f, f"<h4>{escape(UI['global_analysis'])}</h4>")
                    _w(f, _render_term_details(global_details))
                    _w(f, "</section>")

                _w(f, "<section class='details-section'>")
                _w(f, f"<h4>{escape(UI['source_preview'])}</h4>")
                _w(f, _render_source_preview(p["source"]))
                _w(f, "</section>")

                _w(f, "</details>")
                _w(f, "</article>")

            _w(f, "</div>")
            _w(f, "</section>")

        if not has_content:
            _w(f, f"<div class='muted'>{escape(UI['no_content'])}</div>")

        _w(f, "</div>")
        _w(f, f"<div class='footer-note'>{escape(UI['generated_without_dependencies'])}</div>")
        _w(f, "</main>")
        _w(f, "</div>")

        # Inline JS for search and interactive filtering
        _w(f, "<script>")
        _w(f, f"""
(function() {{
  const searchInput = document.getElementById("searchInput");
  const clusterSelect = document.getElementById("clusterSelect");
  const resetFiltersBtn = document.getElementById("resetFiltersBtn");
  const expandAllBtn = document.getElementById("expandAllBtn");
  const resultInfo = document.getElementById("resultInfo");

  const docCards = Array.from(document.querySelectorAll(".doc-card"));
  const clusterSections = Array.from(document.querySelectorAll(".cluster-section"));

  let activeTerm = "";

  function normalize(value) {{
    return (value || "").toLowerCase().trim();
  }}

  function getAllTermButtons() {{
    // Re-querying keeps the logic simple and ensures we always work with the
    // current DOM state, even if the file is later enhanced.
    return Array.from(document.querySelectorAll(".term-badge"));
  }}

  function syncActiveTermUI() {{
    const termButtons = getAllTermButtons();
    termButtons.forEach(btn => {{
      const btnTerm = normalize(btn.dataset.term || btn.textContent || "");
      btn.classList.toggle("active-term", !!activeTerm && btnTerm === activeTerm);
    }});
  }}

  function applyFilters() {{
    const search = normalize(searchInput ? searchInput.value : "");
    const selectedCluster = normalize(clusterSelect ? clusterSelect.value : "");

    let visibleDocs = 0;

    docCards.forEach(card => {{
      const cardCluster = normalize(card.dataset.cluster || "");
      const cardTerms = normalize(card.dataset.terms || "");
      const cardSearch = normalize(card.dataset.search || "");

      const clusterOk = !selectedCluster || cardCluster === selectedCluster;
      const searchOk = !search || cardSearch.includes(search);
      const termOk = !activeTerm || cardTerms.includes(activeTerm);

      const visible = clusterOk && searchOk && termOk;
      card.classList.toggle("hidden", !visible);

      if (visible) {{
        visibleDocs += 1;
      }}
    }});

    clusterSections.forEach(section => {{
      const visibleChild = section.querySelector(".doc-card:not(.hidden)");
      section.classList.toggle("hidden", !visibleChild);
    }});

    const visibleClusters = document.querySelectorAll(".cluster-section:not(.hidden)").length;

    let info = `${{visibleDocs}} {UI['documents_visible']}, ${{visibleClusters}} {UI['clusters_visible']}`;
    if (activeTerm) {{
      info += `, {UI['active_term']}: "${{activeTerm}}"`;
    }}
    resultInfo.textContent = info;

    syncActiveTermUI();
  }}

  function setActiveTerm(term) {{
    activeTerm = normalize(term);
    applyFilters();
  }}

  document.addEventListener("click", function(event) {{
    const btn = event.target.closest(".term-badge");
    if (!btn) return;

    const clickedTerm = normalize(btn.dataset.term || btn.textContent || "");
    if (!clickedTerm) return;

    if (activeTerm === clickedTerm) {{
      setActiveTerm("");
    }} else {{
      setActiveTerm(clickedTerm);
    }}
  }});

  if (searchInput) {{
    searchInput.addEventListener("input", applyFilters);
  }}

  if (clusterSelect) {{
    clusterSelect.addEventListener("change", applyFilters);
  }}

  if (resetFiltersBtn) {{
    resetFiltersBtn.addEventListener("click", function() {{
      if (searchInput) searchInput.value = "";
      if (clusterSelect) clusterSelect.value = "";
      setActiveTerm("");
    }});
  }}

  if (expandAllBtn) {{
    expandAllBtn.addEventListener("click", function() {{
      const details = Array.from(document.querySelectorAll(".details-block"));
      const shouldOpen = details.some(d => !d.open);
      details.forEach(d => {{
        d.open = shouldOpen;
      }});
    }});
  }}

  applyFilters();
}})();
        """)
        _w(f, "</script>")

        _w(f, "</body>")
        _w(f, "</html>")

    return str(output)