"""PM Skill Taxonomy + Synonym Canonicalizer with Embedding Fallback.

Provides:
1. A curated taxonomy of ~150 PM-relevant skills organized by category
2. Synonym groups so "Agile" / "Scrum" / "Agile/Scrum" map to one canonical skill
3. Embedding-based fuzzy matching for skills not in the taxonomy
"""

from typing import Optional

import structlog

logger = structlog.get_logger()

# ────────────────────────────────────────────────────────────────
# PM SKILL TAXONOMY
#
# Each entry: "canonical_name": ["synonym1", "synonym2", ...]
# The canonical name is what we store and match against.
# Synonyms are what might appear on a resume or JD.
# ────────────────────────────────────────────────────────────────

SKILL_TAXONOMY: dict[str, dict[str, list[str]]] = {

    "product_strategy": {
        "Product Strategy":           ["product strategy", "product vision", "strategic product thinking", "product direction"],
        "Product Roadmapping":        ["roadmap", "roadmapping", "product roadmap", "roadmap planning", "roadmap prioritization"],
        "Product Discovery":          ["product discovery", "opportunity discovery", "problem discovery"],
        "Product Vision":             ["product vision", "vision setting", "north star"],
        "Go-to-Market Strategy":      ["go-to-market", "GTM", "GTM strategy", "launch strategy", "market entry"],
        "Product-Led Growth":         ["PLG", "product-led growth", "product-led", "growth loops"],
        "Market Research":            ["market research", "market analysis", "competitive analysis", "market sizing", "TAM SAM SOM"],
        "Competitive Analysis":       ["competitive analysis", "competitor analysis", "competitive intelligence", "competitive landscape"],
        "Business Case Development":  ["business case", "business justification", "ROI analysis", "cost-benefit analysis"],
        "Pricing Strategy":           ["pricing", "pricing strategy", "monetization", "revenue model", "pricing model"],
    },

    "product_execution": {
        "Agile/Scrum":                ["agile", "scrum", "agile/scrum", "agile methodology", "scrum master", "sprint planning", "sprint", "agile framework"],
        "Kanban":                     ["kanban", "kanban board", "lean kanban"],
        "SAFe":                       ["SAFe", "scaled agile", "scaled agile framework", "PI planning"],
        "User Story Writing":         ["user stories", "user story", "story writing", "acceptance criteria", "BDD", "INVEST"],
        "Sprint Planning":            ["sprint planning", "sprint review", "sprint retrospective", "backlog refinement", "grooming"],
        "Backlog Management":         ["backlog management", "backlog grooming", "backlog prioritization", "product backlog"],
        "Release Management":         ["release management", "release planning", "release coordination", "deployment planning"],
        "Feature Prioritization":     ["prioritization", "feature prioritization", "RICE", "MoSCoW", "WSJF", "ICE", "priority framework", "Kano model"],
        "PRD Writing":                ["PRD", "product requirements document", "requirements writing", "product spec", "specification", "BRD"],
        "OKR Setting":                ["OKR", "OKRs", "objectives and key results", "goal setting", "KPI setting"],
        "A/B Testing":                ["A/B testing", "AB testing", "experimentation", "split testing", "multivariate testing"],
        "MVP Development":            ["MVP", "minimum viable product", "lean startup", "build-measure-learn"],
    },

    "technical_skills": {
        "SQL":                        ["SQL", "MySQL", "PostgreSQL", "database queries", "structured query language"],
        "Data Analysis":              ["data analysis", "data analytics", "quantitative analysis", "data-driven", "data-informed"],
        "Python":                     ["Python", "Python scripting", "pandas", "data manipulation"],
        "APIs":                       ["API", "APIs", "REST API", "RESTful", "API design", "API integration", "GraphQL", "webhook"],
        "System Design":              ["system design", "system architecture", "technical architecture", "architecture design"],
        "Cloud Platforms":            ["AWS", "GCP", "Azure", "cloud", "cloud computing", "cloud infrastructure"],
        "Machine Learning/AI":        ["machine learning", "ML", "AI", "artificial intelligence", "deep learning", "NLP", "GenAI", "LLM"],
        "Mobile Development":         ["mobile", "iOS", "Android", "mobile app", "React Native", "Flutter"],
        "Web Technologies":           ["HTML", "CSS", "JavaScript", "React", "frontend", "web development"],
        "Microservices":              ["microservices", "distributed systems", "service-oriented", "SOA"],
        "DevOps/CI-CD":               ["DevOps", "CI/CD", "continuous integration", "continuous deployment", "Jenkins", "GitHub Actions"],
        "Data Warehousing":           ["data warehouse", "ETL", "data pipeline", "data engineering", "BigQuery", "Redshift", "Snowflake"],
    },

    "analytics_tools": {
        "Google Analytics":           ["Google Analytics", "GA", "GA4", "web analytics"],
        "Mixpanel":                   ["Mixpanel", "product analytics"],
        "Amplitude":                  ["Amplitude", "behavioral analytics"],
        "Tableau":                    ["Tableau", "data visualization", "BI tool"],
        "Power BI":                   ["Power BI", "PowerBI", "Microsoft BI"],
        "Looker":                     ["Looker", "LookML"],
        "Heap":                       ["Heap", "Heap Analytics"],
        "Hotjar":                     ["Hotjar", "heatmaps", "session recording"],
        "Fullstory":                  ["Fullstory", "session replay"],
    },

    "product_tools": {
        "Jira":                       ["Jira", "JIRA", "Atlassian Jira", "Jira Software"],
        "Confluence":                 ["Confluence", "Atlassian Confluence"],
        "Linear":                     ["Linear", "Linear app"],
        "Asana":                      ["Asana"],
        "Trello":                     ["Trello"],
        "Monday.com":                 ["Monday", "Monday.com"],
        "Notion":                     ["Notion"],
        "Figma":                      ["Figma", "design tool"],
        "Miro":                       ["Miro", "whiteboarding", "Mural"],
        "Productboard":               ["Productboard", "product management tool"],
        "Aha!":                       ["Aha!", "Aha"],
        "Pendo":                      ["Pendo", "in-app guides"],
    },

    "ux_research": {
        "User Research":              ["user research", "UX research", "usability research", "customer research"],
        "Usability Testing":          ["usability testing", "user testing", "usability studies", "guerrilla testing"],
        "Customer Interviews":        ["customer interviews", "user interviews", "stakeholder interviews", "customer discovery"],
        "Survey Design":              ["surveys", "survey design", "NPS", "CSAT", "customer satisfaction"],
        "Persona Development":        ["personas", "user personas", "buyer personas", "customer personas", "ICP"],
        "Customer Journey Mapping":   ["customer journey", "journey mapping", "user journey", "experience mapping"],
        "Wire-framing":               ["wireframe", "wireframing", "mockup", "prototyping", "lo-fi design"],
        "Design Thinking":            ["design thinking", "human-centered design", "HCD", "double diamond"],
    },

    "leadership": {
        "Stakeholder Management":     ["stakeholder management", "stakeholder engagement", "stakeholder alignment", "executive communication", "managing up"],
        "Cross-functional Leadership":["cross-functional", "cross-functional leadership", "cross-team collaboration", "matrixed organization"],
        "Team Leadership":            ["team leadership", "team management", "people management", "direct reports", "team building"],
        "Mentoring":                  ["mentoring", "coaching", "mentorship", "people development"],
        "Executive Presentation":     ["executive presentation", "C-suite communication", "board presentation", "leadership updates"],
        "Influence Without Authority":["influence without authority", "persuasion", "soft influence", "consensus building"],
        "Vendor Management":          ["vendor management", "vendor evaluation", "third-party management", "partner management"],
        "Change Management":          ["change management", "organizational change", "transformation"],
    },

    "domain_knowledge": {
        "B2B SaaS":                   ["B2B", "B2B SaaS", "enterprise SaaS", "SaaS"],
        "B2C":                        ["B2C", "consumer", "consumer product"],
        "E-commerce":                 ["e-commerce", "ecommerce", "online marketplace", "retail tech"],
        "Fintech":                    ["fintech", "financial technology", "payments", "banking", "lending", "insurance tech"],
        "Healthcare/Healthtech":      ["healthcare", "healthtech", "health tech", "digital health", "medtech"],
        "EdTech":                     ["edtech", "education technology", "e-learning", "online learning"],
        "Marketplace":                ["marketplace", "two-sided marketplace", "platform business"],
        "AdTech":                     ["adtech", "ad tech", "advertising technology", "programmatic"],
        "DevTools":                   ["devtools", "developer tools", "developer experience", "DX", "platform engineering"],
        "Data Platform":              ["data platform", "data product", "data infrastructure"],
    },

    "program_project_management": {
        "Program Management":         ["program management", "programme management", "program manager", "PgM"],
        "Project Management":         ["project management", "project planning", "project execution"],
        "PMP":                        ["PMP", "Project Management Professional", "PMI"],
        "PRINCE2":                    ["PRINCE2"],
        "Risk Management":            ["risk management", "risk assessment", "risk mitigation", "RAID log"],
        "Resource Planning":          ["resource planning", "capacity planning", "resource allocation"],
        "Budget Management":          ["budget management", "budget planning", "cost management", "P&L ownership"],
        "Gantt/Timeline Planning":    ["Gantt", "Gantt chart", "timeline planning", "MS Project", "project timeline"],
        "Dependency Management":      ["dependency management", "cross-team dependencies", "dependency tracking"],
        "Status Reporting":           ["status reporting", "steering committee", "program review", "executive reporting"],
    },
}

