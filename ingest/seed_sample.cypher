// ============================================================
// Seed dataset — core AI Act structure for immediate demo
// (real content, abbreviated summaries; full text via scraper)
// ============================================================

MERGE (reg:Regulation {celex: "32024R1689"})
  SET reg.title = "Regulation (EU) 2024/1689 — Artificial Intelligence Act",
      reg.in_force_from = date("2024-08-01"),
      reg.fully_applicable = date("2026-08-02");

// --- Roles ---
UNWIND ["provider","deployer","importer","distributor","product manufacturer","authorised representative"] AS r
MERGE (:Role {name: r});

// --- Risk categories ---
UNWIND [
  {name:"prohibited", label:"Unacceptable risk"},
  {name:"high", label:"High risk"},
  {name:"limited", label:"Limited risk (transparency)"},
  {name:"minimal", label:"Minimal risk"},
  {name:"gpai", label:"General-purpose AI"},
  {name:"gpai_systemic", label:"GPAI with systemic risk"}
] AS rc
MERGE (x:RiskCategory {name: rc.name}) SET x.label = rc.label;

// --- Key articles (abbreviated) ---
UNWIND [
  {n:5,  t:"Prohibited AI practices", txt:"Bans manipulative techniques, exploitation of vulnerabilities, social scoring, and certain biometric practices."},
  {n:6,  t:"Classification rules for high-risk AI systems", txt:"An AI system is high-risk if it is a safety component of a regulated product or listed in Annex III."},
  {n:9,  t:"Risk management system", txt:"Providers of high-risk AI systems shall establish, implement, document and maintain a risk management system."},
  {n:10, t:"Data and data governance", txt:"Training, validation and testing data sets shall be subject to appropriate data governance practices."},
  {n:13, t:"Transparency and provision of information to deployers", txt:"High-risk AI systems shall be designed to ensure operation is sufficiently transparent."},
  {n:14, t:"Human oversight", txt:"High-risk AI systems shall be designed so they can be effectively overseen by natural persons."},
  {n:26, t:"Obligations of deployers of high-risk AI systems", txt:"Deployers shall use systems per instructions, assign human oversight, monitor operation."},
  {n:50, t:"Transparency obligations for certain AI systems", txt:"Persons must be informed when interacting with an AI system; synthetic content must be marked."},
  {n:53, t:"Obligations for providers of GPAI models", txt:"Technical documentation, information to downstream providers, copyright policy, training data summary."},
  {n:99, t:"Penalties", txt:"Fines up to EUR 35m or 7% of worldwide turnover for prohibited practices; up to EUR 15m or 3% for other violations."}
] AS art
MERGE (a:Article {number: art.n})
  SET a.title = art.t, a.text = art.txt,
      a.url = "https://artificialintelligenceact.eu/article/" + toString(art.n) + "/";

// --- Annex III (high-risk use cases, sample) ---
MERGE (an3:Annex {number:"III"}) SET an3.title = "High-risk AI systems referred to in Article 6(2)";
UNWIND [
  "biometric identification and categorisation",
  "critical infrastructure management",
  "education and vocational training",
  "employment and worker management (recruitment, screening, evaluation)",
  "access to essential private and public services",
  "law enforcement",
  "migration, asylum and border control",
  "administration of justice and democratic processes"
] AS uc
MERGE (s:AISystem {use_case: uc})
MERGE (s)-[:LISTED_IN]->(an3)
WITH s
MATCH (hr:RiskCategory {name:"high"})
MERGE (s)-[:CLASSIFIED_AS]->(hr);

// --- Obligations wired to articles, roles, risk, deadlines ---
MATCH (a9:Article{number:9}), (a10:Article{number:10}), (a14:Article{number:14}),
      (a26:Article{number:26}), (a50:Article{number:50}), (a53:Article{number:53}),
      (prov:Role{name:"provider"}), (depl:Role{name:"deployer"}),
      (high:RiskCategory{name:"high"}), (lim:RiskCategory{name:"limited"}), (gpai:RiskCategory{name:"gpai"})

