# FastAPI на хостинге airlok: airlok.yml не нужен, команда запуска определяется сама.
import logging
import os

from fastapi import FastAPI

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="airlok FastAPI")


@app.get("/")
def index():
    return {"hello": "airlok", "greeting": os.environ.get("GREETING", "")}


@app.get("/boom")
def boom():
    log.error("пример ошибки для логов airlok")
    raise RuntimeError("пример исключения")
