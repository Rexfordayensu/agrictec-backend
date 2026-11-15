"""The AgriCtec letterhead every downloadable PDF shares (fpdf2).

Each page starts with the same letterhead: the official AgriCtec mark, "AgriCtec" and
"DIGITAL IDENTITY" on the left; the document's title, its reference and when it was generated
on the right; a green rule beneath. Each page ends with the same footer: a rule, "AgriCtec
Digital Identity", the reference and "Page X of Y". Every document arranges its own content
below it, with the shared section headings, detail grids and tables here.

Only official information is printed: no address, registration number, signature or seal
(AgriCtec has none configured), and nothing is presented as a bank or lender document.

The built-in PDF fonts cover Latin-1 only, so other characters are mapped to close
equivalents (or "?") instead of failing."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF, FontFace
from fpdf.enums import TableBordersLayout, TableCellFillMode, XPos, YPos

MARK = Path(__file__).resolve().parents[1] / "assets" / "agrictec-mark.png"

GREEN = (16, 130, 50)  # #108232
GREEN_TINT = (219, 244, 231)  # #DBF4E7
INK = (17, 24, 39)  # #111827
MUTED = (100, 116, 139)  # #64748B
RULE = (226, 232, 240)
ROW_TINT = (247, 251, 249)
WHITE = (255, 255, 255)
# A totals row: bold, on the light green tint.
TOTAL_ROW = FontFace(emphasis="BOLD", fill_color=GREEN_TINT)

DISCLAIMER = (
    "This report was prepared by AgriCtec from financial records available in the farmer's "
    "account. It is not an official bank statement, independently audited financial statement, "
    "certification of creditworthiness, or loan approval."
)

_SWAPS = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
          "·": "-", "•": "-", "₵": "GHS ", "…": "...", "→": "->", " ": " "}


def clean(text: object) -> str:
    """Text the core PDF fonts can print."""
    value = "" if text is None else str(text)
    for a, b in _SWAPS.items():
        value = value.replace(a, b)
    return value.encode("latin-1", "replace").decode("latin-1")


def ghs(amount: object, *, prefix: bool = True) -> str:
    """Exact cedi amounts: "1234.5" -> "GHS 1,234.50" (never rounded beyond the pesewa)."""
    value = Decimal(str(amount or "0")).quantize(Decimal("0.01"))
    text = f"{abs(value):,.2f}"
    sign = "-" if value < 0 else ""
    return f"{sign}GHS {text}" if prefix else f"{sign}{text}"


class BrandedPDF(FPDF):
    """An A4 page with the AgriCtec letterhead and footer. `title` is the document's name in
    capitals (e.g. "FINANCIAL TRANSACTION STATEMENT"); `reference` identifies the document."""

    def __init__(self, *, title: str, reference: str, generated_at: datetime, orientation: str = "P"):
        super().__init__(orientation=orientation, unit="mm", format="A4")
        self.doc_title = clean(title)
        self.reference = clean(reference)
        self.generated_at = generated_at
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(auto=True, margin=20)
        self.set_title(f"AgriCtec - {title.title()} {reference}")
        self.set_author("AgriCtec")
        self.set_creator("AgriCtec Digital Identity")
        self.alias_nb_pages()

    @property
    def content_width(self) -> float:
        return self.w - self.l_margin - self.r_margin

    # ---------- letterhead and footer ----------

    def header(self) -> None:
        top = 11
        left = self.l_margin
        if MARK.exists():
            self.image(str(MARK), x=left, y=top, w=13, h=13)
        # "Agri" in ink, "Ctec" in green: the AgriCtec wordmark.
        self.set_xy(left + 15, top + 1.2)
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*INK)
        self.cell(self.get_string_width("Agri"), 6.5, "Agri")
        self.set_text_color(*GREEN)
        self.cell(self.get_string_width("Ctec"), 6.5, "Ctec")
        self.set_xy(left + 15.2, top + 8)
        self.set_font("Helvetica", "B", 6.5)
        self.set_text_color(*MUTED)
        self.set_char_spacing(1.2)
        self.cell(0, 3.5, "DIGITAL IDENTITY")
        self.set_char_spacing(0)

        right = self.w - self.r_margin
        block = 95
        self.set_xy(right - block, top + 0.8)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*INK)
        self.cell(block, 5, self.doc_title, align="R")
        self.set_xy(right - block, top + 6.2)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(*MUTED)
        self.cell(block, 3.8, f"Reference: {self.reference}", align="R")
        self.set_xy(right - block, top + 10)
        self.cell(block, 3.8, clean(f"Generated {self.generated_at:%d %b %Y, %H:%M} GMT"), align="R")

        self.set_draw_color(*GREEN)
        self.set_line_width(0.6)
        self.line(left, top + 17, right, top + 17)
        self.set_line_width(0.2)
        self.set_xy(left, top + 22)
        self.set_text_color(*INK)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_draw_color(*RULE)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)
        self.set_font("Helvetica", size=7.5)
        self.set_text_color(*MUTED)
        third = self.content_width / 3
        self.cell(third, 4, "AgriCtec Digital Identity")
        self.cell(third, 4, f"Ref {self.reference}", align="C")
        self.cell(third, 4, f"Page {self.page_no()} of {{nb}}", align="R")

    # ---------- shared building blocks ----------

    def document_title(self, text: str, subtitle: str | None = None) -> None:
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*INK)
        self.cell(0, 8, clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if subtitle:
            self.set_font("Helvetica", size=9)
            self.set_text_color(*MUTED)
            self.multi_cell(0, 4.6, clean(subtitle), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)

    def section(self, text: str, *, keep: float = 30) -> None:
        """A section heading: a short green bar and the name. Starts a new page rather than
        leaving a heading alone at the bottom of one (`keep` mm must fit after it)."""
        if self.get_y() + 9 + keep > self.page_break_trigger:
            self.add_page()
        self.ln(3)
        y = self.get_y()
        self.set_fill_color(*GREEN)
        self.rect(self.l_margin, y + 1, 1.4, 5, style="F")
        self.set_xy(self.l_margin + 3.5, y)
        self.set_font("Helvetica", "B", 10.5)
        self.set_text_color(*INK)
        self.cell(0, 7, clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def details(self, pairs: list[tuple[str, object]], *, columns: int = 2) -> None:
        """Labelled details in columns, on a light green panel. Empty values say "Not provided"."""
        pairs = [(label, clean(value) if value not in (None, "") else "Not provided") for label, value in pairs]
        gap = 6
        width = (self.content_width - 8 - gap * (columns - 1)) / columns
        rows = [pairs[i:i + columns] for i in range(0, len(pairs), columns)]
        self.set_font("Helvetica", size=9)
        heights = [max(self._lines(value, width) for _, value in row) * 4.4 + 4.5 for row in rows]
        total = sum(heights) + 4
        if self.get_y() + total > self.page_break_trigger:
            self.add_page()
        top = self.get_y()
        self.set_fill_color(*GREEN_TINT)
        self.rect(self.l_margin, top, self.content_width, total, style="F")
        y = top + 2.5
        for row, height in zip(rows, heights):
            for index, (label, value) in enumerate(row):
                x = self.l_margin + 4 + index * (width + gap)
                self.set_xy(x, y)
                self.set_font("Helvetica", "B", 7)
                self.set_text_color(*MUTED)
                self.cell(width, 3.6, clean(label).upper())
                self.set_xy(x, y + 3.8)
                self.set_font("Helvetica", size=9)
                self.set_text_color(*INK)
                self.multi_cell(width, 4.4, value, align="L")
            y += height
        self.set_xy(self.l_margin, top + total + 2)

    def note(self, text: str, *, italic: bool = True) -> None:
        """A short explanatory paragraph, kept in one piece (never split across pages)."""
        self.set_font("Helvetica", "I" if italic else "", 8)
        if self.get_y() + self._lines(clean(text), self.content_width) * 4.2 > self.page_break_trigger:
            self.add_page()
        self.set_font("Helvetica", "I" if italic else "", 8)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 4.2, clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*INK)
        self.ln(1)

    def table_style(self, *, col_widths, text_align, line_height: float = 5, font_size: float = 8.5) -> dict:
        """The shared table look: a green heading row repeated on every page, light rules
        between rows, faint alternating rows, rows never split across pages."""
        self.set_font("Helvetica", size=font_size)
        self.set_draw_color(*RULE)
        self.set_text_color(*INK)
        # The faint row tint (the table falls back on the current fill colour).
        self.set_fill_color(*ROW_TINT)
        return {
            "col_widths": col_widths,
            "text_align": text_align,
            "line_height": line_height,
            "padding": (1.6, 2),
            "first_row_as_headings": True,
            "headings_style": FontFace(emphasis="BOLD", color=WHITE, fill_color=GREEN, size_pt=8),
            "cell_fill_color": ROW_TINT,
            "cell_fill_mode": TableCellFillMode.EVEN_ROWS,
            "borders_layout": TableBordersLayout.HORIZONTAL_LINES,
            "width": self.content_width,
        }

    def disclaimer(self, text: str = DISCLAIMER) -> None:
        """The closing statement, on the last page."""
        if self.get_y() + 24 > self.page_break_trigger:
            self.add_page()
        self.ln(4)
        top = self.get_y()
        self.set_font("Helvetica", size=8)
        height = self._lines(clean(text), self.content_width - 10) * 4.2 + 9
        self.set_draw_color(*RULE)
        self.set_fill_color(248, 250, 252)
        self.rect(self.l_margin, top, self.content_width, height, style="DF")
        self.set_xy(self.l_margin + 5, top + 2.5)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 3.5, "IMPORTANT")
        self.set_xy(self.l_margin + 5, top + 6.5)
        self.set_font("Helvetica", size=8)
        self.multi_cell(self.content_width - 10, 4.2, clean(text))
        self.set_xy(self.l_margin, top + height + 2)

    def _lines(self, text: str, width: float) -> int:
        """How many lines `text` takes at `width` in the current font."""
        return max(1, len(self.multi_cell(width, 4, text, dry_run=True, output="LINES")))
