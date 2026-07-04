"""
Aurora Compliance — ingest pipeline for the EU AI Act.

Stages:
  1. FETCH    — download articles from artificialintelligenceact.eu (113 articles)
  2. EXTRACT  — LLM extracts Obligations / Roles / Exceptions / Deadlines per article
  3. EMBED    — chunk article text, embed for hybrid retrieval
  4. LOAD     — MERGE everything into Neo4j

Run:
  python ingest_aiact.py --stage all
  python ingest_aiact.py --stage fetch          # only download + cache
  python ingest_aiact.py --stage load           # only push cached JSON to Neo4j

Env:
  NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, ANTHROPIC_API_KEY, OPENAI_API_KEY (embeddings)
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

CACHE = Path("cache")
CACHE.mkdir(exist_ok=True)

BASE_URL = "https://artificialintelligenceact.eu/article/{n}/"
ANNEX_URL = "https://artificialintelligenceact.eu/annex/{n}/"
N_ARTICLES = 113
N_ANNEXES = 13

EXTRACTION_PROMPT = """You are a legal knowledge-graph extractor for the EU AI Act.
Given the full text of one article, return ONLY valid JSON (no markdown fences):

{{
  "obligations": [
    {{
      "id": "OBL-ART{n}-1",
      "summary": "<one-sentence obligation>",
      "roles": ["provider" | "deployer" | "importer" | "distributor"],
      "risk_category": "prohibited|high|limited|minimal|gpai|gpai_systemic|null",
      "exceptions": ["<summary of each exception, if any>"],
      "references": [<article numbers referenced>]
    }}
  ]
}}

If the article imposes no obligations (definitions, governance), return {{"obligations": []}}.

ARTICLE {n} — {title}
{text}
"""


# ----------------------------------------------------------------- fetch
def fetch_articles() -> list[dict]:
    out = []
    with httpx.Client(timeout=30, headers={"User-Agent": "AuroraCompliance/0.1 (research)"}) as client:
        for n in range(1, N_ARTICLES + 1):
            cached = CACHE / f"article_{n}.json"
            if cached.exists():
                out.append(json.loads(cached.read_text(encoding="utf-8")))
                continue
            resp = client.get(BASE_URL.format(n=n))
            if resp.status_code != 200:
                print(f"  ! article {n}: HTTP {resp.status_code}, skipping")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            title_el = soup.find("h1")
            content_el = soup.select_one(".et_pb_post_content") or soup.find("article") or soup.body
            title = re.sub(r"^Article \d+:\s*", "", title_el.get_text(strip=True)) if title_el else f"Article {n}"
            text = content_el.get_text(" ", strip=True)[:20000] if content_el else ""
            rec = {"number": n, "title": title, "text": text, "url": BASE_URL.format(n=n)}
            cached.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            out.append(rec)
            print(f"  fetched article {n}: {title[:60]}")
            time.sleep(0.5)  # be polite
    return out


ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII"]


def fetch_annexes() -> list[dict]:
    out = []
    with httpx.Client(timeout=30, headers={"User-Agent": "AuroraCompliance/0.1 (research)"}) as client:
        for n in range(1, N_ANNEXES + 1):
            cached = CACHE / f"annex_{n}.json"
            if cached.exists():
                out.append(json.loads(cached.read_text(encoding="utf-8")))
                continue
            resp = client.get(ANNEX_URL.format(n=n))
            if resp.status_code != 200:
                print(f"  ! annex {n}: HTTP {resp.status_code}, skipping")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            title_el = soup.find("h1")
            content_el = soup.select_one(".et_pb_post_content") or soup.find("article") or soup.body
            title = title_el.get_text(strip=True) if title_el else f"Annex {ROMAN[n-1]}"
            text = content_el.get_text(" ", strip=True)[:30000] if content_el else ""
            rec = {"roman": ROMAN[n - 1], "title": title, "text": text, "url": ANNEX_URL.format(n=n)}
            cached.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            out.append(rec)
            print(f"  fetched annex {ROMAN[n-1]}: {title[:60]}")
            time.sleep(0.5)
    return out


# ----------------------------------------------------------------- extract
def extract_entities(articles: list[dict]) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    for art in articles:
        cached = CACHE / f"extract_{art['number']}.json"
        if cached.exists():
            art["extraction"] = json.loads(cached.read_text(encoding="utf-8"))
            continue
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(n=art["number"], title=art["title"], text=art["text"][:12000]),
            }],
        )
        raw = msg.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            art["extraction"] = json.loads(raw)
        except json.JSONDecodeError:
            print(f"  ! article {art['number']}: bad JSON from LLM, storing empty")
            art["extraction"] = {"obligations": []}
        cached.write_text(json.dumps(art["extraction"], ensure_ascii=False), encoding="utf-8")
        print(f"  extracted article {art['number']}: {len(art['extraction']['obligations'])} obligations")
    return articles


# ----------------------------------------------------------------- embed
def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += size - overlap
    return chunks


def embed_articles(articles: list[dict]) -> list[dict]:
    from openai import OpenAI

    client = OpenAI()
    for art in articles:
        cached = CACHE / f"embed_{art['number']}.json"
        if cached.exists():
            art["chunks"] = json.loads(cached.read_text(encoding="utf-8"))
            continue
        chunks = chunk_text(art["text"])
        resp = client.embeddings.create(model="text-embedding-3-small", input=chunks)
        art["chunks"] = [
            {"id": f"A{art['number']}-C{i}", "text": c, "embedding": e.embedding}
            for i, (c, e) in enumerate(zip(chunks, resp.data))
        ]
        cached.write_text(json.dumps(art["chunks"]), encoding="utf-8")
        print(f"  embedded article {art['number']}: {len(chunks)} chunks")
    return articles


# ----------------------------------------------------------------- load
LOAD_ARTICLE = """
MERGE (a:Article {number: $number})
SET a.title = $title, a.text = $text, a.url = $url
"""

LOAD_OBLIGATION = """
MATCH (a:Article {number: $art})
MERGE (o:Obligation {id: $id}) SET o.summary = $summary
MERGE (a)-[:IMPOSES]->(o)
WITH o
UNWIND $roles AS role
  MERGE (r:Role {name: role})
  MERGE (o)-[:APPLIES_TO]->(r)
