"""Tiny PDF writer for tests (text only, Helvetica), so ingest is tested on real PDF bytes."""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], *, creation_date: str | None = None) -> bytes:
    objs: dict[int, bytes] = {}
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    for i, lines in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        objs[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                         f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>").encode()
        body = "BT /F1 11 Tf 14 TL 50 740 Td\n" + "\n".join(f"({_esc(ln)}) Tj T*" for ln in lines) + "\nET"
        objs[content_id] = f"<< /Length {len(body)} >>\nstream\n{body}\nendstream".encode()
    objs[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    info_id = font_id + 1
    if creation_date:
        objs[info_id] = f"<< /CreationDate ({creation_date}) >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + objs[oid] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for oid in range(1, size):
        out += f"{offsets[oid]:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {size} /Root 1 0 R" + (f" /Info {info_id} 0 R" if creation_date else "") + f" >>\nstartxref\n{xref}\n%%EOF\n"
    return bytes(out) + trailer.encode()
