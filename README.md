# Aurora Compliance

**Ask the EU AI Act a question in plain language — and get an answer that shows its work. Then ask the assistant about its own reasoning.**

Aurora Compliance is a bilingual (EN/SK) GraphRAG assistant over Regulation (EU) 2024/1689 (the EU AI Act). The law itself is a knowledge graph: every article is decomposed into concrete obligations, connected to the roles they bind, the risk categories that trigger them, their exceptions, deadlines, and sanctions. Questions are answered by **traversing the graph**, and every answer ships with the exact subgraph it walked to build it.

What makes this more than a retrieval demo: **every question Aurora answers becomes a node in the same graph.** The app logs its own reasoning as it happens, then runs graph analytics over that log — so instead of reading transcripts by hand, you can ask the system about its own behaviour.


---
![alt text](image-1.png)
---
![alt text](image2-1.png)
---

## What it does

**Ask about the law:**
> "What fine do I risk as a deployer if I violate a given obligation?"

Traverses the graph, answers with article citations, and renders the reasoning path — the subgraph the agent actually walked — so you can audit it instead of trusting a black box.

**Ask about the agent itself:**
> "Which articles does the agent rely on most?"
> "What do the empty-context runs have in common?"

A second, small GraphRAG layer sits over the *trace graph* — the record of every question Aurora has answered and what it visited while answering. It runs GDS (PageRank for hotspots, node similarity for behavioural clusters) and answers meta-questions in plain language, grounded in the actual trace data.

---

## How it works

```
Official text (EUR-Lex / AI Act Explorer)
        │  LLM-based extraction → obligations, roles, exceptions, deadlines
        ▼
   Knowledge graph (Neo4j)
        │  Articles · Obligations · Roles · RiskCategories · Exceptions
        │  Deadlines · Sanctions · full-text + native vector index
        ▼
  Routing agent (Haiku) ──► translates non-English questions for retrieval
        │
        ▼
  Answer synthesis (Sonnet) — grounded strictly in retrieved context, cited
        │
        ▼
  Every run logged as a Trace node ──[:VISITED]──► the Articles it touched
        │
        ▼
  GDS over the trace graph (PageRank, node similarity)
        │
        ▼
  Meta-GraphRAG (/ask_trace) — ask the agent about its own behaviour
```

**Stack:** Neo4j (property graph + native vector index + full-text + GDS), FastAPI, Claude (routing, synthesis, and trace-question answering), D3.js for both the legal reasoning graph and the agent-behaviour map.

---

## Design decisions

**Domain graph, not a document graph.** The schema models obligations, roles, and sanctions as first-class nodes — because questions revolve around duties, not paragraphs. Relationships with more than two participants (an article imposing an obligation on a role, under a condition, with an exception) are reified into their own nodes rather than forced onto a single edge.

**Small model routes, larger model synthesizes.** Classifying a question's retrieval strategy is cheap; composing a cited answer is expensive. The router runs on Haiku, the answer on Sonnet.

**Observability is a second graph, not a side log.** Rather than writing traces to a text log, each run becomes a node in the same Neo4j instance the app already queries. That means the same tools (Cypher, GDS, GraphRAG) that answer questions about the *law* can answer questions about the *agent* — no separate observability stack needed.

**GDS with a plain-Cypher fallback.** Hotspot and cluster analytics use the Graph Data Science library when available (PageRank, node similarity) and fall back to plain visit counts if the plugin isn't installed — the panel never breaks, it just gets less precise.

---

## What building it taught me

**Transparency debugged the system.** The assistant once reported that penalty figures were missing from the graph context. The data was there — the retrieval query simply never fetched sanctions. Because the system could say *"here is what I could not see,"* it exposed a bug in my own query that a black-box answer would have hidden.

**Retrieval is language-bound; generation is not.** Slovak questions silently degraded to vector-only results, because the full-text index is built over English text. The fix — translate for retrieval, answer in the user's language — lives in the router, at no extra cost.

**LLM output is a data proposal, not data.** Entity extraction occasionally returned the literal string `"null"` instead of a null value, and parsed cross-references to *other* regulations as if they were internal article numbers. Every extraction pipeline needs a validation layer between the model and the database.

**Concept questions need their own retrieval path.** Full-text search over articles finds nothing for questions like "why does this cost so much to check?" — the query names no article. Application- and effect-level questions need their own entry points into the graph, separate from keyword search.

---

## Roadmap

- Trace diffing — run a golden question set before/after a graph change (a new amendment, a prompt edit) and surface which reasoning paths shifted, and why.
- Provenance with a decay function — edges already carry *where* a fact came from; the next step is letting confidence age over time rather than being fixed at ingest.
- OTLP export — surface traces in an existing observability stack (Grafana/Datadog) instead of a bespoke panel.

---

## Notes

Informational guidance based on the official text — not legal advice. Source of truth: EUR-Lex (Regulation (EU) 2024/1689). All source data is public.

*Built as a personal project to learn GraphRAG — and agent observability — end to end.*
