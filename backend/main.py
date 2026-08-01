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
    mode: str | None = None  # optional: force TRAVERSAL|VECTOR|HYBRID (for eval/harness);
                             # keeps the router's translation, overrides only the strategy


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
RETURN a.number AS article, a.title AS title, a.url AS url, score,
       collect(DISTINCT {id: o.id, summary: o.summary}) AS obligations,
       collect(DISTINCT r.name) AS roles,
       collect(DISTINCT e.summary) AS exceptions,
       collect(DISTINCT toString(d.date) + ' — ' + d.description) AS deadlines,
       collect(DISTINCT rc.name) AS risk_categories,
       collect(DISTINCT {tier: s.tier, max_fine_eur: s.max_fine_eur, max_fine_pct: s.max_fine_pct}) +
       collect(DISTINCT {tier: s2.tier, max_fine_eur: s2.max_fine_eur, max_fine_pct: s2.max_fine_pct}) AS sanctions,
       collect(DISTINCT ref.number) AS referenced_articles
ORDER BY score DESC
"""

# Relevance floor: a Lucene fulltext score below this means the "match" is just
# incidental keyword overlap ("AI", "Act", "Article"), not a real topical hit.
# Below it, we treat retrieval as empty so the agent can honestly abstain instead
# of hallucinating from irrelevant context. Tune against your own score distribution.
RELEVANCE_FLOOR = 1.5

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
    import time
    import uuid

    t_start = time.time()
    strategy, query_en = route(body.question)
    if body.mode in ("TRAVERSAL", "VECTOR", "HYBRID"):
        strategy = body.mode  # eval override: keep the translation, force the strategy
    context_parts, article_numbers = [], []
    trace_hits = []  # (article_number, retrieval_branch)

    with driver.session() as s:
        if strategy in ("TRAVERSAL", "HYBRID"):
            rows = list(s.run(TRAVERSAL_QUERY, q=query_en, role=body.role))
            top_score = rows[0]["score"] if rows else 0.0
            for rec in rows:
                # Relevance floor: keep the strongest hit only if it clears the floor.
                # Everything weaker is incidental keyword overlap — drop it so that an
                # out-of-scope question yields empty context and an honest "I don't know".
                if rec["score"] < RELEVANCE_FLOOR:
                    continue
                article_numbers.append(rec["article"])
                trace_hits.append((rec["article"], "traversal"))
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
                    trace_hits.append((rec["article"], "vector"))
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

    # ---- trace logging: record this query's reasoning as a graph ----
    log_trace(
        trace_id=str(uuid.uuid4())[:8],
        question=body.question,
        query_en=query_en,
        strategy=strategy,
        lang=body.lang,
        hits=trace_hits,
        n_context=len(context_parts),
        duration_ms=int((time.time() - t_start) * 1000),
    )

    return {
        "strategy": strategy,
        "query_en": query_en,
        "answer": msg.content[0].text,
        "citations": sorted(set(article_numbers)),
        "citations_ranked": list(dict.fromkeys(article_numbers)),  # relevance order, strongest first
        "retrieval_empty": len(context_parts) == 0,
        "subgraph": get_subgraph(article_numbers[:3]),
    }


TRACE_QUERY = """
CREATE (t:Trace {id: $trace_id, question: $question, query_en: $query_en,
                 strategy: $strategy, lang: $lang, n_context: $n_context,
                 duration_ms: $duration_ms, empty: $empty, ts: datetime()})
WITH t
UNWIND $hits AS hit
  MATCH (a:Article {number: hit.article})
  MERGE (t)-[v:VISITED]->(a)
  SET v.branch = hit.branch