# Build lookup index: lowercase synonym -> canonical skill name
_SYNONYM_INDEX: dict[str, str] = {}
_ALL_CANONICALS: list[str] = []

for _category, _skills in SKILL_TAXONOMY.items():
    for canonical, synonyms in _skills.items():
        _ALL_CANONICALS.append(canonical)
        _SYNONYM_INDEX[canonical.lower()] = canonical
        for syn in synonyms:
            _SYNONYM_INDEX[syn.lower()] = canonical


def canonicalize_skill(raw_skill: str) -> Optional[str]:
    """Map a raw skill string to its canonical name, or None if not in taxonomy."""
    return _SYNONYM_INDEX.get(raw_skill.lower().strip())


def get_all_canonical_skills() -> list[str]:
    """Return all canonical skill names."""
    return list(_ALL_CANONICALS)


def get_category_for_skill(canonical: str) -> Optional[str]:
    """Return which category a canonical skill belongs to."""
    for category, skills in SKILL_TAXONOMY.items():
        if canonical in skills:
            return category
    return None


class SkillCanonicalizer:
    """Canonicalize skills using taxonomy + embedding fallback.

    1. Try exact/synonym match from the taxonomy
    2. If no match, use embedding similarity against all canonical skill names
    3. Accept embedding match only if similarity > threshold
    """

    def __init__(self, embedding_model=None, similarity_threshold: float = 0.55):
        self._embedding_model = embedding_model
        self._threshold = similarity_threshold
        self._canonical_embeddings = None
        self._canonical_names = get_all_canonical_skills()

    def _ensure_embeddings(self):
        """Lazily compute embeddings for all canonical skills."""
        if self._canonical_embeddings is None and self._embedding_model is not None:
            self._canonical_embeddings = self._embedding_model.embed(self._canonical_names)

    def canonicalize(self, raw_skill: str) -> tuple[str, str]:
        """Canonicalize a single skill.

        Returns: (canonical_name, method)
            method: "exact" | "embedding" | "unmatched"
        """
        # 1. Taxonomy lookup
        canonical = canonicalize_skill(raw_skill)
        if canonical:
            return canonical, "exact"

        # 2. Embedding fallback
        if self._embedding_model is not None:
            self._ensure_embeddings()
            query_embedding = self._embedding_model.embed([raw_skill])[0]

            best_score = 0.0
            best_match = raw_skill
            for i, canon_emb in enumerate(self._canonical_embeddings):
                score = self._cosine_sim(query_embedding, canon_emb)
                if score > best_score:
                    best_score = score
                    best_match = self._canonical_names[i]

            if best_score >= self._threshold:
                logger.debug("skill_embedding_match",
                             raw=raw_skill, matched=best_match, score=round(best_score, 3))
                return best_match, "embedding"

        # 3. No match — keep raw skill as-is
        return raw_skill, "unmatched"

    def canonicalize_many(self, raw_skills: list[str]) -> list[dict]:
        """Canonicalize a list of skills. Returns list of {raw, canonical, method, category}."""
        results = []
        seen_canonicals = set()
        for raw in raw_skills:
            canonical, method = self.canonicalize(raw)
            if canonical not in seen_canonicals:  # Deduplicate
                seen_canonicals.add(canonical)
                results.append({
                    "raw": raw,
                    "canonical": canonical,
                    "method": method,
                    "category": get_category_for_skill(canonical) or "other",
                })
        return results

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x ** 2 for x in a) ** 0.5
        norm_b = sum(x ** 2 for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
