"""
Aurora — GDS analýza agent traces.

"Fast, unsupervised algorithms for discovering and troubleshooting agent traces"
— presne tento use case. Každý /ask dopyt zanechal Trace uzol s VISITED hranami
na články, ktoré reasoning použil. Tento skript nad nimi pustí tri analýzy:

  1. HOT-SPOTY   — degree centrality: ktoré články sú najčastejšie navštevované
                   naprieč dopytmi (jadro reasoning-u appky)
  2. KLASTRE     — node similarity: skupiny traces, ktoré šli podobnou cestou
                   (typy otázok podľa správania, nie podľa textu)
  3. ANOMÁLIE    — traces, ktoré sa nepodobajú na žiadne iné + prázdne/pomalé
                   behy (kandidáti na troubleshooting)

Beh:  python analyze_traces.py
Vyžaduje: GDS plugin v Neo4j (RETURN gds.version() musí fungovať)
"""

import os

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

driver = GraphDatabase.driver(
    os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
)

GRAPH_NAME = "trace_graph"


def run(session, query, **params):
    return list(session.run(query, **params))


def ensure_projection(s):
    """(Re)create the in-memory GDS projection of Trace->Article."""
    exists = run(s, "CALL gds.graph.exists($name) YIELD exists RETURN exists", name=GRAPH_NAME)
    if exists and exists[0]["exists"]:
        run(s, "CALL gds.graph.drop($name)", name=GRAPH_NAME)
    run(s, """
        CALL gds.graph.project(
            $name,
            ['Trace', 'Article'],
            {VISITED: {orientation: 'UNDIRECTED'}}
        )
    """, name=GRAPH_NAME)


def hotspots(s, top=10):
    print("\n=== 1. HOT-SPOTY — najnavštevovanejšie články naprieč traces ===")
    rows = run(s, """
        CALL gds.degree.stream($name)
        YIELD nodeId, score
        WITH gds.util.asNode(nodeId) AS n, score
        WHERE n:Article
        RETURN n.number AS article, n.title AS title, score AS visits
        ORDER BY visits DESC LIMIT $top
    """, name=GRAPH_NAME, top=top)
    if not rows:
        print("  (žiadne dáta — najprv nazbieraj traces cez /ask)")
    for r in rows:
        print(f"  Art. {r['article']:>3}  visits={int(r['visits']):>3}  {r['title'][:60]}")


def clusters(s, top=10):
    print("\n=== 2. KLASTRE — traces s podobným správaním (node similarity) ===")
    rows = run(s, """
        CALL gds.nodeSimilarity.stream($name)
        YIELD node1, node2, similarity
        WITH gds.util.asNode(node1) AS a, gds.util.asNode(node2) AS b, similarity
        WHERE a:Trace AND b:Trace AND id(a) < id(b) AND similarity > 0.5
        RETURN a.question AS q1, b.question AS q2, round(similarity, 2) AS sim
        ORDER BY sim DESC LIMIT $top
    """, name=GRAPH_NAME, top=top)
    if not rows:
        print("  (málo traces alebo žiadne podobné dvojice — zbieraj ďalej)")
    for r in rows:
        print(f"  sim={r['sim']}  '{r['q1'][:40]}'  ~  '{r['q2'][:40]}'")


def anomalies(s):
    print("\n=== 3. ANOMÁLIE — kandidáti na troubleshooting ===")
    print("  -- prázdne behy (retrieval nič nenašiel):")
    for r in run(s, """
        MATCH (t:Trace) WHERE t.empty = true
        RETURN t.question AS q, t.strategy AS strat, t.ts AS ts
        ORDER BY ts DESC LIMIT 10
    """):
        print(f"     [{r['strat']}] '{r['q'][:60]}'")

    print("  -- osamelé traces (nepodobné žiadnym iným — nezvyčajné cesty):")
    rows = run(s, """
        CALL gds.nodeSimilarity.stream($name)
        YIELD node1, similarity
        WITH gds.util.asNode(node1) AS t, max(similarity) AS max_sim
        WHERE t:Trace
        WITH t, max_sim WHERE max_sim < 0.2
        RETURN t.question AS q, round(max_sim, 2) AS closest
        LIMIT 10
    """, name=GRAPH_NAME)
    if not rows:
        print("     (žiadne výrazné anomálie)")
    for r in rows:
        print(f"     closest_sim={r['closest']}  '{r['q'][:60]}'")

    print("  -- najpomalšie behy:")
    for r in run(s, """
        MATCH (t:Trace)
        RETURN t.question AS q, t.duration_ms AS ms
        ORDER BY ms DESC LIMIT 5
    """):
        print(f"     {r['ms']:>6} ms  '{r['q'][:60]}'")


def summary(s):
    r = run(s, "MATCH (t:Trace) RETURN count(t) AS n")[0]
    print(f"\nTraces v grafe: {r['n']}")
    return r["n"]


if __name__ == "__main__":
    with driver.session() as s:
        n = summary(s)
        if n == 0:
            print("Najprv polož Aurore pár otázok (/ask) — každá zanechá trace.")
        else:
            ensure_projection(s)
            hotspots(s)
            clusters(s)
            anomalies(s)
            run(s, "CALL gds.graph.drop($name)", name=GRAPH_NAME)
    driver.close()
    print("\nHotovo ✦")