MERGE (o1:Obligation {id:"OBL-RMS"}) SET o1.summary="Maintain a documented risk management system across the lifecycle"
MERGE (a9)-[:IMPOSES]->(o1) MERGE (o1)-[:APPLIES_TO]->(prov) MERGE (o1)-[:CONDITIONAL_ON]->(high)

MERGE (o2:Obligation {id:"OBL-DATA-GOV"}) SET o2.summary="Apply data governance to training/validation/test datasets"
MERGE (a10)-[:IMPOSES]->(o2) MERGE (o2)-[:APPLIES_TO]->(prov) MERGE (o2)-[:CONDITIONAL_ON]->(high)

MERGE (o3:Obligation {id:"OBL-OVERSIGHT"}) SET o3.summary="Design for effective human oversight"
MERGE (a14)-[:IMPOSES]->(o3) MERGE (o3)-[:APPLIES_TO]->(prov) MERGE (o3)-[:CONDITIONAL_ON]->(high)

MERGE (o4:Obligation {id:"OBL-DEPLOYER-USE"}) SET o4.summary="Use per instructions, assign oversight, monitor and log"
MERGE (a26)-[:IMPOSES]->(o4) MERGE (o4)-[:APPLIES_TO]->(depl) MERGE (o4)-[:CONDITIONAL_ON]->(high)

MERGE (o5:Obligation {id:"OBL-DISCLOSE-AI"}) SET o5.summary="Inform people they are interacting with AI; mark synthetic content"
MERGE (a50)-[:IMPOSES]->(o5) MERGE (o5)-[:APPLIES_TO]->(prov) MERGE (o5)-[:APPLIES_TO]->(depl) MERGE (o5)-[:CONDITIONAL_ON]->(lim)

MERGE (o6:Obligation {id:"OBL-GPAI-DOC"}) SET o6.summary="Technical documentation, downstream info, copyright policy, training data summary"
MERGE (a53)-[:IMPOSES]->(o6) MERGE (o6)-[:APPLIES_TO]->(prov) MERGE (o6)-[:CONDITIONAL_ON]->(gpai);

// --- Exceptions (sample) ---
MATCH (o5:Obligation {id:"OBL-DISCLOSE-AI"})
MERGE (e1:Exception {summary:"Not required where obvious from context, or authorised by law for criminal offence detection"})
MERGE (o5)-[:HAS_EXCEPTION]->(e1);

// --- Sanctions ---
MATCH (a5:Article{number:5}), (a99:Article{number:99})
MERGE (s1:Sanction {tier:"prohibited"}) SET s1.max_fine_eur=35000000, s1.max_fine_pct=7.0
MERGE (s2:Sanction {tier:"other"})      SET s2.max_fine_eur=15000000, s2.max_fine_pct=3.0
MERGE (a5)-[:PENALIZED_BY]->(s1)
MERGE (a99)-[:REFERS_TO]->(a5);

// --- Deadlines ---
UNWIND [
  {d:date("2025-02-02"), desc:"Prohibited practices + AI literacy obligations apply"},
  {d:date("2025-08-02"), desc:"GPAI obligations + governance rules apply"},
  {d:date("2026-08-02"), desc:"Act fully applicable (incl. Annex III high-risk)"},
  {d:date("2028-08-02"), desc:"Extended transition for high-risk in regulated products (AI omnibus)"}
] AS dl
MERGE (x:Deadline {date: dl.d}) SET x.description = dl.desc;

MATCH (o6:Obligation {id:"OBL-GPAI-DOC"}), (d25:Deadline {date:date("2025-08-02")})
MERGE (o6)-[:EFFECTIVE_FROM]->(d25);
MATCH (o1:Obligation {id:"OBL-RMS"}), (d26:Deadline {date:date("2026-08-02")})
MERGE (o1)-[:EFFECTIVE_FROM]->(d26);
