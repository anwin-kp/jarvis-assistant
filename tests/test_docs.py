import numpy as np

from jarvis.docs.index import DocIndex, chunk_text
from jarvis.memory.store import Memory

VOCAB = ["rent", "notice", "deposit", "laptop", "warranty", "battery"]


def fake_embed(texts):  # bag-of-words "embedding" so the test needs no model
    return np.array([[t.lower().count(w) for w in VOCAB] for t in texts], dtype=np.float32) + 1e-3


def test_chunking_overlaps_and_covers_text():
    text = "Sentence number one. " * 200
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 5 and all(len(c) <= 300 for c in chunks)
    assert chunk_text("") == []


def test_index_and_search(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "rental.txt").write_text("The notice period for rent is two months. Deposit is refundable.")
    (docs / "laptop.md").write_text("Laptop warranty covers the battery for one year.")
    idx = DocIndex(Memory(tmp_path / "m.db"), [docs], fake_embed)
    assert "Indexed 2" in idx.index()
    assert idx.search("what is the notice period for rent")[0]["file"] == "rental.txt"
    assert idx.search("battery warranty")[0]["file"] == "laptop.md"
    assert "Indexed 0" in idx.index()  # unchanged files are skipped
    (docs / "laptop.md").unlink()
    assert "removed 1" in idx.index()
