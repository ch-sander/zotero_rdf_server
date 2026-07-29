from fastapi import FastAPI
from html import escape
from .lifespan import app_lifespan
from .api import router, include_plugins, open_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi import Request
from .config import STATIC_UI_DIRECTORY, ROOT_PATH, STATIC_UI_PREFIX, INCLUDE_OPEN_ROUTER, INCLUDE_CLOSED_ROUTER, INCLUDE_PLUGINS, FASTAPI_META, API_UI_URL, logger, ROOT_REDIRECT, ZOT_ONTOLOGY_TTL, STATIC_ONTOLOGY_MOUNT


_title = FASTAPI_META.get('title', "Zotero RDF Server App")
_version = FASTAPI_META.get('version', "0.1.0")
logger.info(
    f"\n\n{'*'*50}\n"
    f"Starting App\n"
    f"{_title}\n"
    f"version: {_version}\n"
    f"root: {ROOT_PATH}\n"
    f"{'*'*50}\n\n"
)

app = FastAPI(lifespan=app_lifespan, docs_url=API_UI_URL, root_path=ROOT_PATH if ROOT_PATH else None, **FASTAPI_META if isinstance(FASTAPI_META, dict) else None)    

ui_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if INCLUDE_CLOSED_ROUTER:
    logger.warning("Proceed --> INCLUDE_CLOSED_ROUTER = True")
    app.include_router(router)
else:
    logger.warning("Skip --> INCLUDE_CLOSED_ROUTER = False")

if INCLUDE_OPEN_ROUTER:
    logger.warning("Proceed --> INCLUDE_OPEN_ROUTER = True")
    app.include_router(open_router)
else:
    logger.warning("Skip --> INCLUDE_OPEN_ROUTER = False")

if INCLUDE_PLUGINS:
    logger.warning("Proceed --> INCLUDE_PLUGINS = True")
    include_plugins(app)
else:
    logger.warning("Skip --> INCLUDE_PLUGINS = False")

if (
    STATIC_ONTOLOGY_MOUNT
    and ZOT_ONTOLOGY_TTL
    and ZOT_ONTOLOGY_TTL.is_file()
):
    ont_route = f"{STATIC_ONTOLOGY_MOUNT.rstrip('/')}/ttl"

    logger.info(
        "Adding route at %s for %s",
        ont_route,
        ZOT_ONTOLOGY_TTL,
    )

    app.add_api_route(
        path=ont_route,
        endpoint=lambda: FileResponse(
            path=ZOT_ONTOLOGY_TTL,
            media_type="text/turtle",
            filename=ZOT_ONTOLOGY_TTL.name,
            content_disposition_type="inline",
        ),
        methods=["GET", "HEAD"],
        name="ontology-ttl",
        response_class=FileResponse,
    )
else:
    logger.warning(
        "Ontology TTL file not found: %s",
        ZOT_ONTOLOGY_TTL,
    )

if STATIC_UI_PREFIX and STATIC_UI_DIRECTORY:
    if STATIC_UI_PREFIX in {"","/"}:
        logger.warning(f"Static files mounted at: {STATIC_UI_PREFIX} -- VERY RISKY!")
    else:
        logger.info(f"Static files mounted at: {STATIC_UI_PREFIX}")
    ui_app.mount("/", StaticFiles(directory=STATIC_UI_DIRECTORY, html=True,check_dir=False), name="User Interfaces")
    app.mount(STATIC_UI_PREFIX, ui_app, name="User Interfaces")
else:
    logger.warning("No static files mount!")

def join_url(root_path: str, path: str | None) -> str | None:
    if not path:
        return None

    root_path = (root_path or "").rstrip("/")
    path = "/" + path.strip("/")

    return root_path + path

@app.get("/", include_in_schema=False)
async def root(request: Request):
    if ROOT_REDIRECT:
        return RedirectResponse(ROOT_REDIRECT)
    
    root_path = request.scope.get("root_path", "")
    ui_url = join_url(root_path, STATIC_UI_PREFIX)
    api_docs_url = join_url(root_path, API_UI_URL)

    return HTMLResponse(f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <title>Available Interfaces</title>
      </head>
      <body>
        <h1>Available Interfaces</h1>

        <ul>
          {f'<li><a href="{escape(ui_url)}">User Interfaces</a></li>' if ui_url else ''}
          {f'<li><a href="{escape(api_docs_url)}">API Documentation</a></li>' if api_docs_url else ''}
        </ul>
      </body>
    </html>
    """)