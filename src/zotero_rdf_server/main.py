from fastapi import FastAPI
from .lifespan import app_lifespan
from .api import router, include_plugins, open_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .config import STATIC_UI_DIRECTORY, ROOT_PATH

app = FastAPI(lifespan=app_lifespan, docs_url="/", root_path=ROOT_PATH if ROOT_PATH else None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(router)
app.include_router(open_router)

app.mount("/ui", StaticFiles(directory=STATIC_UI_DIRECTORY, html=True,check_dir=False), name="User Interfaces")

include_plugins(app)