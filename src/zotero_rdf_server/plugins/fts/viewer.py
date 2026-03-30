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
    cfg = load_dict_like(config_path, label="Viewer Config", verbose=True)
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
BASE_IMAGES = cfg.get("dashboard_url", "/image-files")

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


def discover_doc_url(original_os_doc_id: str, page: str) -> str | None:
    """
    Discover URL should use the original document ID semantics, not the sanitized folder name.
    """
    dashboard_url = DASHBOARD_URL
    index_pattern_id = DEFAULT_ALIAS

    if not dashboard_url or not index_pattern_id:
        return None

    doc_id = f"{original_os_doc_id}:{page}"
    return f"{dashboard_url.rstrip('/')}/app/discover#/doc/{index_pattern_id}/{doc_id}"


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
    plugin_prefix = "/plugin/fts/view"
    options = []
    for p in pages:
        selected = " selected" if p == page else ""
        label = str(int(p)) if p.isdigit() else p
        options.append(
            f'<option value="{plugin_prefix}/{safe_os_doc_id}:{escape(p)}"{selected}>{escape(label)}</option>'
        )
    options_html = "\n".join(options)

    nav_parts = []
    if prev_page:
        nav_parts.append(
            f'<a href="{plugin_prefix}/{safe_os_doc_id}:{escape(prev_page)}">Previous</a>'
        )
    if next_page:
        nav_parts.append(
            f'<a href="{plugin_prefix}/{safe_os_doc_id}:{escape(next_page)}">Next</a>'
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