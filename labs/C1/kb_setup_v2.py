"""
ISDO Lab C1 — Knowledge Base Setup with ChromaDB
------------------------------------------------
1. Reads all .md files from data/kb/
2. Splits each article into chunks at '## ' headings
3. Stores every chunk in the ChromaDB collection 'isdo_kb'
4. Runs 4 sample queries and prints the best-matching article + confidence

Dependency: chromadb only  (pip install chromadb)
Run from anywhere:  python labs/C1/kb_setup_v2.py
Note: the first run downloads Chroma's default embedding model (~80 MB).
"""

import sys
from pathlib import Path

import chromadb

# Make ✓ / ✅ safe on Windows consoles and redirected output
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Paths are resolved from this file, so the script works from any working dir.
# labs/C1/kb_setup_v2.py -> project root is two levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_DIR = PROJECT_ROOT / "data" / "kb"
CHROMA_DIR = PROJECT_ROOT / "data" / "chroma_db"   # persisted for Labs C4/C6/C8
COLLECTION = "isdo_kb"

TEST_QUERIES = [
    "VPN not connecting after password change",
    "How do I reset an Active Directory password?",
    "SAP ERP login failure for multiple users",
    "Network switch down, building offline",
]


def chunk_article(text: str, filename: str) -> list[dict]:
    """Split one markdown article at '## ' headings (### stays inside its section)."""
    lines = text.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), filename)

    chunks, current, heading = [], [], "Overview"
    for line in lines:
        if line.startswith("## "):
            if "\n".join(current).strip():
                chunks.append((heading, current))
            heading, current = line[3:].strip(), []
        current.append(line)
    if "\n".join(current).strip():
        chunks.append((heading, current))

    return [
        {
            # Prefix the article title so each chunk keeps its context when embedded
            "content": f"{title}\n\n" + "\n".join(body).strip(),
            "filename": filename,
            "title": title,
            "heading": heading,
        }
        for heading, body in chunks
    ]


def main() -> None:
    print("=" * 55)
    print("ISDO Lab C1 — KB Setup with ChromaDB")
    print("=" * 55, "\n")

    # ── Step 1: load markdown files ─────────────────────────────────────────
    md_files = sorted(KB_DIR.glob("*.md"))
    if not md_files:
        sys.exit(f"ERROR: no .md files found in {KB_DIR}")
    print(f"Step 1: Found {len(md_files)} KB articles in {KB_DIR}")
    for f in md_files:
        print(f"  ✓ {f.name}")
    print()

    # ── Step 2: chunk at ## headings ────────────────────────────────────────
    all_chunks = []
    for f in md_files:
        all_chunks.extend(chunk_article(f.read_text(encoding="utf-8"), f.name))
    print(f"Step 2: Split into {len(all_chunks)} chunks")
    for f in md_files:
        n = sum(c["filename"] == f.name for c in all_chunks)
        print(f"  {f.name:<28} {n} chunks")
    print()

    # ── Step 3: store in ChromaDB ───────────────────────────────────────────
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        client.delete_collection(COLLECTION)  # clean rebuild on re-run
    except Exception:
        pass
    # Cosine distance lies in [0, 2]; similarity = 1 - distance gives a usable confidence
    kb = client.create_collection(name=COLLECTION, metadata={"hnsw:space": "cosine"})

    kb.add(
        ids=[f"{c['filename'].removesuffix('.md')}::{i:02d}" for i, c in enumerate(all_chunks)],
        documents=[c["content"] for c in all_chunks],
        metadatas=[{"filename": c["filename"], "title": c["title"], "heading": c["heading"]}
                   for c in all_chunks],
    )
    print(f"Step 3: Stored {kb.count()} chunks in collection '{COLLECTION}'")
    print(f"        Persisted at {CHROMA_DIR}\n")

    # ── Step 4: test queries ────────────────────────────────────────────────
    print("Step 4: Testing KB with sample queries")
    print("-" * 55)
    for query in TEST_QUERIES:
        res = kb.query(query_texts=[query], n_results=1)
        if not res["ids"][0]:
            print(f"  Q: {query}\n     → No match found\n")
            continue
        meta = res["metadatas"][0][0]
        confidence = max(0.0, 1.0 - res["distances"][0][0])
        print(f"  Q: {query}")
        print(f"     → {meta['filename']}  ({meta['title']})")
        print(f"       section: {meta['heading']}  |  confidence: {confidence:.0%}\n")

    print("✅ KB setup complete — collection 'isdo_kb' is ready for Labs C4, C6 and C8.")


if __name__ == "__main__":
    main()
