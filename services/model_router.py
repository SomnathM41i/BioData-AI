"""
services/model_router.py — Intelligent file-type → model routing

Routing table:
  image   → ImageProcessor  (OCR via pytesseract → Groq LLM)
  pdf     → PdfProcessor    (PyMuPDF text → Groq LLM)
  docx    → DocxProcessor   (python-docx text → Groq LLM)
  txt     → TxtProcessor    (plain text → Groq LLM)

New processors can be added by:
  1. Creating a class that inherits BaseProcessor
  2. Registering it in ROUTER_MAP
"""
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  Base processor interface
# ──────────────────────────────────────────────────────────────────────────────
class BaseProcessor(ABC):
    """
    Contract every file-type processor must fulfill.
    Each processor converts a file into a list of (page_num, text) tuples,
    which are then fed to the Groq LLM extractor.
    """

    @abstractmethod
    def extract_pages(self, file_path: str, max_chars: int = 5000) -> list[tuple[int, str]]:
        """
        Return a list of (page_number, text_content) tuples.
        Each page is processed independently by the LLM.
        """

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name shown in logs."""


# ──────────────────────────────────────────────────────────────────────────────
#  Concrete processors
# ──────────────────────────────────────────────────────────────────────────────
class PdfProcessor(BaseProcessor):
    display_name = "PDF Processor (PyMuPDF)"

    def extract_pages(self, file_path: str, max_chars: int = 5000):
        import fitz   # PyMuPDF
        doc    = fitz.open(file_path)
        total  = len(doc)
        pages  = []
        for i in range(total):
            text = doc[i].get_text().strip()
            if len(text) > 100:          # skip near-empty pages
                pages.append((i + 1, text[:max_chars]))
        logger.info("PDF '%s': %d/%d non-empty pages", os.path.basename(file_path), len(pages), total)
        return pages


class DocxProcessor(BaseProcessor):
    display_name = "DOCX/DOC Processor (python-docx)"

    def extract_pages(self, file_path: str, max_chars: int = 5000):
        import docx as _docx
        doc  = _docx.Document(file_path)
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        logger.info("DOCX '%s': %d chars", os.path.basename(file_path), len(text))
        return [(1, text[:max_chars])]


class TxtProcessor(BaseProcessor):
    display_name = "Plain Text Processor"

    def extract_pages(self, file_path: str, max_chars: int = 5000):
        with open(file_path, "r", errors="ignore") as fh:
            text = fh.read().strip()
        logger.info("TXT '%s': %d chars", os.path.basename(file_path), len(text))
        return [(1, text[:max_chars])]


class ImageProcessor(BaseProcessor):
    """
    Uses pytesseract for OCR, then passes extracted text to the LLM.
    Falls back to a description prompt if OCR yields too little text.
    """
    display_name = "Image Processor (OCR + LLM)"

    def extract_pages(self, file_path: str, max_chars: int = 5000):
        try:
            import pytesseract
            from PIL import Image
            img  = Image.open(file_path)
            text = pytesseract.image_to_string(img).strip()
            if len(text) < 50:
                logger.warning("OCR yielded very little text for '%s' (%d chars)",
                               os.path.basename(file_path), len(text))
                text = f"[Image file: {os.path.basename(file_path)}. OCR output: {text}]"
        except ImportError:
            logger.warning("pytesseract not installed; using filename stub for image.")
            text = f"[Image file: {os.path.basename(file_path)} — install pytesseract for OCR]"
        except Exception as exc:
            logger.error("OCR failed for '%s': %s", file_path, exc)
            text = f"[OCR error on {os.path.basename(file_path)}: {exc}]"

        logger.info("Image '%s': %d OCR chars", os.path.basename(file_path), len(text))
        return [(1, text[:max_chars])]


# ──────────────────────────────────────────────────────────────────────────────
#  Router
# ──────────────────────────────────────────────────────────────────────────────

# Map category string → processor class (add new entries here to extend)
ROUTER_MAP: dict[str, type[BaseProcessor]] = {
    "pdf":   PdfProcessor,
    "docx":  DocxProcessor,
    "txt":   TxtProcessor,
    "image": ImageProcessor,
}


class ModelRouter:
    """
    Given a file category, return the appropriate processor instance.
    Raises ValueError for unknown categories.
    """

    def __init__(self):
        self._processors: dict[str, BaseProcessor] = {}   # lazy-init cache

    def get_processor(self, category: str) -> BaseProcessor:
        if category not in ROUTER_MAP:
            raise ValueError(
                f"No processor registered for category '{category}'. "
                f"Known categories: {list(ROUTER_MAP.keys())}"
            )
        if category not in self._processors:
            self._processors[category] = ROUTER_MAP[category]()
            logger.info("Initialised processor: %s", self._processors[category].display_name)
        return self._processors[category]

    def extract_pages(self, file_path: str, category: str, max_chars: int = 5000):
        """Convenience: route + extract in one call."""
        processor = self.get_processor(category)
        logger.info("Routing '%s' → %s", os.path.basename(file_path), processor.display_name)
        return processor.extract_pages(file_path, max_chars)

    @staticmethod
    def supported_categories() -> list[str]:
        return list(ROUTER_MAP.keys())


# Singleton for use across the app
model_router = ModelRouter()
