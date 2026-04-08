"""ChromaDB vector store for resume RAG.

Chunks the candidate profile into embeddable segments (per-project,
per-experience, skills summary) so the matching agent can retrieve
the most relevant resume context for each job description.
"""

import json
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings

import structlog

from src.core.embeddings import EmbeddingModel

logger = structlog.get_logger()


class ResumeVectorStore:
    """Store and query resume chunks via ChromaDB."""

    COLLECTION_NAME = "resume_chunks"

    def __init__(self, config: dict, embedding_model: EmbeddingModel):
        self.embedding_model = embedding_model
        chroma_dir = config.get("output", {}).get("chroma_dir", "./data/chroma")
        Path(chroma_dir).mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(path=chroma_dir)
        self._collection = None
        logger.info("vectorstore_initialized", path=chroma_dir)

    @property
    def collection(self):
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def index_profile(self, profile: dict, stories_file: str = "") -> int:
        """Chunk and index a candidate profile + optional stories. Returns chunks indexed."""
        chunks = self._chunk_profile(profile)

        # Also index PM stories if available
        if stories_file:
            story_chunks = self._chunk_stories_file(stories_file)
            chunks.extend(story_chunks)

        if not chunks:
            logger.warning("vectorstore_no_chunks")
            return 0

        # Clear and re-index
        existing = self.collection.count()
        if existing > 0:
            self.client.delete_collection(self.COLLECTION_NAME)
            self._collection = None

        texts = [c["text"] for c in chunks]
        ids = [c["id"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]
        embeddings = self.embedding_model.embed(texts)

        self.collection.add(
            documents=texts,
            embeddings=embeddings,
            ids=ids,
            metadatas=metadatas,
        )

        logger.info("vectorstore_indexed", chunks=len(chunks),
                     profile_chunks=len(self._chunk_profile(profile)),
                     story_chunks=len(chunks) - len(self._chunk_profile(profile)))
        return len(chunks)

    def query(self, jd_text: str, top_k: int = 3) -> list[dict]:
        """Find the most relevant resume chunks for a job description.

        Returns list of {text, metadata, score} sorted by relevance.
        """
        if self.collection.count() == 0:
            return []

        query_embedding = self.embedding_model.embed([jd_text[:1000]])[0]

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self.collection.count()),
        )

        chunks = []
        for i in range(len(results["ids"][0])):
            chunks.append({
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                "score": 1 - results["distances"][0][i],  # Convert distance to similarity
            })

        return chunks

    def _chunk_profile(self, profile: dict) -> list[dict]:
        """Break a candidate profile into embeddable chunks."""
        chunks = []

        # 1. Professional summary
        summary = profile.get("summary", "")
        if summary:
            chunks.append({
                "id": "summary",
                "text": f"Professional summary: {summary}",
                "metadata": {"type": "summary"},
            })

        # 2. Skills overview
        all_skills = profile.get("all_skills_canonical", [])
        if all_skills:
            chunks.append({
                "id": "skills_all",
                "text": f"Skills and expertise: {', '.join(all_skills)}",
                "metadata": {"type": "skills"},
            })

        # 3. Skills by category
        skills_canonical = profile.get("skills_canonical", {})
        for category, skills in skills_canonical.items():
            if skills:
                chunks.append({
                    "id": f"skills_{category}",
                    "text": f"{category.replace('_', ' ').title()} skills: {', '.join(skills)}",
                    "metadata": {"type": "skills", "category": category},
                })

        # 4. Each work experience as a chunk
        for i, exp in enumerate(profile.get("experience", [])):
            company = exp.get("company", "")
            title = exp.get("title", "")
            duration = exp.get("duration", "")
            highlights = exp.get("highlights", [])
            skills_demo = exp.get("skills_demonstrated", [])

            text = f"{title} at {company} ({duration})"
            if highlights:
                text += ". " + ". ".join(highlights)
            if skills_demo:
                text += f". Skills: {', '.join(skills_demo)}"

            chunks.append({
                "id": f"exp_{i}",
                "text": text,
                "metadata": {"type": "experience", "company": company, "title": title},
            })

        # 5. Each project as a chunk
        for i, proj in enumerate(profile.get("projects", [])):
            name = proj.get("name", "")
            desc = proj.get("description", "")
            impact = proj.get("impact", "")
            skills_used = proj.get("skills_used", [])

            text = f"Project: {name}. {desc}"
            if impact:
                text += f" Impact: {impact}"
            if skills_used:
                text += f". Skills: {', '.join(skills_used)}"

            chunks.append({
                "id": f"proj_{i}",
                "text": text,
                "metadata": {"type": "project", "name": name},
            })

        # 6. Education + certifications
        education = profile.get("education", [])
        certs = profile.get("certifications", [])
        if education or certs:
            edu_text = ""
            for e in education:
                edu_text += f"{e.get('degree', '')} from {e.get('institution', '')} ({e.get('year', '')}). "
            if certs:
                edu_text += f"Certifications: {', '.join(certs)}"
            chunks.append({
                "id": "education",
                "text": edu_text.strip(),
                "metadata": {"type": "education"},
            })

        return chunks

    def _chunk_stories_file(self, filepath: str) -> list[dict]:
        """Chunk a PM stories markdown file into embeddable segments.

        Each story (### heading) becomes a separate chunk for RAG retrieval.
        """
        import re
        try:
            text = Path(filepath).read_text()
        except Exception as e:
            logger.warning("stories_file_read_failed", path=filepath, error=str(e))
            return []

        chunks = []
        # Split by ### headings (story boundaries)
        stories = re.split(r'\n###\s+', text)

        for i, story in enumerate(stories):
            if not story.strip() or len(story.strip()) < 50:
                continue

            lines = story.strip().split('\n')
            title = lines[0].strip().lstrip('#').strip()
            story_text = story.strip()[:1500]

            chunks.append({
                "id": f"story_{i}",
                "text": f"PM Story: {title}. {story_text}",
                "metadata": {"type": "story", "title": title},
            })

        logger.info("stories_chunked", file=filepath, chunks=len(chunks))
        return chunks
