import io
import os
import time

import modal

app = modal.App("pdf-evidence-search")
storage = modal.Volume.from_name("pdf-evidence-search-data", create_if_missing=True)
dead_letters = modal.Queue.from_name("pdf-evidence-search-dlq", create_if_missing=True)
counts = modal.Dict.from_name("pdf-evidence-search-limits", create_if_missing=True)
database = modal.Secret.from_name("pdf-demo-db")

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("tesseract-ocr")
    .pip_install("psycopg[binary]==3.2.3", "fastapi[standard]==0.162.0", "anthropic==2.4.0", "beautifulsoup4==4.12.3",
                 "pdfplumber==0.11.10", "pypdfium2==5.13.0", "pytesseract==0.3.13", "pillow==12.3.0")
    .add_local_python_source("pdfsys")
    .add_local_dir("web", "/root/web")
)


@app.function(image=image, volumes={"/data": storage}, secrets=[database], cpu=4, memory=4096, timeout=900,
              max_containers=4, retries=modal.Retries(max_retries=2, initial_delay=5.0, backoff_coefficient=2.0))
def ocr_worker(doc_id):
    from pdfsys.worker import process

    storage.reload()
    process(doc_id, f"/data/pdfs/{doc_id}.pdf", dead_letters)


@app.function(image=image, secrets=[database, modal.Secret.from_name("jobsweep-anthropic")],
              max_containers=3, timeout=90, scaledown_window=1200)
def query_service(doc_id, question):
    from pdfsys.query import answer

    day = "asks-" + time.strftime("%Y-%m-%d")
    counts[day] = counts.get(day, 0) + 1
    if counts[day] > 500:
        return {"answer": "The demo's question limit for today is used up.", "cited": [], "pages": []}
    return answer(doc_id, question)


@app.function(image=image, volumes={"/data": storage}, secrets=[database, modal.Secret.from_name("pdf-demo-signing")],
              max_containers=2, scaledown_window=1200)
@modal.concurrent(max_inputs=16)
@modal.asgi_app()
def web():
    from pdfsys.api import make_api

    def save_pdf(doc_id, data):
        path = f"/data/pdfs/{doc_id}.pdf"
        if os.path.exists(path):
            return False
        os.makedirs("/data/pdfs", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        storage.commit()
        return True

    def page_image(doc_id, page_no):
        import pypdfium2

        path = f"/data/pdfs/{doc_id}.pdf"
        if not os.path.exists(path):
            storage.reload()
        if not os.path.exists(path):
            return None
        pdf = pypdfium2.PdfDocument(path)
        if not 1 <= page_no <= len(pdf):
            return None
        png = io.BytesIO()
        pdf[page_no - 1].render(scale=2).to_pil().save(png, "PNG")
        return png.getvalue()

    return make_api(save_pdf=save_pdf, queue=ocr_worker.spawn, ask=query_service.remote, page_image=page_image,
                    web_dir="/root/web")
