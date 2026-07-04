"""
Aurora Compliance — FastAPI backend.

GraphRAG agent with three retrieval strategies routed per-question:
  * TRAVERSAL — multi-hop Cypher (obligations → roles → exceptions → deadlines)
  * VECTOR    — semantic search over article chunks
  * HYBRID    — both, merged

Endpoints:
  POST /ask                 {question, role?}   → answer + citations + subgraph
  GET  /graph/article/{n}                       → neighbourhood subgraph for viz
  GET  /health
"""

import os
from contextlib import asynccontextmanager

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase
from pydantic import BaseModel

load_dotenv()  # reads .env from the current folder or any parent

# ----------------------------------------------------------------- setup
driver = None
llm = anthropic.Anthropic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global driver
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    yield
    driver.close()


app = FastAPI(title="Aurora Compliance", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Ask(BaseModel):
    question: str
    role: str | None = None  # provider / deployer / ...
    lang: str = "en"         # "en" | "sk" — answer language


# ----------------------------------------------------------------- routing
ROUTER_PROMPT = """Classify this EU AI Act question into exactly one retrieval strategy and, if the
question is not in English, translate it to English for retrieval purposes.
Reply with ONLY valid JSON, no fences: {{"strategy": "TRAVERSAL|VECTOR|HYBRID", "query_en": "<English version of the question>"}}

TRAVERSAL — asks about obligations, roles, exceptions, deadlines, sanctions, risk categories,
            or relationships between them ("what must a deployer do", "exceptions to Article 50").
VECTOR    — open/definitional questions best answered from article text ("what is an AI system").
HYBRID    — mixes both ("what does Article 9 require and when does it apply to my chatbot").

Question: {q}"""


def route(question: str) -> tuple[str, str]:
    import json as _json

    msg = llm.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": ROUTER_PROMPT.format(q=question)}],
    )
    raw = msg.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        j = _json.loads(raw)
        strategy = j.get("strategy", "HYBRID").upper()
        query_en = j.get("query_en") or question
    except _json.JSONDecodeError:
        strategy, query_en = "HYBRID", question
    if strategy not in ("TRAVERSAL", "VECTOR", "HYBRID"):
        strategy = "HYBRID"
    return strategy, query_en


# ----------------------------------------------------------------- retrieval
TRAVERSAL_QUERY = """
CALL db.index.fulltext.queryNodes('article_text', $q) YIELD node AS a, score
WITH a, score ORDER BY score DESC LIMIT 4
OPTIONAL MATCH p = (a)-[:IMPOSES]->(o:Obligation)-[:APPLIES_TO]->(r:Role)
  WHERE $role IS NULL OR r.name = $role
OPTIONAL MATCH pe = (o)-[:HAS_EXCEPTION]->(e:Exception)
OPTIONAL MATCH pd = (o)-[:EFFECTIVE_FROM]->(d:Deadline)
OPTIONAL MATCH pc = (o)-[:CONDITIONAL_ON]->(rc:RiskCategory)
OPTIONAL MATCH (a)-[:PENALIZED_BY]->(s:Sanction)
OPTIONAL MATCH (a)-[:REFERS_TO]->(ref:Article)
OPTIONAL MATCH (ref)-[:PENALIZED_BY]->(s2:Sanction)
RETURN a.number AS article, a.title AS title, a.url AS url,
       collect(DISTINCT {id: o.id, summary: o.summary}) AS obligations,
       collect(DISTINCT r.name) AS roles,
       collect(DISTINCT e.summary) AS exceptions,
       collect(DISTINCT toString(d.date) + ' — ' + d.description) AS deadlines,
       collect(DISTINCT rc.name) AS risk_categories,
       collect(DISTINCT {tier: s.tier, max_fine_eur: s.max_fine_eur, max_fine_pct: s.max_fine_pct}) +
       collect(DISTINCT {tier: s2.tier, max_fine_eur: s2.max_fine_eur, max_fine_pct: s2.max_fine_pct}) AS sanctions,
       collect(DISTINCT ref.number) AS referenced_articles
"""

VECTOR_QUERY = """
CALL db.index.vector.queryNodes('article_chunks', 6, $embedding)
YIELD node AS c, score
MATCH (c)-[:CHUNK_OF]->(src)
RETURN CASE WHEN src:Article THEN src.number ELSE NULL END AS article,
       CASE WHEN src:Annex THEN src.number ELSE NULL END AS annex,
       src.title AS title, src.url AS url, c.text AS chunk, score
"""

SUBGRAPH_QUERY = """
MATCH (a:Article) WHERE a.number IN $articles
OPTIONAL MATCH p = (a)-[rel:IMPOSES|REFERS_TO|PENALIZED_BY]->(x)
OPTIONAL MATCH p2 = (a)-[:IMPOSES]->(:Obligation)-[rel2:APPLIES_TO|HAS_EXCEPTION|CONDITIONAL_ON|EFFECTIVE_FROM]->(y)
WITH collect(p) + collect(p2) AS paths
UNWIND paths AS path
WITH DISTINCT path WHERE path IS NOT NULL
UNWIND nodes(path) AS n
WITH collect(DISTINCT {id: elementId(n), label: labels(n)[0],
     name: coalesce(a_title(n), n.title, n.summary, n.name, toString(n.number), n.description, n.id)}) AS ns, path
UNWIND relationships(path) AS r
RETURN ns AS nodes,
       collect(DISTINCT {source: elementId(startNode(r)), target: elementId(endNode(r)), type: type(r)}) AS links
"""


