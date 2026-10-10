from datetime import date

import pytest
from pdfgen import make_pdf

from crisisgrid_api.ingest.chunk import chunk_pages
from crisisgrid_api.ingest.embed import HashEmbedder, InMemoryVectorIndex
from crisisgrid_api.ingest.pdf import PdfError, ingest_pdf

PAGE1 = ["Deep Residual Learning (CIFAR-10 study)", "We train ResNet-20 with a learning rate of 0.1 and batch size 128."]
PAGE2 = ["Results: our model reaches 91.25% top-1 accuracy.", "Code: https://github.com/acme/resnet-cifar"]


def test_ingest_extracts_text_hash_and_date():
    data = make_pdf([PAGE1, PAGE2], creation_date="D:20240315120000Z")
    p = ingest_pdf(data)
    assert p.n_pages == 2 and "learning rate of 0.1" in p.pages[0] and "github.com/acme/resnet-cifar" in p.pages[1]
    assert p.metadata_date == date(2024, 3, 15) and not p.needs_ocr
    assert len(p.paper_hash) == 64 and p.paper_hash == ingest_pdf(data).paper_hash
    assert ingest_pdf(make_pdf([PAGE1])).paper_hash != p.paper_hash  # a different paper version has a different pin


def test_ingest_flags_scanned_pdf_instead_of_returning_empty_text():
    scanned = make_pdf([[""], [""]])
    assert ingest_pdf(scanned).needs_ocr is True
    used = ingest_pdf(scanned, ocr=lambda b: ["recovered text " * 10, "more " * 20])
    assert used.ocr_used and not used.needs_ocr and "recovered" in used.text


@pytest.mark.parametrize("junk", [b"", b"not a pdf at all", b"%PDF-1.4\ngarbage"])
def test_ingest_rejects_unreadable_input(junk):
    with pytest.raises(PdfError):
        ingest_pdf(junk)


def test_chunking_covers_everything_with_overlap_and_pages():
    pages = [" ".join(f"w{i}" for i in range(0, 400)), " ".join(f"v{i}" for i in range(0, 400))]
    chunks = chunk_pages(pages, max_chars=300, overlap=60)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(len(c.text) <= 300 for c in chunks)
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 2
    assert any(c.page_start == 1 and c.page_end == 2 for c in chunks)  # a chunk spans the page boundary
    words = " ".join(c.text for c in chunks).split()
    assert {"w0", "w399", "v0", "v399"} <= set(words)
    assert set(chunks[0].text.split()[-3:]) & set(chunks[1].text.split())  # overlap


def test_chunking_edge_cases():
    assert chunk_pages([]) == [] and chunk_pages([""]) == []
    [c] = chunk_pages(["short text"])
    assert c.text == "short text"
    assert len(chunk_pages(["x" * 5000], max_chars=100, overlap=10)) == 1  # one unbreakable token still terminates
    with pytest.raises(ValueError):
        chunk_pages(["a"], max_chars=10, overlap=10)


def test_vector_index_retrieves_relevant_chunk_deterministically():
    texts = ["optimizer SGD learning rate momentum weight decay schedule", "related work on attention and transformers",
             "dataset split train validation test images", "we thank the reviewers for feedback"]
    chunks = chunk_pages([" ".join(texts)], max_chars=70, overlap=0)
    idx = InMemoryVectorIndex(HashEmbedder())
    idx.add(chunks)
    top = idx.search("what learning rate and optimizer", k=1)[0]
    assert "learning rate" in top.text
    assert [c.index for c in idx.search("dataset split", k=3)] == [c.index for c in idx.search("dataset split", k=3)]
