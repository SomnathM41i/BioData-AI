import os

def read_pdf(path, page_range=None):
    import fitz
    doc   = fitz.open(path)
    total = len(doc)
    start, end = 0, total
    if page_range:
        try:
            parts = page_range.split("-")
            start = int(parts[0]) - 1
            end   = int(parts[1]) if len(parts) > 1 else total
        except Exception:
            pass
    pages = []
    for i in range(start, min(end, total)):
        text = doc[i].get_text().strip()
        if len(text) > 100:
            pages.append((i + 1, text))
    return pages, total

def read_docx(path):
    import docx
    doc  = docx.Document(path)
    text = "\n".join(p.text for p in doc.paragraphs).strip()
    return [(1, text)], 1

def read_txt(path):
    with open(path, "r", errors="ignore") as f:
        text = f.read().strip()
    return [(1, text)], 1

def get_pages(path, page_range=None):
    ext = path.lower()
    if ext.endswith(".pdf"):  return read_pdf(path, page_range)
    if ext.endswith(".docx"): return read_docx(path)
    return read_txt(path)

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt")
