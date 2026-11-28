#!/usr/bin/env python3
"""Extract text from ODRL research PDFs into research_papers/extracted/."""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "pdfs"
OUT_DIR = ROOT / "extracted"


def extract_pdf(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    if doc.is_encrypted:
        raise RuntimeError(f"{pdf_path.name} is encrypted")
    if doc.page_count == 0:
        raise RuntimeError(f"{pdf_path.name} has zero pages")
    parts: list[str] = []
    parts.append(f"# SOURCE: {pdf_path.name}")
    parts.append(f"# PAGES: {doc.page_count}")
    parts.append("")
    for i, page in enumerate(doc, start=1):
        parts.append(f"\n\n===== PAGE {i} / {doc.page_count} =====\n")
        text = page.get_text("text")
        if not text or not text.strip():
            # Fallback: try blocks to recover layout
            blocks = page.get_text("blocks")
            text = "\n".join(b[4] for b in blocks if len(b) > 4 and isinstance(b[4], str))
        parts.append(text.strip())
    doc.close()
    return "\n".join(parts).rstrip() + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        raise SystemExit("No PDFs found")
    for pdf in pdfs:
        header = pdf.read_bytes()[:8]
        if not header.startswith(b"%PDF"):
            raise RuntimeError(f"{pdf.name} is not a PDF (header={header!r})")
        size = pdf.stat().st_size
        if size < 10_000:
            raise RuntimeError(f"{pdf.name} is suspiciously small ({size} bytes)")
        text = extract_pdf(pdf)
        out = OUT_DIR / f"{pdf.stem}.txt"
        out.write_text(text, encoding="utf-8")
        # page count from first lines
        pages_line = next(line for line in text.splitlines() if line.startswith("# PAGES:"))
        print(f"OK {pdf.name:24} {size:8d} bytes  {pages_line}  extracted={out.stat().st_size} chars")


if __name__ == "__main__":
    main()
