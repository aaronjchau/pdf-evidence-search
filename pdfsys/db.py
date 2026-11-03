import os

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id        uuid PRIMARY KEY,
  engagement_id bigint NOT NULL,
  file_name     text,
  file_sha256   text,
  status        text NOT NULL,
  attempts      int NOT NULL DEFAULT 0,
  error         text,
  pages         int,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_engagement_sha ON documents (engagement_id, file_sha256);

CREATE TABLE IF NOT EXISTS pages (
  doc_id  uuid NOT NULL REFERENCES documents ON DELETE CASCADE,
  page_no int NOT NULL,
  html    text NOT NULL,
  text    text NOT NULL,
  search  tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  PRIMARY KEY (doc_id, page_no)
);
CREATE INDEX IF NOT EXISTS pages_search ON pages USING gin (search);
"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
