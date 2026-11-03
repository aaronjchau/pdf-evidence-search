import hashlib
import hmac
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .db import SCHEMA, connect

ENGAGEMENT = 1
MAX_MB = 20
UPLOADS_PER_DAY = 200


class NewDocument(BaseModel):
    file_name: str


class Question(BaseModel):
    question: str


def sign(doc_id, expires):
    key = os.environ["UPLOAD_SIGNING_KEY"].encode()
    return hmac.new(key, f"{doc_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def make_api(save_pdf, queue, ask, page_image, web_dir):
    with connect() as conn:
        conn.execute(SCHEMA)
    api = FastAPI()

    @api.get("/")
    def home():
        return FileResponse(f"{web_dir}/index.html")

    @api.post("/documents", status_code=201)
    def create_document(body: NewDocument):
        doc_id = str(uuid.uuid4())
        with connect() as conn:
            today = conn.execute("SELECT count(*) FROM documents WHERE created_at > now() - interval '1 day'").fetchone()
            if today["count"] >= UPLOADS_PER_DAY:
                raise HTTPException(429, "The demo's upload limit for today is used up.")
            conn.execute("INSERT INTO documents (doc_id, engagement_id, file_name, status) VALUES (%s, %s, %s, 'queued')",
                         (doc_id, ENGAGEMENT, body.file_name[:200]))
        expires = int(time.time()) + 600
        return {"doc_id": doc_id, "upload_url": f"/documents/{doc_id}/upload?expires={expires}&sig={sign(doc_id, expires)}"}

    @api.put("/documents/{doc_id}/upload")
    async def upload(doc_id: uuid.UUID, expires: int, sig: str, request: Request):
        if expires < time.time() or not hmac.compare_digest(sig, sign(doc_id, expires)):
            raise HTTPException(403, "This upload URL is wrong or has expired.")
        data = await request.body()
        if len(data) > MAX_MB * 2**20 or not data.startswith(b"%PDF"):
            raise HTTPException(400, f"Send one PDF of up to {MAX_MB} MB.")
        if not await run_in_threadpool(save_pdf, str(doc_id), data):
            raise HTTPException(409, "This document already has its file.")
        await run_in_threadpool(queue, str(doc_id))
        return {"doc_id": doc_id, "status": "queued"}

    @api.get("/documents/{doc_id}")
    def status(doc_id: uuid.UUID):
        with connect() as conn:
            doc = conn.execute("SELECT doc_id, file_name, status, attempts, error, pages FROM documents WHERE doc_id = %s",
                               (doc_id,)).fetchone()
        if doc is None:
            raise HTTPException(404, "No such document.")
        return doc

    @api.get("/documents/{doc_id}/pages/{page_no}")
    def page(doc_id: uuid.UUID, page_no: int):
        with connect() as conn:
            found = conn.execute("SELECT page_no, html FROM pages WHERE doc_id = %s AND page_no = %s",
                                 (doc_id, page_no)).fetchone()
        if found is None:
            raise HTTPException(404, "No such page.")
        return found

    @api.get("/documents/{doc_id}/pages/{page_no}/image")
    def image(doc_id: uuid.UUID, page_no: int):
        png = page_image(str(doc_id), page_no)
        if png is None:
            raise HTTPException(404, "No such page.")
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @api.post("/documents/{doc_id}/ask")
    def ask_question(doc_id: uuid.UUID, body: Question):
        return ask(str(doc_id), body.question.strip()[:300])

    return api
