from fastapi import FastAPI
from .lifespan import app_lifespan
from .api import router, include_plugins, open_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .config import STATIC_UI_DIRECTORY, ROOT_PATH, STATIC_UI_PREFIX, INCLUDE_OPEN_ROUTER, INCLUDE_CLOSED_ROUTER, INCLUDE_PLUGINS, FASTAPI_META, API_UI_URL, logger

try:
    _title = FASTAPI_META.get('title', "Zotero RDF Server App")
    logger.info(
        f"\n\n{'*'*50}\n"
        f"Starting App\n"
        f"{_title}\n"
        f"{'*'*50}\n\n"
    )
    app = FastAPI(lifespan=app_lifespan, docs_url=API_UI_URL, root_path=ROOT_PATH if ROOT_PATH else None, **FASTAPI_META if isinstance(FASTAPI_META, dict) else None)    
except Exception as e:
    logger.critical(f"Failed to start app: {e}")
    logger.info("Starting app with minimal config")
    app = FastAPI(lifespan=app_lifespan, docs_url="/")

ui_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)



if INCLUDE_CLOSED_ROUTER: app.include_router(router)
if INCLUDE_OPEN_ROUTER: app.include_router(open_router)
if INCLUDE_PLUGINS: include_plugins(app)

if STATIC_UI_PREFIX and STATIC_UI_DIRECTORY:
    if STATIC_UI_PREFIX in {"","/"}:
        logger.warning(f"Static files mounted at: {STATIC_UI_PREFIX} -- VERY RISKY!")
    else:
        logger.info(f"Static files mounted at: {STATIC_UI_PREFIX}")
    app.mount(STATIC_UI_PREFIX, StaticFiles(directory=STATIC_UI_DIRECTORY, html=True,check_dir=False), name="User Interfaces")
    # app.mount(STATIC_UI_PREFIX, ui_app, name="User Interfaces")
else:
    logger.warning("No static files mount!")