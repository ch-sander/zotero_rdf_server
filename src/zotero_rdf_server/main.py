from fastapi import FastAPI
from .lifespan import app_lifespan
from .api import router, include_plugins, open_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .config import STATIC_UI_DIRECTORY, ROOT_PATH, STATIC_UI_PREFIX, INCLUDE_OPEN_ROUTER, INCLUDE_CLOSED_ROUTER, INCLUDE_PLUGINS, FASTAPI_META, API_UI_URL, logger


_title = FASTAPI_META.get('title', "Zotero RDF Server App")
logger.info(
    f"\n\n{'*'*50}\n"
    f"Starting App\n"
    f"{_title}\n"
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

if STATIC_UI_PREFIX and STATIC_UI_DIRECTORY:
    if STATIC_UI_PREFIX in {"","/"}:
        logger.warning(f"Static files mounted at: {STATIC_UI_PREFIX} -- VERY RISKY!")
    else:
        logger.info(f"Static files mounted at: {STATIC_UI_PREFIX}")
    ui_app.mount("/", StaticFiles(directory=STATIC_UI_DIRECTORY, html=True,check_dir=False), name="User Interfaces")
    app.mount(STATIC_UI_PREFIX, ui_app, name="User Interfaces")
else:
    logger.warning("No static files mount!")