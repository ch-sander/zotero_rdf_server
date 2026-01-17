from fastapi import FastAPI
from .lifespan import app_lifespan
from .api import router, include_plugins
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(lifespan=app_lifespan, docs_url="/")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

include_plugins(app)