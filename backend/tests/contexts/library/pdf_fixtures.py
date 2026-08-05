"""Generated (never committed as binaries) PDF fixtures for
``test_pymupdf_extractor.py``, built with PyMuPDF itself (PLANS/phase-3.md
§10.1) -- it's already a runtime dependency, gives byte-level geometry
control, and keeps this repo free of binary fixtures to keep in sync.

Each builder returns raw PDF bytes. ``CORRUPT_PDF_BYTES`` is a bytes literal
(as the plan calls for) since a corrupt PDF cannot be produced by asking a
working PDF library to write one.
"""

from __future__ import annotations

import io

import pymupdf

PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0

CORRUPT_PDF_BYTES = b"%PDF-1.7\n<<not really a pdf>>"

# A hand-built, structurally-valid-but-minimal PDF whose Pages tree declares
# zero pages -- PyMuPDF opens this without error (its parser is lenient about
# a missing/short xref) and reports page_count == 0. Cannot be produced via
# pymupdf.Document.save(), which explicitly refuses "cannot save with zero
# pages".
EMPTY_PDF_BYTES = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [] /Count 0 >>
endobj
trailer
<< /Size 3 /Root 1 0 R >>
%%EOF
"""


def _save(doc: pymupdf.Document, **kwargs: object) -> bytes:
    buf = io.BytesIO()
    doc.save(buf, **kwargs)
    return buf.getvalue()


def simple_text_pdf(
    text: str = (
        "Hello, this is a simple test document. It has enough words to clear the "
        "extractor's minimum-text-length threshold comfortably."
    ),
) -> bytes:
    """1 page, 11pt body -- the happy-path / offset-mapping fixture."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text((72, 100), text, fontsize=11)
    return _save(doc)


def multipage_pdf(pages: int = 5) -> bytes:
    """N pages, each with distinct body text -- exercises page mapping and
    chunks that span multiple pages."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        page.insert_text(
            (72, 100),
            f"This is the body content of page {i + 1} of {pages}. "
            f"It contains a few sentences so extraction has something real to work with.",
            fontsize=11,
        )
    return _save(doc)


def headered_pdf(pages: int = 6) -> bytes:
    """A running header + a footer page number on every page -- the
    header/footer edge case, exercised end to end through the real
    extractor (not just the pure ``domain/layout.py`` unit tests)."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        # Header band: y1 <= 0.08 * 842 ~= 67.4 -- baseline well above that.
        page.insert_text((72, 30), "Bookloud User Guide", fontsize=10)
        # Body, well inside the middle of the page.
        page.insert_text(
            (72, 400),
            f"Chapter content for page {i + 1}. This paragraph is the real body text.",
            fontsize=11,
        )
        # Footer band: y0 >= 0.92 * 842 ~= 774.6 -- baseline near the bottom.
        page.insert_text((72, 820), f"Page {i + 1}", fontsize=9)
    return _save(doc)


def footnote_pdf() -> bytes:
    """An inline superscript marker + a small-font footnote block at the
    bottom of the page, alongside plenty of ordinary 11pt body text so the
    document's modal span size stays 11 despite the footnote content."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    # Plenty of 11pt body text so it dominates the char-weighted mode.
    page.insert_text(
        (72, 100),
        "This is the first line of ordinary body text on the page.",
        fontsize=11,
    )
    page.insert_text(
        (72, 116),
        "This is the second line, continuing the same paragraph block.",
        fontsize=11,
    )
    # A third body line carrying an inline superscript footnote marker
    # ("12") right after "claim" -- smaller font, raised (smaller origin_y).
    page.insert_text((72, 132), "This sentence makes a claim", fontsize=11)
    page.insert_text((72 + 155, 132 - 3), "12", fontsize=6)
    page.insert_text((72 + 168, 132), " that needs support.", fontsize=11)
    # The footnote block itself: bottom quarter of the page (y0 >= 0.75 *
    # 842 ~= 631.5), small font, and NOT the only block on the page.
    page.insert_text(
        (72, 700),
        "12. A footnote explaining the claim made above in more detail.",
        fontsize=8,
    )
    return _save(doc)


def hyphenated_pdf() -> bytes:
    """A word broken across a line boundary with a trailing hyphen --
    exercises de-hyphenation joining."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text((72, 100), "This is an exam-", fontsize=11)
    page.insert_text((72, 116), "ple of hyphenation across a line break.", fontsize=11)
    page.insert_text(
        (72, 140),
        "Additional filler text follows so the page clears the minimum text length.",
        fontsize=11,
    )
    return _save(doc)


def long_paragraph_pdf(chars: int = 8000) -> bytes:
    """A single paragraph long enough to force the sentence-split chunking
    path. Rendered on one tall custom page (rather than PyMuPDF's default
    letter size) so the whole paragraph lands in one text block -- if it
    instead spilled onto a second physical page, the extractor's per-page
    "\\n\\n" join would inject a spurious paragraph break in the middle of
    the sentence stream, defeating the point of this fixture."""
    sentence = "This is one sentence in a very long paragraph that keeps going on and on. "
    text = (sentence * (chars // len(sentence) + 1))[:chars]
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=6000)
    rect = pymupdf.Rect(72, 72, PAGE_WIDTH - 72, 5900)
    page.insert_textbox(rect, text, fontsize=11)
    return _save(doc)


def encrypted_pdf(user_password: str = "user-pass", owner_password: str = "owner-pass") -> bytes:
    """AES-256, a real user password -- ``needs_pass`` is true and
    ``authenticate("")`` must fail."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text((72, 100), "Secret contents.", fontsize=11)
    return _save(
        doc,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw=owner_password,
        user_pw=user_password,
        permissions=int(
            pymupdf.PDF_PERM_ACCESSIBILITY
            | pymupdf.PDF_PERM_PRINT
            | pymupdf.PDF_PERM_COPY
            | pymupdf.PDF_PERM_ANNOTATE
        ),
    )


def owner_password_pdf(owner_password: str = "owner-pass") -> bytes:
    """Owner password only (empty user password) -- PyMuPDF's
    ``needs_pass`` is already false for this, and ``authenticate("")``
    succeeds regardless; a regression guard that this case must extract
    normally, not be treated as ``ENCRYPTED_PDF``."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text(
        (72, 100),
        "Owner-protected but readable contents. No user password is required to open "
        "or extract text from this document, only to change its permissions.",
        fontsize=11,
    )
    return _save(
        doc,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw=owner_password,
        user_pw="",
        permissions=int(
            pymupdf.PDF_PERM_ACCESSIBILITY
            | pymupdf.PDF_PERM_PRINT
            | pymupdf.PDF_PERM_COPY
            | pymupdf.PDF_PERM_ANNOTATE
        ),
    )


def image_only_pdf() -> bytes:
    """A page with an inserted pixmap and no text at all -- the scanned-
    image ``NO_TEXT_LAYER`` case."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
    pixmap.set_rect(pixmap.irect, (200, 200, 200))
    page.insert_image(pymupdf.Rect(50, 50, 500, 700), pixmap=pixmap)
    return _save(doc)


def empty_pdf() -> bytes:
    """0 pages -- ``EMPTY_PDF``."""
    return EMPTY_PDF_BYTES
