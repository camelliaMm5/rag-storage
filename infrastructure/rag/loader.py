import os
import re
from .models import Document


def _parse_filename(filename: str) -> dict:
    """Extract product info from filename like 'Doc1-X1智能门锁FAQ.md'."""
    name = os.path.splitext(filename)[0]
    name = re.sub(r"^Doc\d+-", "", name)
    name = re.sub(r"FAQ$", "", name)
    # Product code: "X1" from "X1智能门锁", "" for general docs
    code_match = re.search(r"([A-Z]+\d+)", name)
    product_code = code_match.group(1) if code_match else ""
    return {
        "product": name,
        "product_code": product_code,
        "source_file": filename,
    }


def load_file(filepath: str) -> Document | None:
    """Load a single .md file, return a Document with metadata."""
    filename = os.path.basename(filepath)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    if not text.strip():
        return None

    metadata = _parse_filename(filename)
    doc_id = os.path.splitext(filename)[0]
    return Document(text=text.strip(), metadata=metadata, doc_id=doc_id)


def load_dir(dirpath: str) -> list[Document]:
    """Scan directory for .md files, load all valid documents."""
    documents = []
    if not os.path.isdir(dirpath):
        return documents

    for filename in sorted(os.listdir(dirpath)):
        if filename.endswith(".md"):
            filepath = os.path.join(dirpath, filename)
            doc = load_file(filepath)
            if doc is not None:
                documents.append(doc)
    return documents
