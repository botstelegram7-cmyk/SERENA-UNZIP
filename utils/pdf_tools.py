# utils/pdf_tools.py
import os
from pathlib import Path
from typing import List, Tuple, Optional

from PyPDF2 import PdfReader, PdfWriter, PdfMerger


def get_pdf_info(pdf_path: str) -> dict:
    """Return basic info about a PDF."""
    reader = PdfReader(pdf_path)
    return {
        "pages": len(reader.pages),
        "size_mb": round(os.path.getsize(pdf_path) / (1024 * 1024), 2),
        "encrypted": reader.is_encrypted,
        "title": reader.metadata.title if reader.metadata else None,
    }


def split_pdf(pdf_path: str, output_dir: str, pages_per_part: int = 1) -> List[str]:
    """
    Split a PDF into parts of `pages_per_part` pages each.
    Returns list of output file paths.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    reader = PdfReader(pdf_path)
    total  = len(reader.pages)
    parts  = []

    base = Path(pdf_path).stem
    i = 0
    part_num = 1
    while i < total:
        writer = PdfWriter()
        for j in range(pages_per_part):
            if i + j < total:
                writer.add_page(reader.pages[i + j])
        out_path = os.path.join(output_dir, f"{base}_part{part_num:03d}.pdf")
        with open(out_path, "wb") as f:
            writer.write(f)
        parts.append(out_path)
        i += pages_per_part
        part_num += 1

    return parts


def split_pdf_by_range(pdf_path: str, output_dir: str, ranges: List[Tuple[int, int]]) -> List[str]:
    """
    Split PDF by page ranges (1-indexed, inclusive).
    ranges = [(1, 5), (6, 10), ...]
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    reader = PdfReader(pdf_path)
    base   = Path(pdf_path).stem
    parts  = []

    for idx, (start, end) in enumerate(ranges, 1):
        writer = PdfWriter()
        for pg_num in range(start - 1, min(end, len(reader.pages))):
            writer.add_page(reader.pages[pg_num])
        out_path = os.path.join(output_dir, f"{base}_pages{start}-{end}.pdf")
        with open(out_path, "wb") as f:
            writer.write(f)
        parts.append(out_path)

    return parts


def merge_pdfs(pdf_paths: List[str], output_path: str) -> str:
    """Merge multiple PDFs into one."""
    merger = PdfMerger()
    for p in pdf_paths:
        merger.append(p)
    with open(output_path, "wb") as f:
        merger.write(f)
    merger.close()
    return output_path


def extract_text_from_pdf(pdf_path: str, output_path: str) -> str:
    """Extract all text from PDF and save as .txt."""
    reader = PdfReader(pdf_path)
    lines = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        lines.append(f"=== Page {i} ===\n{text}\n")
    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    return output_path


def parse_page_ranges(input_str: str, total_pages: int) -> List[Tuple[int, int]]:
    """
    Parse user input like '1-5,7,10-15' into list of (start, end) tuples.
    Returns [] on parse error.
    """
    ranges = []
    try:
        parts = input_str.replace(" ", "").split(",")
        for part in parts:
            if "-" in part:
                s, e = part.split("-", 1)
                s, e = int(s), int(e)
                if 1 <= s <= e <= total_pages:
                    ranges.append((s, e))
            else:
                n = int(part)
                if 1 <= n <= total_pages:
                    ranges.append((n, n))
    except Exception:
        return []
    return ranges
