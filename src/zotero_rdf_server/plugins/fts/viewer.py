from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from zotero_rdf_server.config import STATIC_UI_PREFIX
from .helpers import plugin_logger, resolve_config_path, safe_doc_id
from zotero_rdf_server.main import app
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

IMAGE_ROOT_STR = cfg.get("image_root") or ""
TEXT_ROOT_STR = cfg.get("text_root") or ""
IMAGE_EXT = str(cfg.get("image_ext") or "jpg").lstrip(".")
TEXT_EXT = str(cfg.get("text_ext") or "txt").lstrip(".")
DASHBOARD_URL = cfg.get("dashboard_url") or None
# VIEW_URLS = cfg.get("viewer_urls") or {
#               'view': app.url_path_for("view", os_doc_id="doc_id") or "/plugin/fts/view",
#               # 'edit-view': router.url_path_for("edit-view"),
#               # 'save-view': router.url_path_for("save-view"),
#               # 'ocr-view': open_router.url_path_for("ocr-view"),
#               # 'text': open_router.url_path_for("save-view"),
#               # 'image': open_router.url_path_for("save-view"),
#              } 
# BASE_URL = str(cfg.get("base_url", "/plugin/fts")).rstrip("/")
# BASE_URL = f"{ROOT_PATH}/{BASE_URL.lstrip(str(ROOT_PATH))}"
STATIC_URL = str(cfg.get("static_url", f"{STATIC_UI_PREFIX}/view")).rstrip("/") 
ATLAS_URL = str(cfg.get("atlas_url", "/ui/atlas")).rstrip("/") 
OSD_CONFIG = cfg.get("OpenSeadragon") or {}
OCR_FRAMEWORKS = cfg.get("ocr_frameworks") or []

MOUNT_PATH = "/image-files"

if not IMAGE_ROOT_STR:
    logger.warning("viewer.image_root is not configured")
if not TEXT_ROOT_STR:
    logger.warning("viewer.text_root is not configured")

image_root = Path(IMAGE_ROOT_STR) if IMAGE_ROOT_STR else Path(".")
text_root = Path(TEXT_ROOT_STR) if TEXT_ROOT_STR else Path(".")


def ensure_router_mount(app, mount_path = MOUNT_PATH) -> None:
    """
    Mount static files once.
    Call this during router/app setup, not inside the route handler.
    """
    for route in getattr(app, "routes", []):
        if getattr(route, "path", None) == mount_path:
            return
    
    app.mount(
        mount_path,
        StaticFiles(directory=str(image_root),check_dir=False),
        name="image-files",
    )
    logger.info(f"Mounted static image path at {mount_path} -> {image_root}")


ensure_router_mount(app)

for route in app.routes:
    logger.info(f"name={getattr(route, 'name', None)} path={getattr(route, 'path', None)}")

