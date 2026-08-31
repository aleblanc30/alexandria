"""Shared text assembly for arXiv / bioRxiv fetch handlers."""

from __future__ import annotations


def build_preprint_text(
    *,
    title: str,
    authors: list[str] | str,
    abstract: str,
    pdf_text: str | None,
) -> str:
    """Combine metadata and PDF body for embedding; skip PDF lead when it repeats the abstract."""
    parts: list[str] = []
    if title.strip():
        parts.append(title.strip())
    if authors:
        if isinstance(authors, list):
            author_line = ", ".join(a for a in authors if a.strip())
        else:
            author_line = authors.strip()
        if author_line:
            parts.append(f"by {author_line}")
    abstract = abstract.strip()
    if abstract:
        parts.append(abstract)

    body = (pdf_text or "").strip()
    if body:
        if abstract and body.startswith(abstract[: min(len(abstract), 200)]):
            pass
        elif abstract and abstract in body[: len(abstract) + 80]:
            pass
        else:
            parts.append(body)

    return "\n\n".join(parts)
