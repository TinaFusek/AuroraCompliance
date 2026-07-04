"""
Aurora Compliance — enrich the graph with Sanction nodes.

Reads the penalty articles (99, 100, 101) from ingest cache, has Claude extract
the sanction tiers, and links every article listed as penalized to its Sanction node:

    (Article)-[:PENALIZED_BY]->(Sanction {tier, max_fine_eur, max_fine_pct, description})

Run once, after the main ingest:
    python add_sanctions.py
"""

import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

CACHE = Path("cache")
PENALTY_ARTICLES = [99, 100, 101]

PROMPT = """You are extracting sanction tiers from EU AI Act penalty articles.
Return ONLY valid JSON (no fences): a list of sanction objects:

[
  {{
    "tier": "<short unique slug, e.g. prohibited-practices>",
    "description": "<one sentence: what conduct this tier penalizes>",
    "max_fine_eur": <number or null>,
    "max_fine_pct": <number or null, % of worldwide annual turnover>,
    "penalized_articles": [<numbers of AI Act articles whose violation triggers this tier>]
  }}
]

Rules:
- Only EU AI Act article numbers (1-113) in penalized_articles; ignore references to other regulations.
- If a tier applies to a list of articles (e.g. "obligations under Articles 16, 22..."), include them all.
- Include SME caps as a note in description, not as separate tiers.

ARTICLE {n} TEXT:
{text}
"""


def main() -> None:
    texts = []
    for n in PENALTY_ARTICLES:
        f = CACHE / f"article_{n}.json"
        if not f.exists():
            print(f"  ! cache/article_{n}.json missing — run the main ingest fetch first")
            return
        texts.append(json.loads(f.read_text(encoding="utf-8")))

    llm = anthropic.Anthropic()
    sanctions: list[dict] = []
    for art in texts:
        msg = llm.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": PROMPT.format(n=art["number"], text=art["text"][:15000])}],
        )
        raw = msg.content[0].text.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError:
            print(f"  ! article {art['number']}: bad JSON, skipping")
            continue
        for s in batch:
            s["source_article"] = art["number"]
        sanctions.extend(batch)
        print(f"  article {art['number']}: {len(batch)} tiers")

    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    LOAD = """
    MERGE (s:Sanction {tier: $tier})
    SET s.description = $description, s.max_fine_eur = $max_fine_eur,
        s.max_fine_pct = $max_fine_pct, s.source_article = $source_article
    WITH s
    UNWIND $penalized_articles AS n
      MATCH (a:Article {number: n})
      MERGE (a)-[:PENALIZED_BY]->(s)
    """
    with driver.session() as sess:
        for s in sanctions:
            arts = [int(x) for x in s.get("penalized_articles", []) if isinstance(x, (int, float)) and 1 <= int(x) <= 113]
            sess.run(
                LOAD,
                tier=s["tier"], description=s.get("description", ""),
                max_fine_eur=s.get("max_fine_eur"), max_fine_pct=s.get("max_fine_pct"),
                penalized_articles=arts, source_article=s["source_article"],
            )
            print(f"  loaded {s['tier']}: linked to {len(arts)} articles")
    driver.close()
    print("Sanctions done ✦")


if __name__ == "__main__":
    main()