def embed(text: str) -> list[float]:
    from openai import OpenAI

    return OpenAI().embeddings.create(model="text-embedding-3-small", input=[text]).data[0].embedding


def get_subgraph(article_numbers: list[int], max_nodes: int = 18) -> dict:
    q = """
    MATCH (a:Article) WHERE a.number IN $articles
    OPTIONAL MATCH p1 = (a)-[:IMPOSES]->(o:Obligation)-[:APPLIES_TO|HAS_EXCEPTION|CONDITIONAL_ON|EFFECTIVE_FROM]->(x)
    OPTIONAL MATCH p2 = (a)-[:PENALIZED_BY]->(:Sanction)
    OPTIONAL MATCH p3 = (a)-[:REFERS_TO]->(:Article)
    WITH a, (collect(p1)[..8] + collect(p2)[..3] + collect(p3)[..3]) AS paths
    RETURN a, paths
    """
    nodes, links, seen = [], [], set()

    def add(node):
        nid = node.element_id
        if nid not in seen:
            seen.add(nid)
            label = list(node.labels)[0]
            props = dict(node)
            if label == "Sanction":
                name = f"€{props.get('max_fine_eur', 0):,.0f} / {props.get('max_fine_pct', 0)}% turnover"
            else:
                name = props.get("title") or props.get("summary") or props.get("name") \
                    or props.get("description") or props.get("id") or str(props.get("number", ""))
            nodes.append({"id": nid, "label": label, "name": str(name)[:80]})
        return nid

    with driver.session() as s:
        for rec in s.run(q, articles=article_numbers):
            add(rec["a"])
            for path in rec["paths"]:
                if len(seen) >= max_nodes:
                    break
                for rel in path.relationships:
                    src, dst = add(rel.start_node), add(rel.end_node)
                    links.append({"source": src, "target": dst, "type": rel.type})
    return {"nodes": nodes, "links": links}


# ----------------------------------------------------------------- answer
ANSWER_PROMPT = """You are Aurora, an EU AI Act compliance assistant backed by a knowledge graph.
Answer the question using ONLY the graph context below. Cite article numbers inline like [Art. 9].
Keep formatting light: short paragraphs and simple "- " bullets only; no headings, no horizontal rules,
and NEVER markdown tables (the UI cannot render them — use bullets instead).
If the question is not about the EU AI Act (e.g. "what can you do?"), reply in 2-3 sentences describing
what you can answer, with two example questions — do not analyse the empty context.
If the context is insufficient for an on-topic question, say what is missing. This is informational
guidance, not legal advice (the UI already shows this disclaimer — don't repeat it).

{role_line}
QUESTION: {question}

GRAPH CONTEXT:
{context}
"""


@app.post("/ask")
def ask(body: Ask):
    strategy, query_en = route(body.question)
    context_parts, article_numbers = [], []

    with driver.session() as s:
        if strategy in ("TRAVERSAL", "HYBRID"):
            for rec in s.run(TRAVERSAL_QUERY, q=query_en, role=body.role):
                article_numbers.append(rec["article"])
                sanctions = [x for x in rec["sanctions"] if x.get("tier") is not None]
                context_parts.append(
                    f"Article {rec['article']} — {rec['title']} ({rec['url']})\n"
                    f"  obligations: {rec['obligations']}\n  roles: {rec['roles']}\n"
                    f"  exceptions: {rec['exceptions']}\n  deadlines: {rec['deadlines']}\n"
                    f"  risk: {rec['risk_categories']}\n  sanctions: {sanctions}\n"
                    f"  refers_to_articles: {rec['referenced_articles']}"
                )
        if strategy in ("VECTOR", "HYBRID"):
            emb = embed(query_en)
            for rec in s.run(VECTOR_QUERY, embedding=emb):
                if rec["article"] is not None and rec["article"] not in article_numbers:
                    article_numbers.append(rec["article"])
                src = f"Article {rec['article']}" if rec["article"] is not None else f"Annex {rec['annex']}"
                context_parts.append(f"{src} — {rec['title']}: {rec['chunk']}")

    msg = llm.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": ANSWER_PROMPT.format(
                role_line=(f"USER ROLE: {body.role}\n" if body.role else "")
                + ("ANSWER IN SLOVAK. Keep legal terms of art and [Art. N] citations as-is.\n" if body.lang == "sk" else ""),
                question=body.question,
                context="\n\n".join(context_parts) or "(no matches)",
            ),
        }],
    )

    return {
        "strategy": strategy,
        "answer": msg.content[0].text,
        "citations": sorted(set(article_numbers)),
        "subgraph": get_subgraph(article_numbers[:3]),
    }


@app.get("/graph/article/{n}")
def article_graph(n: int):
    return get_subgraph([n])


@app.get("/health")
def health():
    with driver.session() as s:
        count = s.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"]
    return {"status": "ok", "articles": count}


# ---- serve the frontend from the same server (must come after API routes) ----
from pathlib import Path

from fastapi.staticfiles import StaticFiles

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
    # Import string ("main:app") is needed for reload to work.
    # Run from the backend/ folder:  python main.py
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