def add_viewer_url(hits: list) -> list:
    for h in hits:
        _id = h.get("_id")
        src = h.get("_source")
        if _id and src:
            src['viewer'] = app.url_path_for("view", os_doc_id=_id)
    return hits

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
    editable: bool = False,
    save_url: str | None = None,
    page_url_base: str | None = None,
    edit_url: str | None = None,
    ocr_url: str | None = None,
    current_framework: str = "kraken",
    root_path: str = ""
) -> str:
    import json
    from html import escape

    raw_text = text or ""
    has_real_text = bool(raw_text and raw_text != "[no text on this page]")

    editor_text = raw_text if has_real_text else ""
    display_text = raw_text if has_real_text else "[no text on this page]"

    
    safe_os_doc_id = escape(os_doc_id)
    safe_page = escape(page)
    safe_editor_text = escape(editor_text)
    safe_display_text = escape(display_text)
    safe_save_url = escape(save_url or "")
    safe_page_url_base = escape((page_url_base).rstrip("/"))
    safe_edit_url = escape(edit_url or "")
    safe_discover_url = escape(discover_url or "")
    safe_static_url = f"{root_path}/{STATIC_URL.lstrip('/')}" if STATIC_URL and root_path else STATIC_URL
    safe_image_url = f"{root_path}/{image_url.lstrip('/')}" if image_url and root_path else image_url

    options = []
    for p in pages:
        selected = " selected" if p == page else ""
        label = str(int(p)) if p.isdigit() else p
        options.append(
            f'<option value="{safe_page_url_base}/{safe_os_doc_id}:{escape(p)}"{selected}>{escape(label)}</option>'
        )
    options_html = "\n".join(options)

    nav_parts = []
    if prev_page:
        nav_parts.append(
            f'<a href="{safe_page_url_base}/{safe_os_doc_id}:{escape(prev_page)}">Previous</a>'
        )
    if next_page:
        nav_parts.append(
            f'<a href="{safe_page_url_base}/{safe_os_doc_id}:{escape(next_page)}">Next</a>'
        )
    if (not editable) and edit_url:
        nav_parts.append(f'<a href="{safe_edit_url}">Edit</a>')
    nav_html = "\n".join(nav_parts)

    discover_html = ""
    if discover_url:
        discover_html = (
            f'<a href="{safe_discover_url}" target="_blank" '
            f'rel="noopener noreferrer">Open in Discover</a>'
        )

    ocr_controls_html = ""
    if image_url and ocr_url:
        frameworks = OCR_FRAMEWORKS or [
            {"value": "kraken", "label": "Kraken"},
            {"value": "tesseract", "label": "Tesseract"},
            {"value": "transformer", "label": "Transformer"},
            {"value": "source", "label": "From Source"},
        ]
        framework_options = []
        for fw in frameworks:
            value = str(fw.get("value", "")).strip()
            label = str(fw.get("label", value)).strip()
            if not value:
                continue
            selected = " selected" if value == current_framework else ""
            framework_options.append(
                f'<option value="{escape(value)}"{selected}>{escape(label)}</option>'
            )
        framework_options_html = "\n".join(framework_options)

        ocr_controls_html = f"""
        <div class="ocr-tools">
          <label for="ocr-framework">OCR</label>
          <select id="ocr-framework">
            {framework_options_html}
          </select>
          <button type="button" id="rerun-ocr-btn">OCR</button>
          <span id="ocr-status" class="ocr-status"></span>
        </div>
        """

    if editable and save_url:
        text_html = f"""
        <div class="text-content">
          {ocr_controls_html}
          <form method="post" action="{safe_save_url}" class="text-form">
            <textarea id="page-text" name="text" class="editor">{safe_editor_text}</textarea>
            <div class="editor-actions">
              <button type="submit">Save</button>
            </div>
          </form>
        </div>
        """
    else:
        text_html = f"""
        <div class="text-content">
          {ocr_controls_html}
          <div id="page-text-display" class="text">{safe_display_text}</div>
        </div>
        """

    if image_url:
        viewer_html = '<div id="osd"></div>'
    else:
        viewer_html = '<div class="viewer-empty">No image available.</div>'

    osd_config = dict(OSD_CONFIG or {})
    osd_script_src = osd_config.pop(
        "src",
        "https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/openseadragon.min.js",
    )
    parts = os_doc_id.split(":", 1)
    doc_prefix = parts[0]

    viewer_config = {
        "editable": editable,
        "imageUrl": safe_image_url or "",
        "ocrUrl": ocr_url or "",
        "currentFramework": current_framework,
        "osdConfig": osd_config,
        "pageUrlBase": safe_page_url_base,
        "currentDocId": os_doc_id,
        "currentPage": page,
        "docPrefix": doc_prefix,
    }
    
    viewer_config_json = (json.dumps(viewer_config))
    safe_osd_script_src = escape(osd_script_src)

    viewer_open_attr = " open" if image_url else ""
    title = f"{safe_os_doc_id}:{safe_page}" if safe_os_doc_id and safe_page else "Viewer"

    return f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{title}</title>
      <link rel="stylesheet" href="{safe_static_url}/viewer.css">
      <script src="{safe_osd_script_src}"></script>
      <script id="viewer-config" type="application/json">{viewer_config_json}</script>
      <script src="{safe_static_url}/viewer.js" defer></script>
    </head>
    <body>
      <div id="viewer-root">
        <header>
          <div><strong>{title}</strong></div>
          <nav>
            <form id="page-jump-form" class="jump-to-page">
            <input
              type="text"
              id="page-input"
              placeholder="Enter page or doc ID"
              title="Enter:
          - page (e.g. 22)
          - suffix (e.g. 26WV4HVJ)
          - suffix:page (e.g. 26WV4HVJ:22)
          - full doc (e.g. 4929619:26WV4HVJ)
          - full doc:page (e.g. 4929619:26WV4HVJ:22)"
            />
            <button type="submit" id="page-go-btn">Go</button>
          </form>
            {"<label for='page-select'>Page</label>" if pages else ""}
            {"<select id='page-select'>" + options_html + "</select>" if pages else ""}
            {nav_html}


            {discover_html}
          </nav>
        </header>

        <div class="layout">
          <details class="panel viewer-panel"{viewer_open_attr}>
            <summary>Viewer</summary>
            {viewer_html}
          </details>

          <details class="panel text-panel" open>
            <summary>Text</summary>
            {text_html}
          </details>
        </div>
      </div>
    </body>
    </html>
    """

from zotero_rdf_server.config import STATIC_UI_DIRECTORY

def export_atlas_folder(
    inputs,
    output_dir: str = STATIC_UI_DIRECTORY / "atlas" ,
    *,
    text: str | None = "text",
    x_column: str | None = "projection_x",
    y_column: str | None = "projection_y",
    neighbors_column: str | None = "neighbors",
    point_size: float | None = None,
    stop_words: str | None = None,    
    export_metadata: dict | None = None,
):
    try:
        import embedding_atlas
        logger.info("Imported embedding_atlas")
    except ImportError:
        from .helpers import ensure_import
        ensure_import("embedding-atlas==0.20.0", requirements=None)        
        import embedding_atlas

    from embedding_atlas import __version__
    from embedding_atlas.data_source import DataSource
    from embedding_atlas.cli import find_column_name
    from embedding_atlas.options import make_embedding_atlas_props
    from embedding_atlas.utils import load_pandas_data
    from embedding_atlas.cache import sha256_hexdigest    
    import pandas as pd

    logger.info("Creating Embedding Atlas")

    def make_labels_from_clusters(
        df: pd.DataFrame,
        cluster_col: str = "cluster_id",
        x_col: str = "projection_x",
        y_col: str = "projection_y",
        terms_col: str = "cluster_label_terms",
    ) -> list[dict]:
        labels = []

        for cluster_id, group in df.groupby(cluster_col, sort=False):
            terms = group.iloc[0][terms_col]
            if not isinstance(terms, list):
                terms = [str(terms)] if terms is not None else []

            labels.append({
                "x": float(group[x_col].mean()),
                "y": float(group[y_col].mean()),
                "text": ", ".join(str(t) for t in terms if t) or f"Cluster {cluster_id}",
                "priority": int(len(group)),
            })

        return labels
    
    df = pd.DataFrame(inputs)
    logger.info(
        "before atlas id assignment: %r",
        df[["__row_index__", "neighbors", "meta_parent"]].head(10).to_dict(orient="records")
    )
    id_column = "__row_index__"
    if id_column not in df.columns:
        df[id_column] = range(df.shape[0])
    logger.info(
        "after atlas id assignment: %r",
        df[["__row_index__", "neighbors", "meta_parent"]].head(10).to_dict(orient="records")
    )
    stop_words_resolved = None
    if stop_words is not None:
        stop_words_df = pd.DataFrame(stop_words)
        stop_words_resolved = stop_words_df["word"].to_list()

    props = make_embedding_atlas_props(
        row_id=id_column,
        x=x_column,
        y=y_column,
        neighbors=neighbors_column,
        text=text,
        point_size=point_size,
        stop_words=["est","non","ut"],
        labels=make_labels_from_clusters(df),
    )

    metadata = {"props": props}

    identifier = sha256_hexdigest([__version__, inputs, metadata], scope="DataSource")
    
    try:
        logger.info("atlas: creating DataSource")
        dataset = DataSource(identifier, df, metadata)
    except Exception:
        logger.exception("atlas: DataSource failed")
        raise

    static = Path(embedding_atlas.__file__).resolve().parent / "static"
    dataset.export_to_folder(str(static), str(output_dir), export_metadata)

#  end