// ============================================================
// AURORA COMPLIANCE — EU AI Act Knowledge Graph Schema
// Regulation (EU) 2024/1689
// ============================================================

// --- Constraints (unique identifiers) ---
CREATE CONSTRAINT article_id IF NOT EXISTS
FOR (a:Article) REQUIRE a.number IS UNIQUE;

CREATE CONSTRAINT recital_id IF NOT EXISTS
FOR (r:Recital) REQUIRE r.number IS UNIQUE;

CREATE CONSTRAINT annex_id IF NOT EXISTS
FOR (x:Annex) REQUIRE x.number IS UNIQUE;

CREATE CONSTRAINT obligation_id IF NOT EXISTS
FOR (o:Obligation) REQUIRE o.id IS UNIQUE;

CREATE CONSTRAINT role_name IF NOT EXISTS
FOR (r:Role) REQUIRE r.name IS UNIQUE;

CREATE CONSTRAINT risk_name IF NOT EXISTS
FOR (rc:RiskCategory) REQUIRE rc.name IS UNIQUE;

CREATE CONSTRAINT chunk_id IF NOT EXISTS
FOR (c:Chunk) REQUIRE c.id IS UNIQUE;

// --- Vector index for hybrid retrieval (text-embedding chunks) ---
CREATE VECTOR INDEX article_chunks IF NOT EXISTS
FOR (c:Chunk) ON (c.embedding)
OPTIONS { indexConfig: {
  `vector.dimensions`: 1536,
  `vector.similarity_function`: 'cosine'
}};

// --- Full-text index (keyword fallback) ---
CREATE FULLTEXT INDEX article_text IF NOT EXISTS
FOR (a:Article) ON EACH [a.title, a.text];

// ============================================================
// Node types
// ------------------------------------------------------------
// (:Regulation {celex, title, in_force_from})
// (:Chapter {number, title})
// (:Article {number, title, text, url})
// (:Recital {number, text})
// (:Annex {number, title})
// (:Obligation {id, summary, deadline})
// (:Role {name})            provider | deployer | importer | distributor | product manufacturer
// (:RiskCategory {name})    prohibited | high | limited | minimal | gpai | gpai_systemic
// (:AISystem {use_case})    e.g. "recruitment screening", "biometric identification"
// (:Exception {summary})
// (:Sanction {max_fine_eur, max_fine_pct, tier})
// (:Deadline {date, description})
// (:Chunk {id, text, embedding, article_number})
//
// Relationships
// ------------------------------------------------------------
// (Regulation)-[:HAS_CHAPTER]->(Chapter)-[:HAS_ARTICLE]->(Article)
// (Article)-[:IMPOSES]->(Obligation)
// (Obligation)-[:APPLIES_TO]->(Role)
// (Obligation)-[:CONDITIONAL_ON]->(RiskCategory)
// (Obligation)-[:HAS_EXCEPTION]->(Exception)
// (Obligation)-[:EFFECTIVE_FROM]->(Deadline)
// (AISystem)-[:CLASSIFIED_AS]->(RiskCategory)
// (AISystem)-[:LISTED_IN]->(Annex)
// (Article)-[:INTERPRETED_BY]->(Recital)
// (Article)-[:REFERS_TO]->(Article)
// (Article)-[:PENALIZED_BY]->(Sanction)
// (Chunk)-[:CHUNK_OF]->(Article)
// ============================================================