WITH o
CALL (o) {
  WITH o WHERE $risk IS NOT NULL
  MERGE (rc:RiskCategory {name: $risk})
  MERGE (o)-[:CONDITIONAL_ON]->(rc)
}
WITH o
UNWIND $exceptions AS exc
  MERGE (e:Exception {summary: exc})
  MERGE (o)-[:HAS_EXCEPTION]->(e)
"""

LOAD_REFERENCE = """
MATCH (a:Article {number: $src})
MERGE (b:Article {number: $dst})
MERGE (a)-[:REFERS_TO]->(b)
"""

LOAD_CHUNK = """
MATCH (a:Article {number: $art})
MERGE (c:Chunk {id: $id})
SET c.text = $text, c.embedding = $embedding, c.article_number = $art
MERGE (c)-[:CHUNK_OF]->(a)
"""

LOAD_ANNEX = """
MERGE (x:Annex {number: $roman})
SET x.title = $title, x.text = $text, x.url = $url
"""

LOAD_ANNEX_CHUNK = """
MATCH (x:Annex {number: $roman})
MERGE (c:Chunk {id: $id})
SET c.text = $text, c.embedding = $embedding, c.annex_number = $roman
MERGE (c)-[:CHUNK_OF]->(x)
"""


def load_annexes(annexes: list[dict]) -> None:
    from openai import OpenAI

    oa = OpenAI()
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    with driver.session() as s:
        for ax in annexes:
            s.run(LOAD_ANNEX, roman=ax["roman"], title=ax["title"], text=ax["text"], url=ax["url"])
            cached = CACHE / f"annex_embed_{ax['roman']}.json"
            if cached.exists():
                chunks = json.loads(cached.read_text(encoding="utf-8"))
            else:
                parts = chunk_text(ax["text"])
                resp = oa.embeddings.create(model="text-embedding-3-small", input=parts)
                chunks = [
                    {"id": f"AX{ax['roman']}-C{i}", "text": c, "embedding": e.embedding}
                    for i, (c, e) in enumerate(zip(parts, resp.data))
                ]
                cached.write_text(json.dumps(chunks), encoding="utf-8")
            for ch in chunks:
                s.run(LOAD_ANNEX_CHUNK, roman=ax["roman"], **ch)
            print(f"  loaded annex {ax['roman']}: {len(chunks)} chunks")
    driver.close()


def load_neo4j(articles: list[dict]) -> None:
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    with driver.session() as s:
        for art in articles:
            s.run(LOAD_ARTICLE, number=art["number"], title=art["title"], text=art["text"], url=art["url"])
            for ob in art.get("extraction", {}).get("obligations", []):
                risk = ob.get("risk_category")
                if isinstance(risk, str) and risk.strip().lower() in ("null", "none", ""):
                    risk = None
                s.run(
                    LOAD_OBLIGATION,
                    art=art["number"], id=ob["id"], summary=ob["summary"],
                    roles=ob.get("roles", []), risk=risk,
                    exceptions=ob.get("exceptions", []),
                )
                for ref in ob.get("references", []):
                    try:
                        ref_n = int(ref)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= ref_n <= N_ARTICLES:  # skip cross-regulation refs like 2016/679
                        s.run(LOAD_REFERENCE, src=art["number"], dst=ref_n)
            for ch in art.get("chunks", []):
                s.run(LOAD_CHUNK, art=art["number"], **ch)
            print(f"  loaded article {art['number']}")
    driver.close()


# ----------------------------------------------------------------- main
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "fetch", "extract", "embed", "load", "annexes"])
    stage = ap.parse_args().stage

    if stage == "annexes":
        load_annexes(fetch_annexes())
        print("Annexes done ✦")
        raise SystemExit

    articles = fetch_articles()
    if stage in ("all", "extract"):
        articles = extract_entities(articles)
    if stage in ("all", "embed"):
        articles = embed_articles(articles)
    if stage in ("all", "load"):
        load_neo4j(articles)
    if stage == "all":
        load_annexes(fetch_annexes())
    print("Done ✦")
