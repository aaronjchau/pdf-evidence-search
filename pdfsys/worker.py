import hashlib

from .db import connect
from .ocr import pdf_to_pages

MAX_TRIES = 3


def process(doc_id, pdf_path, dlq):
    with connect() as conn:
        doc = conn.execute(
            "UPDATE documents SET status = 'processing', attempts = attempts + 1, updated_at = now()"
            " WHERE doc_id = %s RETURNING engagement_id, attempts", (doc_id,)).fetchone()
        try:
            with open(pdf_path, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
            conn.execute("UPDATE documents SET file_sha256 = %s WHERE doc_id = %s", (sha, doc_id))
            same = conn.execute(
                "SELECT doc_id FROM documents WHERE engagement_id = %s AND file_sha256 = %s AND status = 'done' LIMIT 1",
                (doc["engagement_id"], sha)).fetchone()
            pages = [] if same else pdf_to_pages(pdf_path)
            with conn.transaction():
                conn.execute("DELETE FROM pages WHERE doc_id = %s", (doc_id,))
                if same:
                    conn.execute("INSERT INTO pages (doc_id, page_no, html, text)"
                                 " SELECT %s, page_no, html, text FROM pages WHERE doc_id = %s", (doc_id, same["doc_id"]))
                with conn.cursor() as cur:
                    cur.executemany("INSERT INTO pages (doc_id, page_no, html, text) VALUES (%s, %s, %s, %s)",
                                    [(doc_id, *page) for page in pages])
                conn.execute("UPDATE documents SET status = 'done', error = NULL, updated_at = now(),"
                             " pages = (SELECT count(*) FROM pages WHERE doc_id = %s) WHERE doc_id = %s", (doc_id, doc_id))
        except Exception as e:
            failed = doc["attempts"] >= MAX_TRIES
            conn.execute("UPDATE documents SET status = %s, error = %s, updated_at = now() WHERE doc_id = %s",
                         ("failed" if failed else "queued", f"{type(e).__name__}: {e}"[:500], doc_id))
            if failed:
                dlq.put(doc_id)
            raise