"""


def log_trace(trace_id, question, query_en, strategy, lang, hits, n_context, duration_ms):
    """Record one /ask run as a Trace node linked to the articles it visited.
    Failures here must never break the user-facing answer."""
    try:
        with driver.session() as s:
            s.run(
                TRACE_QUERY,
                trace_id=trace_id, question=question[:300], query_en=query_en[:300],
                strategy=strategy, lang=lang, n_context=n_context,
                duration_ms=duration_ms, empty=(n_context == 0),
                hits=[{"article": a, "branch": b} for a, b in hits],
            )
    except Exception as e:  # noqa: BLE001
        print(f"[trace] logging failed (non-fatal): {e}")


@app.get("/graph/article/{n}")
def article_graph(n: int):
    return get_subgraph([n])


GDS_GRAPH_NAME = "trace_graph_live"


def gds_available(s) -> bool:
    try:
        s.run("RETURN gds.version()").single()
        return True
    except Exception:
        return False


def gds_hotspots(s, top=10):
    """PageRank over Trace-Article graph — richer than raw visit counts,
    since it weighs *which* traces visit an article, not just how many."""
    if s.run("MATCH (t:Trace) RETURN count(t) AS n").single()["n"] == 0:
        return []
    if s.run("CALL gds.graph.exists($n) YIELD exists", n=GDS_GRAPH_NAME).single()["exists"]:
        s.run("CALL gds.graph.drop($n)", n=GDS_GRAPH_NAME)
    s.run("""
        CALL gds.graph.project($n, ['Trace','Article'], {VISITED: {orientation:'UNDIRECTED'}})
    """, n=GDS_GRAPH_NAME)
    rows = [dict(r) for r in s.run("""
        CALL gds.pageRank.stream($n)
        YIELD nodeId, score
        WITH gds.util.asNode(nodeId) AS node, score
        WHERE node:Article
        RETURN node.number AS article, node.title AS title, round(score, 3) AS pagerank
        ORDER BY pagerank DESC LIMIT $top
    """, n=GDS_GRAPH_NAME, top=top)]
    return rows


def gds_clusters(s, min_sim=0.5, top=10):
    """Node similarity between Trace nodes: which questions behave alike,
    discovered from graph structure, not from question text."""
    rows = [dict(r) for r in s.run("""
        CALL gds.nodeSimilarity.stream($n)
        YIELD node1, node2, similarity
        WITH gds.util.asNode(node1) AS a, gds.util.asNode(node2) AS b, similarity
        WHERE a:Trace AND b:Trace AND elementId(a) < elementId(b) AND similarity >= $min_sim
        RETURN a.question AS q1, b.question AS q2, round(similarity, 2) AS sim
        ORDER BY sim DESC LIMIT $top
    """, n=GDS_GRAPH_NAME, min_sim=min_sim, top=top)]
    s.run("CALL gds.graph.drop($n)", n=GDS_GRAPH_NAME)
    return rows


@app.get("/traces")
def traces(limit: int = 30):
    """Trace analytics for the UI: GDS-powered hotspots/clusters when the
    plugin is available, falling back to plain Cypher counts otherwise."""
    with driver.session() as s:
        use_gds = gds_available(s)
        if use_gds:
            try:
                hot = gds_hotspots(s)
                clusters = gds_clusters(s)
            except Exception as e:  # noqa: BLE001
                print(f"[gds] falling back to Cypher: {e}")
                use_gds = False
        if not use_gds:
            hot = [dict(r) for r in s.run("""
                MATCH (t:Trace)-[:VISITED]->(a:Article)
                RETURN a.number AS article, a.title AS title, count(t) AS visits
                ORDER BY visits DESC LIMIT 10
            """)]
            clusters = []

        recent = [dict(r) for r in s.run("""
            MATCH (t:Trace)
            OPTIONAL MATCH (t)-[:VISITED]->(a:Article)
            WITH t, count(a) AS n_visited
            RETURN t.id AS id, t.question AS question, t.strategy AS strategy,
                   t.lang AS lang, t.duration_ms AS duration_ms,
                   t.empty AS empty, n_visited, toString(t.ts) AS ts
            ORDER BY t.ts DESC LIMIT $limit
        """, limit=limit)]

        # trace graph: recent traces + the articles they visited
        nodes, links, seen = [], [], set()
        for rec in s.run("""
            MATCH (t:Trace)
            WITH t ORDER BY t.ts DESC LIMIT $limit
            MATCH (t)-[v:VISITED]->(a:Article)
            RETURN t.id AS tid, t.question AS q, t.strategy AS strat, t.empty AS empty,
                   a.number AS art, a.title AS title, v.branch AS branch
        """, limit=limit):
            tkey = f"t-{rec['tid']}"
            akey = f"a-{rec['art']}"
            if tkey not in seen:
                seen.add(tkey)
                nodes.append({"id": tkey, "label": "Trace",
                              "name": (rec["q"] or "")[:40], "strategy": rec["strat"],
                              "empty": bool(rec["empty"])})
            if akey not in seen:
                seen.add(akey)
                nodes.append({"id": akey, "label": "Article",
                              "name": f"Art. {rec['art']}"})
            links.append({"source": tkey, "target": akey,
                          "type": rec["branch"] or "visited"})

        n_total = s.run("MATCH (t:Trace) RETURN count(t) AS n").single()["n"]

    return {"total": n_total, "gds_active": use_gds, "hotspots": hot,
            "clusters": clusters, "recent": recent,
            "graph": {"nodes": nodes, "links": links}}


# ============================================================
# Trace-GraphRAG — GraphRAG over the agent's OWN reasoning traces.
#
# Regular /ask answers questions about the LAW by traversing the
# knowledge graph. This answers questions about the AGENT'S BEHAVIOUR
# by traversing the TRACE graph — the signal Andrew described:
# "where to start" instead of reading every trace by hand.
# ============================================================

class AskTrace(BaseModel):
    question: str


TRACE_ROUTER_PROMPT = """Classify this question about an AI agent's own reasoning traces
into one category. Reply ONLY valid JSON: {{"mode": "SPECIFIC|AGGREGATE|EMPTY_ONLY|HOTSPOT", "keyword": "..."}}

