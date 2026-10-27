"""Runbook retrieval: chunking, embedding and hybrid (lexical + vector) search in Postgres.

The default embedder is a deterministic feature-hashing model. It needs no download and no API
key, so CI and offline/on-prem runs behave identically. It is a baseline, not a semantic model:
swapping in a real embedding model only means implementing ``Embedder`` with the same dimension.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

DIM = 256
_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Signed feature hashing over unigrams and bigrams, L2-normalised."""

    def embed(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
        vec = [0.0] * DIM
        for feat in features:
            digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % DIM
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


@dataclass(frozen=True)
class Chunk:
    doc: str
    chunk_no: int
    heading: str
    content: str


def chunk_markdown(doc: str, text: str) -> list[Chunk]:
    """One chunk per ``##`` section: runbook sections are already self-contained answers."""
    chunks: list[Chunk] = []
    heading, lines = "", list[str]()
    for line in [*text.splitlines(), "## "]:
        if line.startswith("## "):
            body = "\n".join(lines).strip()
            if heading and body:
                chunks.append(Chunk(doc, len(chunks), heading, body))
            heading, lines = line[3:].strip(), []
        elif not line.startswith("# "):
            lines.append(line)
    return chunks


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def ingest_runbooks(pool: ConnectionPool, folder: Path, embedder: Embedder) -> int:
    count = 0
    with pool.connection() as conn, conn.transaction():
        for path in sorted(folder.glob("*.md")):
            for c in chunk_markdown(path.name, path.read_text(encoding="utf-8")):
                conn.execute(
                    """
                    INSERT INTO runbook_chunks (doc, chunk_no, heading, content, embedding)
                    VALUES (%s, %s, %s, %s, %s::vector)
                    ON CONFLICT (doc, chunk_no) DO UPDATE
                    SET heading = EXCLUDED.heading, content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding
                    """,
                    (
                        c.doc,
                        c.chunk_no,
                        c.heading,
                        c.content,
                        _vector_literal(embedder.embed(f"{c.heading} {c.content}")),
                    ),
                )
                count += 1
    return count


class Passage(BaseModel):
    source: str
    """Citation id, ``<doc>#<chunk_no>``. The agent must cite only ids returned here."""
    heading: str
    content: str
    score: float


HYBRID_SQL = """
WITH lexical AS (
    SELECT doc, chunk_no,
           row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
    FROM runbook_chunks, websearch_to_tsquery('english', %(q)s) AS q
    WHERE tsv @@ q
    ORDER BY r LIMIT 20
),
semantic AS (
    SELECT doc, chunk_no,
           row_number() OVER (ORDER BY embedding <=> %(v)s::vector) AS r
    FROM runbook_chunks
    ORDER BY r LIMIT 20
),
fused AS (  -- reciprocal rank fusion: robust to the two scores living on different scales
    SELECT doc, chunk_no, sum(1.0 / (60 + r)) AS score
    FROM (SELECT * FROM lexical UNION ALL SELECT * FROM semantic) s
    GROUP BY doc, chunk_no
)
SELECT c.doc, c.chunk_no, c.heading, c.content, f.score
FROM fused f JOIN runbook_chunks c USING (doc, chunk_no)
ORDER BY f.score DESC
LIMIT %(k)s
"""


def search(pool: ConnectionPool, query: str, embedder: Embedder, k: int = 3) -> list[Passage]:
    params = {"q": query, "v": _vector_literal(embedder.embed(query)), "k": k}
    with pool.connection() as conn:
        rows = conn.execute(HYBRID_SQL, params).fetchall()
    return [
        Passage(source=f"{doc}#{no}", heading=h, content=c, score=round(float(s), 5))
        for doc, no, h, c, s in rows
    ]
