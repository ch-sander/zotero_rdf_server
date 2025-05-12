from fastapi import FastAPI
from .lifespan import app_lifespan
from .api import router

app = FastAPI(lifespan=app_lifespan, docs_url="/")
app.include_router(router)