SPECIFIC   — asks about one particular trace/question (quote or close paraphrase given).
             keyword = the distinguishing phrase from that question.
AGGREGATE  — asks what a group of traces have in common, or to summarize behaviour
             around a topic/article. keyword = the topic/article mentioned (or "").
EMPTY_ONLY — asks specifically about failed/empty-context runs.
             keyword = "".
HOTSPOT    — asks which articles/topics the agent relies on most.
             keyword = "".

Question: {q}"""


def route_trace_question(question: str) -> tuple[str, str]:
    msg = llm.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": TRACE_ROUTER_PROMPT.format(q=question)}],
    )
    raw = msg.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        j = _json.loads(raw)
        mode = j.get("mode", "AGGREGATE").upper()
        keyword = j.get("keyword", "")
    except Exception:  # noqa: BLE001
        mode, keyword = "AGGREGATE", ""
    if mode not in ("SPECIFIC", "AGGREGATE", "EMPTY_ONLY", "HOTSPOT"):
        mode = "AGGREGATE"
    return mode, keyword


TRACE_CONTEXT_QUERY = """
MATCH (t:Trace)
WHERE ($kw = '' OR toLower(t.question) CONTAINS toLower($kw))
OPTIONAL MATCH (t)-[v:VISITED]->(a:Article)
WITH t, collect({article: a.number, title: a.title, branch: v.branch}) AS visited
RETURN t.id AS id, t.question AS question, t.strategy AS strategy, t.lang AS lang,
       t.empty AS empty, t.duration_ms AS duration_ms, toString(t.ts) AS ts, visited
ORDER BY t.ts DESC LIMIT 15
"""

TRACE_EMPTY_QUERY = """
MATCH (t:Trace) WHERE t.empty = true
RETURN t.id AS id, t.question AS question, t.strategy AS strategy,
       toString(t.ts) AS ts
ORDER BY t.ts DESC LIMIT 15
"""

TRACE_HOTSPOT_QUERY = """
MATCH (t:Trace)-[:VISITED]->(a:Article)
RETURN a.number AS article, a.title AS title, count(t) AS visits
ORDER BY visits DESC LIMIT 10
"""

TRACE_ANSWER_PROMPT = """You are analysing an AI agent's own reasoning traces — this is
observability/debugging over the agent's behaviour, not a question about the EU AI Act itself.
Answer using ONLY the trace data below. Be concrete: cite trace questions and article numbers.
If the data doesn't support a claim, say so plainly. Keep it short: a few sentences or bullets,
no headings, no tables.

QUESTION: {question}

TRACE DATA:
{context}
"""


@app.post("/ask_trace")
def ask_trace(body: AskTrace):
    mode, keyword = route_trace_question(body.question)
    context_parts = []

    with driver.session() as s:
        if mode == "EMPTY_ONLY":
            rows = list(s.run(TRACE_EMPTY_QUERY))
            for r in rows:
                context_parts.append(f"[{r['strategy']}] empty run at {r['ts']}: \"{r['question']}\"")
            if not rows:
                context_parts.append("(no empty-context runs recorded)")

        elif mode == "HOTSPOT":
            rows = list(s.run(TRACE_HOTSPOT_QUERY))
            for r in rows:
                context_parts.append(f"Art. {r['article']} — {r['title']}: visited {r['visits']}x")

        else:  # SPECIFIC or AGGREGATE — both pull matching/recent traces with their paths
            rows = list(s.run(TRACE_CONTEXT_QUERY, kw=keyword))
            for r in rows:
                visited = ", ".join(f"Art.{v['article']}({v['branch']})" for v in r["visited"] if v["article"])
                context_parts.append(
                    f"trace {r['id']} [{r['strategy']}·{r['lang']}] {r['duration_ms']}ms "
                    f"empty={r['empty']} — \"{r['question']}\" — visited: {visited or '(none)'}"
                )
            if not rows:
                context_parts.append(f"(no traces matched keyword '{keyword}')")

    msg = llm.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[{
            "role": "user",
            "content": TRACE_ANSWER_PROMPT.format(
                question=body.question,
                context="\n".join(context_parts),
            ),
        }],
    )

    return {"mode": mode, "keyword": keyword, "answer": msg.content[0].text,
            "n_traces_used": len(context_parts)}


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
