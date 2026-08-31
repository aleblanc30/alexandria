"""Image response models."""

from pydantic import BaseModel


class ImageOut(BaseModel):
    id: int
    path: str
    filename: str
    image_type: str | None
    width: int | None
    height: int | None
    description: str | None
    ocr_text: str | None
    date_taken: int | None
    tags: list[str]
    similarity: float | None = None
    # Which search path produced this hit: "clip" (visual), "text"
    # (inferred content/description/OCR), or "clip+text". None outside search.
    matched_by: str | None = None
