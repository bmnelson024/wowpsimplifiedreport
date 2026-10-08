"""
Builds the simplified White Oak client review PDF from an Orion export.

Server-side version (merged 10/2026). Combines:
  * the web-service adaptations: per-request `workdir` for every intermediate
    file (so concurrent requests never clobber each other), the logo read
    from a shared read-only STATIC_DIR, automatic performance-chart
    page/crop detection (extract_data.locate_and_crop_performance_chart),
    and the Orion "Performance History" benchmark-returns row; with
  * the self-directed / managed-accounts split, the paginated holdings
    section, the optional RMD section, the larger-logo header and the
    "Top 10 Holdings by Percentage" section.
`build()` returns {"data": ..., "warnings": [...]}.
"""
import os
import sys
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from copy import deepcopy

from extract_data import extract, build_combined_data, locate_and_crop_performance_chart
from make_allocation_chart import (
    build_donut, CATEGORY_COLORS, CATEGORY_ORDER,
    build_breakdown_donut, classification_colors,
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

NAVY = colors.HexColor("#1B3557")
INK = colors.HexColor("#26251F")
SECONDARY_INK = colors.HexColor("#52514E")
MUTED = colors.HexColor("#898781")
GOOD_GREEN = colors.HexColor("#0F7A3D")
RULE = colors.HexColor("#E1E0D9")
PAGE_W, PAGE_H = letter


def money(v, cents=False):
    if cents:
        return f"${v:,.2f}"
    return f"${v:,.0f}"


def money_signed(v):
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.0f}"


def crop_performance_chart(source_pdf, page_index, crop_box, out_png, workdir, bottom_trim=0.0):
    reader = PdfReader(source_pdf)
    page = reader.pages[page_index]
    writer = PdfWriter()
    cropped = deepcopy(page)
    box = RectangleObject(crop_box)
    cropped.cropbox = box
    cropped.mediabox = box
    writer.add_page(cropped)
    tmp_pdf = os.path.join(workdir, "_perf_crop_tmp.pdf")
    with open(tmp_pdf, "wb") as f:
        writer.write(f)

    os.system(f'pdftoppm -r 300 -png "{tmp_pdf}" "{os.path.join(workdir, "_perf_raw")}"')
    raw_png = os.path.join(workdir, "_perf_raw-1.png")
    if not os.path.exists(raw_png):
        # Some poppler versions omit the "-1" suffix for a single-page PDF.
        alt = os.path.join(workdir, "_perf_raw.png")
        raw_png = alt if os.path.exists(alt) else raw_png

    from PIL import Image
    im = Image.open(raw_png)
    w, h = im.size
    # Trim a sliver of the next section's header that can creep in at the
    # bottom of an imprecise crop box (calibrated per export layout --
    # bottom_trim=0 when crop_box is already tight around the chart).
    trimmed = im.crop((0, 0, w, h - int(h * bottom_trim))) if bottom_trim else im
    trimmed.save(out_png)


def section_bar(c, x, y, w, h, title):
    c.setFillColor(NAVY)
    c.rect(x, y, w, h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x + 10, y + h / 2 - 4, title)


def stat_tile(c, x, y, w, label, value, value_color=INK, sub=None):
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawString(x, y, label.upper())
    c.setFillColor(value_color)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y - 20, value)
    if sub:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        for i, ln in enumerate(wrap_text(sub, "Helvetica", 7.5, w - 6)[:2]):
            c.drawString(x, y - 33 - i * 9, ln)


def draw_header(c, client_name, period_start, period_end, advisor):
    logo_path = os.path.join(STATIC_DIR, "white_oak_logo.png")
    # Logo is ~2.24:1; sized by height (0.8in -> ~1.8in wide). It was 0.55in
    # tall originally and read small next to a long client name.
    logo_w = 0.0
    if os.path.exists(logo_path):
        logo_h = 0.8 * inch
        logo_w = logo_h * 450 / 201
        c.drawImage(logo_path, 0.75 * inch, PAGE_H - 1.16 * inch,
                    width=logo_w, height=logo_h,
                    preserveAspectRatio=True, mask="auto")

    # Client name: 18pt, auto-shrunk (floor 13pt) so a long household name
    # never runs into the logo.
    avail = PAGE_W - 1.5 * inch - logo_w - 0.3 * inch
    name_size = 18
    while name_size > 13 and stringWidth(client_name, "Helvetica-Bold", name_size) > avail:
        name_size -= 0.5
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", name_size)
    c.drawRightString(PAGE_W - 0.75 * inch, PAGE_H - 0.75 * inch, client_name)
    c.setFillColor(SECONDARY_INK)
    c.setFont("Helvetica", 10.5)
    c.drawRightString(PAGE_W - 0.75 * inch, PAGE_H - 0.95 * inch,
                       f"Portfolio Review · {period_start} – {period_end}")
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawRightString(PAGE_W - 0.75 * inch, PAGE_H - 1.11 * inch,
                       f"Prepared by {advisor}")

    c.setStrokeColor(NAVY)
    c.setLineWidth(1.2)
    c.line(0.75 * inch, PAGE_H - 1.22 * inch, PAGE_W - 0.75 * inch, PAGE_H - 1.22 * inch)


def group_accounts(accounts):
    """Rolls Orion's internal per-sleeve account rows up to one row per real
    household registration -- a client doesn't need to see that their joint
    account is internally split into a core/dividend/legacy sleeve."""
    groups, order = {}, []
    for a in accounts:
        # Self-directed accounts never merge with a managed account, even
        # when Orion gives both the same display registration (McSwain).
        key = (a["registration"], bool(a.get("self_directed")))
        if key not in groups:
            groups[key] = {"registration": a["registration"], "type": a["type"], "value": 0.0,
                           "count": 0, "self_directed": bool(a.get("self_directed"))}
            order.append(key)
        groups[key]["value"] += a["value"]
        groups[key]["count"] += 1
    return [groups[k] for k in order]


def wrap_text(text, font, size, max_width):
    words = text.split()
    lines, line = [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if stringWidth(test, font, size) > max_width and line:
            lines.append(line)
            line = w
        else:
            line = test
    if line:
        lines.append(line)
    return lines


def draw_accounts_section(c, top_y, data):
    """Draws the Accounts table with its section bar's top edge at `top_y`
    and returns the y position just below the total row."""
    section_bar(c, 0.75 * inch, top_y - 18, PAGE_W - 1.5 * inch, 18, "ACCOUNTS")

    grouped = group_accounts(data["accounts"])
    account_col_w = 4.3 * inch - 0.75 * inch - 0.2 * inch
    col_x = [0.75 * inch, 4.3 * inch]
    row_y = top_y - 18 - 22
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(col_x[0], row_y, "REGISTRATION")
    c.drawString(col_x[1], row_y, "TYPE")
    c.drawRightString(PAGE_W - 0.75 * inch, row_y, "VALUE")
    row_y -= 8
    c.setStrokeColor(RULE)
    c.line(0.75 * inch, row_y, PAGE_W - 0.75 * inch, row_y)
    row_y -= 18

    # The gap between account rows is fixed at 18pt for a small household
    # (2-4 accounts, the only sizes verified against real clients so far),
    # but tightens for larger households so the table doesn't push the
    # Asset Allocation section low enough to collide with the page footer.
    # Verified against a 6-account client (Livingston) without visible
    # crowding; re-check visually if a client ever has more than ~8.
    n_accounts = len(grouped)
    row_gap = 18 if n_accounts <= 4 else max(9, 18 - 3 * (n_accounts - 4))

    for g in grouped:
        name_lines = wrap_text(g["registration"], "Helvetica", 9.5, account_col_w)
        c.setFillColor(INK)
        c.setFont("Helvetica", 9.5)
        for i, ln in enumerate(name_lines):
            c.drawString(col_x[0], row_y - i * 11, ln)
        if g.get("self_directed") or g["count"] > 1:
            c.setFillColor(MUTED)
            c.setFont("Helvetica-Oblique" if g.get("self_directed") else "Helvetica", 8)
            sub = ("Self-directed · invested at the client's direction" if g.get("self_directed")
                   else f"{g['count']} accounts combined")
            c.drawString(col_x[0], row_y - len(name_lines) * 11, sub)
            extra = 11
        else:
            extra = 0
        c.setFillColor(SECONDARY_INK)
        c.setFont("Helvetica", 9.5)
        c.drawString(col_x[1], row_y, g["type"])
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawRightString(PAGE_W - 0.75 * inch, row_y, money(g["value"]))
        row_y -= (len(name_lines) * 11 + extra + row_gap)

    c.setStrokeColor(NAVY)
    c.setLineWidth(1)
    c.line(0.75 * inch, row_y + 10, PAGE_W - 0.75 * inch, row_y + 10)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(col_x[0], row_y - 4, "Total")
    c.drawRightString(PAGE_W - 0.75 * inch, row_y - 4, money(data["total_value"]))
    return row_y - 4


def draw_benchmark_returns(c, top_y, bench, client_name):
    """Row of review-period returns from Orion's own "Performance History"
    table, one column per series (portfolio first, then each benchmark).
    Returns the y just below the block."""
    series = bench["series"]
    n = len(series)
    col_w = (PAGE_W - 1.5 * inch) / n
    rule_y = top_y + 10
    c.setStrokeColor(RULE)
    c.line(0.75 * inch, rule_y, PAGE_W - 0.75 * inch, rule_y)

    for i, s in enumerate(series):
        x = 0.75 * inch + i * col_w
        is_portfolio = (i == 0)
        label = "Portfolio" if is_portfolio else s["label"]
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        lines = wrap_text(label.upper(), "Helvetica", 8, col_w - 10)[:2]
        for j, ln in enumerate(lines):
            c.drawString(x, top_y - j * 9, ln)
        v = s["pct"]
        c.setFillColor(NAVY if is_portfolio else INK)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(x, top_y - 36, f"{v:+.2f}%")
    base = top_y - 36
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 7.5)
    c.drawString(0.75 * inch, base - 14,
                 f"Returns for {bench['period_start']} – {bench['period_end']}, as reported by Orion.")
    return base - 14


def draw_gain_loss_section(c, top_y, data):
    """Draws the Realized / Unrealized Gain-Loss summary with its section
    bar's top edge at `top_y`. Two side-by-side figures, colored green/red
    by sign like the page-1 stat tiles, plus a one-line note on what each
    term means. Returns the y position below the note."""
    gl = data.get("gain_loss") or {}
    section_bar(c, 0.75 * inch, top_y - 18, PAGE_W - 1.5 * inch, 18, "GAIN / LOSS SUMMARY")

    tile_top = top_y - 18 - 34
    tile_w = (PAGE_W - 1.5 * inch) / 2

    def _tile(x, label, value):
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawString(x, tile_top, label.upper())
        c.setFont("Helvetica-Bold", 16)
        if value is None:
            c.setFillColor(INK)
            c.drawString(x, tile_top - 20, "–")
            return
        c.setFillColor(GOOD_GREEN if value >= 0 else colors.HexColor("#B3261E"))
        c.drawString(x, tile_top - 20, money_signed(value))

    _tile(0.75 * inch, "Realized Gain/Loss", gl.get("realized_gain"))
    _tile(0.75 * inch + tile_w, "Unrealized Gain/Loss", gl.get("unrealized_gain"))

    note_y = tile_top - 42
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 8.5)
    note = ("Realized reflects gains and losses on positions sold this period; unrealized reflects gains "
            "and losses on positions still held, based on original cost.")
    # Wrapped: as one line this ran past the right margin.
    note_lines = wrap_text(note, "Helvetica-Oblique", 8.5, PAGE_W - 1.5 * inch)
    for i, ln in enumerate(note_lines):
        c.drawString(0.75 * inch, note_y - i * 11, ln)
    return note_y - (len(note_lines) - 1) * 11 - 10


def draw_rmd_section(c, top_y, rmd):
    """Required Minimum Distributions section, drawn only when RMD info is
    supplied (it is NOT in the Orion export -- entered per report).
    `rmd` = list of {"account": str, "required": float, "taken": float,
    optional "year": int, "deadline": "12/31/2026", "as_of": "mm/dd/yyyy",
    "note": str}. One row per account: required / taken to date / remaining,
    with a thin progress bar. Returns the y below the section."""
    year = next((r.get("year") for r in rmd if r.get("year")), None)
    title = "REQUIRED MINIMUM DISTRIBUTIONS" + (f" ({year})" if year else "")
    section_bar(c, 0.75 * inch, top_y - 18, PAGE_W - 1.5 * inch, 18, title)

    right = PAGE_W - 0.75 * inch
    col_req, col_taken, col_rem = right - 2.6 * inch, right - 1.35 * inch, right
    y = top_y - 18 - 22
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(0.75 * inch, y, "ACCOUNT")
    c.drawRightString(col_req, y, "REQUIRED")
    c.drawRightString(col_taken, y, "TAKEN TO DATE")
    c.drawRightString(col_rem, y, "REMAINING")
    y -= 8
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(0.75 * inch, y, right, y)
    y -= 18

    for r in rmd:
        required, taken = float(r["required"]), float(r.get("taken", 0) or 0)
        remaining = max(0.0, required - taken)
        c.setFillColor(INK)
        c.setFont("Helvetica", 9.5)
        c.drawString(0.75 * inch, y, r["account"])
        c.setFillColor(SECONDARY_INK)
        c.drawRightString(col_req, y, money(required))
        c.drawRightString(col_taken, y, money(taken))
        c.setFont("Helvetica-Bold", 9.5)
        if remaining <= 0:
            c.setFillColor(GOOD_GREEN)
            c.drawRightString(col_rem, y, "Satisfied")
        else:
            c.setFillColor(INK)
            c.drawRightString(col_rem, y, money(remaining))
        # progress bar
        bar_y, bar_w = y - 9, right - 0.75 * inch
        c.setFillColor(RULE)
        c.rect(0.75 * inch, bar_y, bar_w, 3.5, stroke=0, fill=1)
        frac = 1.0 if required <= 0 else min(1.0, taken / required)
        c.setFillColor(GOOD_GREEN if remaining <= 0 else NAVY)
        c.rect(0.75 * inch, bar_y, bar_w * frac, 3.5, stroke=0, fill=1)
        y -= 30

    bits = []
    dl = next((r.get("deadline") for r in rmd if r.get("deadline")), None)
    if dl:
        bits.append(f"Deadline: {dl}.")
    as_of = next((r.get("as_of") for r in rmd if r.get("as_of")), None)
    bits.append("Required amounts are provided by the account custodian"
                + (f" as of {as_of}" if as_of else "") + "; confirm with your tax advisor.")
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(0.75 * inch, y + 4, " ".join(bits))
    return y - 6


def draw_footer(c, page_num, total_pages, generated_date):
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(0.75 * inch, 0.55 * inch, f"White Oak Wealth Partners · Prepared {generated_date}")
    c.drawRightString(PAGE_W - 0.75 * inch, 0.55 * inch, f"Page {page_num} of {total_pages}")


class _NullCanvas:
    """Swallows every canvas call. Used to "dry run" draw_holdings_section
    -- run the exact same layout logic (including page-break decisions)
    with no actual drawing, purely to count how many pages the Holdings
    section will need. That count has to be known *before* page 1 is drawn,
    since page 1's own footer ("Page 1 of N") needs the final N."""
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def draw_holdings_section(c, data, generated_date, start_page_num, total_pages, density=1.0):
    """Draws the Holdings by Asset Class section (and, following it, any
    Self-Directed holdings sections) starting on a fresh page, including
    that page's header. Paginates onto additional pages -- redrawing the
    header and a "(continued)" section bar each time -- whenever the next
    block wouldn't fit above the footer.

    A single fixed page here was only ever verified against reports with
    one holdings block (4 categories, capped at 10 rows each). A
    managed/self-directed split adds a full extra holdings block per
    self-directed account on top of that, so the content no longer
    reliably fits on one page -- see the "self-directed accounts" skill
    note. Pass a `_NullCanvas` as `c` to dry-run this (no drawing, just
    page counting) before the real render.

    `density` (1.0 = normal) scales the vertical spacing between rows and
    blocks. build() tries progressively tighter values so that a list that
    overflows by only a few lines stays on one page instead of leaving a
    nearly empty page behind it; see fit_holdings_density().

    Returns the page number of the last page this section used."""
    BOTTOM = 1.0 * inch
    TOP = PAGE_H - 1.65 * inch
    col_value_right = 6.4 * inch
    col_pct_right = PAGE_W - 0.75 * inch

    RH = 15.5 * density          # one holding row
    FONT = 9.5 if density >= 0.9 else 9.0
    page_num = start_page_num
    y = TOP

    def new_page():
        nonlocal page_num, y
        draw_footer(c, page_num, total_pages, generated_date)
        c.showPage()
        page_num += 1
        draw_header(c, data["client_name"], data["period_start"], data["period_end"], data["advisor"])
        y = TOP
        section_bar(c, 0.75 * inch, y - 18, PAGE_W - 1.5 * inch, 18, "TOP 10 HOLDINGS BY PERCENTAGE (CONTINUED)")
        y -= 18 + 20
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8)
        c.drawRightString(col_value_right, y, "MARKET VALUE")
        c.drawRightString(col_pct_right, y, "% OF PORTFOLIO")
        y -= 12

    def ensure_room(needed):
        if y - needed < BOTTOM:
            new_page()

    has_managed = "managed_holdings_by_category" in data
    draw_header(c, data["client_name"], data["period_start"], data["period_end"], data["advisor"])
    section_bar(c, 0.75 * inch, y - 18, PAGE_W - 1.5 * inch, 18, "TOP 10 HOLDINGS BY PERCENTAGE")
    y -= 18 + 20
    # What the list actually is: the 10 largest positions within each asset
    # class, ranked by % of portfolio (renamed from "Holdings by Asset Class"
    # at Brian's request, since the ranking is the real point).
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(0.75 * inch, y, "The 10 largest positions in each asset class, ranked by percent of portfolio.")
    y -= 12
    if has_managed:
        # Managed-accounts export excludes self-directed holdings (shown in
        # their own section below) -- flag that explicitly rather than
        # leaving the reader to guess why this list is smaller than the
        # Accounts table on page 1.
        c.drawString(0.75 * inch, y, "Reflects accounts managed by White Oak; self-directed holdings are shown separately below.")
        y -= 12
    y -= 2
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(col_value_right, y, "MARKET VALUE")
    c.drawRightString(col_pct_right, y, "% OF PORTFOLIO")
    y -= 12

    holdings_by_cat = data.get("managed_holdings_by_category") if has_managed else data.get("holdings_by_category", {})
    for cat in CATEGORY_ORDER:
        info = holdings_by_cat.get(cat)
        if not info or not info.get("top"):
            continue

        # Keep a category's header glued to at least its first holding row
        # rather than letting it get stranded alone at the bottom of a page.
        block_h = 32 * density + RH * len(info["top"])
        ensure_room(block_h)

        y -= 10 * density
        dot_color = CATEGORY_COLORS.get(cat, "#CFCCC3")
        c.setFillColor(colors.HexColor(dot_color))
        c.circle(0.75 * inch + 4, y - 3.5, 4, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11.5)
        c.drawString(0.75 * inch + 15, y - 8, cat)

        shown, count = len(info["top"]), info["count"]
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.5)
        if count > shown:
            coverage = 100 * sum(h["market_value"] for h in info["top"]) / info["total_value"] \
                if info["total_value"] else 0
            note = f"top {shown} of {count} positions · {coverage:.0f}% of this asset class"
        else:
            note = f"{count} position{'s' if count != 1 else ''} · all shown"
        c.drawRightString(col_pct_right, y - 8, note)
        y -= 22 * density

        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(0.75 * inch, y, PAGE_W - 0.75 * inch, y)
        y -= 15 * density

        for h in info["top"]:
            ensure_room(RH)
            name_lines = wrap_text(h["security"], "Helvetica", FONT, col_value_right - 0.75 * inch - 0.3 * inch)
            c.setFillColor(INK)
            c.setFont("Helvetica", FONT)
            c.drawString(0.75 * inch, y, name_lines[0])
            c.setFillColor(SECONDARY_INK)
            c.drawRightString(col_value_right, y, money(h["market_value"]))
            c.drawRightString(col_pct_right, y, f"{h['allocation_pct']:.2f} %")
            y -= RH

    # Self-directed holdings -- one small labeled list per self-directed
    # account, kept separate from the managed-account buckets above rather
    # than folded into them (a stock-picker's own choices shouldn't read as
    # part of White Oak's asset allocation). See "self-directed accounts"
    # skill note.
    for sd in data.get("self_directed", []):
        block_h = 14 + 22 + 15 + RH * len(sd.get("top_holdings", [])) + 14
        ensure_room(block_h)

        y -= 14
        c.setFillColor(MUTED)
        c.circle(0.75 * inch + 4, y - 3.5, 4, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11.5)
        c.drawString(0.75 * inch + 15, y - 8, f"Self-Directed — {sd['name']}")

        ret = sd.get("total_return_pct")
        ret_str = f"{'+' if ret >= 0 else ''}{ret:.2f}%" if ret is not None else "–"
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawRightString(col_pct_right, y - 8, f"{money(sd['total_value'])} · {ret_str} for the period")
        y -= 22 * density

        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(0.75 * inch, y, PAGE_W - 0.75 * inch, y)
        y -= 15 * density

        for h in sd.get("top_holdings", []):
            ensure_room(RH)
            name_lines = wrap_text(h["security"], "Helvetica", FONT, col_value_right - 0.75 * inch - 0.3 * inch)
            c.setFillColor(INK)
            c.setFont("Helvetica", FONT)
            c.drawString(0.75 * inch, y, name_lines[0])
            c.setFillColor(SECONDARY_INK)
            c.drawRightString(col_value_right, y, money(h["market_value"]))
            c.drawRightString(col_pct_right, y, f"{h['allocation_pct']:.2f} %")
            y -= RH

        c.setFillColor(MUTED)
        c.setFont("Helvetica-Oblique", 7.8)
        c.drawString(0.75 * inch, y - 2, "Invested at the client's own direction; excluded from the managed figures above.")
        y -= 14

    draw_footer(c, page_num, total_pages, generated_date)
    c.showPage()
    return page_num


def fit_holdings_density(data, generated_date, start_page_num=2):
    """Picks the loosest spacing at which the Holdings section fits on a
    single page. A list that runs only a line or two over a page used to
    spill onto its own nearly empty page (and push the performance page to
    page 4); tightening row spacing a little keeps it on one page. If even
    the tightest setting doesn't fit (a genuinely long list, e.g. with
    self-directed blocks), normal spacing is used and it paginates as
    before. Returns (density, last_page_number)."""
    natural = draw_holdings_section(_NullCanvas(), data, generated_date,
                                    start_page_num=start_page_num, total_pages=0)
    if natural == start_page_num:
        return 1.0, natural
    for d in (0.95, 0.90, 0.85, 0.80):
        end = draw_holdings_section(_NullCanvas(), data, generated_date,
                                    start_page_num=start_page_num, total_pages=0, density=d)
        if end == start_page_num:
            return d, end
    return 1.0, natural


def build(input_pdf, output_pdf, workdir, registration_overrides=None, perf_page_index=None,
          perf_crop_box=None, perf_bottom_trim=None,
          managed_pdf=None, selfdirected_pdfs=None,
          managed_perf_page_index=None, managed_perf_crop_box=None, managed_perf_bottom_trim=0,
          rmd=None):
    """workdir: a directory for this request's intermediate files (charts,
    cropped PDF). Must be unique per concurrent request.

    perf_page_index / perf_crop_box / perf_bottom_trim (and the managed_perf_*
    equivalents for `managed_pdf`): leave as None (default) to auto-detect the
    performance chart page and crop in pixel space; pass explicit page index +
    crop box to override for one client (original point-space method).

    Returns {"data": <extracted data dict>, "warnings": [str, ...]}.

    rmd: optional list of per-account Required Minimum Distribution dicts
    ({"account", "required", "taken", optional "year"/"deadline"/"as_of"}),
    entered manually because Orion's export has no RMD data. Omit/None for
    clients without RMDs -- no section is drawn.

    registration_overrides: optional {account_number: display_registration}
    for one-off, client-specific label corrections Orion doesn't know about
    (e.g. a DAF that Orion tags as a plain joint account). Applied after
    extraction, before the accounts table is grouped/drawn; does not change
    the underlying Orion data or affect any other client's report.

    perf_page_index / perf_crop_box / perf_bottom_trim: override these if a
    given export's page count/layout puts the Performance chart somewhere
    other than the calibrated default (see skill notes on chart-crop
    calibration risk).

    managed_pdf / selfdirected_pdfs: for a household where some money is
    invested at the client's own direction rather than by White Oak (see
    the "self-directed accounts" skill note). `input_pdf` stays the full
    household export -- it still backs Portfolio Value, Change This Period,
    Income Received, the Accounts table, and both Asset Allocation donuts,
    unchanged. `managed_pdf` is a second Orion export scoped to just the
    accounts White Oak manages (Orion can produce a custom multi-account
    group export the same way it produces a single-account one) -- when
    given, it replaces the household blend for Portfolio Return, the page-3
    Performance vs. Benchmarks chart, the Gain/Loss Summary, and page 2's
    Holdings by Asset Class. `selfdirected_pdfs` is an optional list of
    single-account exports (one or more) for the self-directed account(s)
    -- when given, each gets its own small holdings list on page 2 and its
    own return% noted on page 3, clearly separated from the managed figures
    rather than blended into them. Which accounts are "managed" vs.
    "self-directed" varies by household -- it's determined entirely by
    which accounts Brian puts in which export, not detected automatically.

    managed_perf_page_index / managed_perf_crop_box / managed_perf_bottom_trim:
    the performance-chart crop calibration for `managed_pdf` -- this is a
    separate PDF from `input_pdf` and needs its own calibration the same
    way every new client export does (see the chart-crop-calibration skill
    note); required whenever `managed_pdf` is given."""
    if selfdirected_pdfs and not managed_pdf:
        # Without the "all but self-directed" export, the household return
        # still includes the self-directed account, so the report's "excluded
        # from the figures above" wording would be false.
        raise ValueError("selfdirected_pdfs requires managed_pdf (the all-but-self-directed export).")
    data = build_combined_data(input_pdf, managed_pdf=managed_pdf, selfdirected_pdfs=selfdirected_pdfs)
    sd_numbers = {n for sd in data.get("self_directed", []) for n in sd.get("account_numbers", [])}
    for a in data["accounts"]:
        a["self_directed"] = a["account_number"] in sd_numbers
    if registration_overrides:
        for a in data["accounts"]:
            if a["account_number"] in registration_overrides:
                a["registration"] = registration_overrides[a["account_number"]]
    generated_date = datetime.now().strftime("%m/%d/%Y")

    warnings = []
    donut_path = os.path.join(workdir, "allocation_donut.png")
    build_donut(data["allocation_buckets"], donut_path, data["total_value"])

    classification_path = os.path.join(workdir, "classification_donut.png")
    build_breakdown_donut(data.get("allocation_breakdown", []), classification_path)

    # Chart source: the managed-accounts export when given (its chart is the
    # one that matches the managed Portfolio Return), else the household.
    chart_pdf = managed_pdf if managed_pdf else input_pdf
    if managed_pdf:
        o_idx, o_box, o_trim = managed_perf_page_index, managed_perf_crop_box, managed_perf_bottom_trim
    else:
        o_idx, o_box, o_trim = perf_page_index, perf_crop_box, perf_bottom_trim
    if o_idx is None or o_box is None:
        perf_png, detect_warnings = locate_and_crop_performance_chart(chart_pdf, workdir)
        warnings.extend(detect_warnings)
        if perf_png is None:
            # Total detection failure -- blank placeholder so the rest of the
            # report still builds rather than erroring out entirely.
            from PIL import Image
            perf_png = os.path.join(workdir, "performance_chart.png")
            Image.new("RGB", (1650, 500), "white").save(perf_png)
            warnings.append("Performance chart could not be generated for this report.")
    else:
        perf_png = os.path.join(workdir, "performance_chart.png")
        crop_performance_chart(chart_pdf, page_index=o_idx, crop_box=list(o_box), out_png=perf_png,
                                workdir=workdir, bottom_trim=(o_trim or 0.0))

    # The Holdings section (page 2) can now spill onto extra pages (a
    # managed/self-directed split adds a full extra holdings block per
    # self-directed account -- see draw_holdings_section). Page 1's own
    # footer needs the final page count, so silently dry-run the Holdings
    # section first to find it before drawing anything for real.
    holdings_density, holdings_dry_end = fit_holdings_density(data, generated_date, start_page_num=2)
    total_pages = holdings_dry_end + 1  # +1 for the closing Performance/Gain-Loss page

    c = canvas.Canvas(output_pdf, pagesize=letter)

    # ---------------------------------------------------------- PAGE 1 ----
    draw_header(c, data["client_name"], data["period_start"], data["period_end"], data["advisor"])

    act = data["activity"]
    top = PAGE_H - 1.65 * inch
    tile_w = (PAGE_W - 1.5 * inch) / 4
    # When a managed-accounts export is given, Portfolio Return reflects
    # just the accounts White Oak manages, not the household blend --
    # otherwise a self-directed account's own results would skew the
    # figure clients read as "how did our management do." See the
    # "self-directed accounts" skill note.
    has_managed = "managed_total_return_pct" in data
    return_pct = data.get("managed_total_return_pct") if has_managed else data.get("total_return_pct")
    return_str = f"{'+' if return_pct >= 0 else ''}{return_pct:.2f}%" if return_pct is not None else "–"
    return_color = (GOOD_GREEN if return_pct >= 0 else colors.HexColor("#B3261E")) if return_pct is not None else INK
    tiles = [
        ("Portfolio Value", money(act.get("Ending Market Value w/ Bond Accrual", data["total_value"])),
         NAVY,
         f"{'up' if act.get('Market Value Change', 0) >= 0 else 'down'} from "
         f"{money(act.get('Beginning Market Value', 0))} on {data['period_start']}"),
        ("Portfolio Return", return_str, return_color,
         "for managed accounts, this period" if has_managed else "for the period"),
        ("Change This Period", money_signed(act.get("Market Value Change", 0)),
         GOOD_GREEN if act.get("Market Value Change", 0) >= 0 else colors.HexColor("#B3261E"), None),
        ("Income Received", money(act.get("Income", 0)), INK, None),
    ]
    for i, (label, value, vcolor, sub) in enumerate(tiles):
        stat_tile(c, 0.75 * inch + i * tile_w, top, tile_w, label, value, vcolor, sub)

    c.setStrokeColor(RULE)
    c.setLineWidth(0.75)
    rule_y = top - 63
    c.line(0.75 * inch, rule_y, PAGE_W - 0.75 * inch, rule_y)

    # Cash-flow mini-row: Net Cash Flow, Contributions, Distributions --
    # added per Brian's request to surface contributions/distributions as
    # their own labeled figures under the main stat tiles, rather than
    # buried in a single parenthetical sentence.
    contributions = act.get("Contributions", 0)
    distributions = act.get("Distributions", 0)
    net_flow = contributions + distributions
    flow_tiles = [
        ("Net Cash Flow", money_signed(net_flow),
         GOOD_GREEN if net_flow >= 0 else colors.HexColor("#B3261E")),
        ("Contributions", money(contributions), INK),
        ("Distributions", money(abs(distributions)), INK),
    ]
    for i, (label, value, vcolor) in enumerate(flow_tiles):
        x = 0.75 * inch + i * tile_w
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        c.drawString(x, rule_y - 14, label.upper())
        c.setFillColor(vcolor)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, rule_y - 27, value)

    # Accounts section (moved above Asset Allocation per Brian's request)
    accounts_bottom_y = draw_accounts_section(c, rule_y - 55, data)

    # Allocation section -- two columns: the 4-bucket donut (left) and a
    # further breakdown by Orion's granular classification labels (right,
    # added per Brian's request), sized to sit side by side under one
    # shared section bar.
    section_y = accounts_bottom_y - 32
    section_h = 20
    section_bar(c, 0.75 * inch, section_y - section_h, PAGE_W - 1.5 * inch, section_h, "ASSET ALLOCATION")

    col_gap = 24
    col_w = (PAGE_W - 1.5 * inch - col_gap) / 2
    col1_x = 0.75 * inch
    col2_x = col1_x + col_w + col_gap

    subhead_y = section_y - section_h - 16
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(col1_x, subhead_y, "BY ASSET CLASS")
    c.drawString(col2_x, subhead_y, "BY CLASSIFICATION")

    donut_top = subhead_y - 14
    donut_size = 1.55 * inch

    def _donut_column(x, image_path, legend_rows, row_h, label_font_size, value_font_size, two_line):
        c.drawImage(image_path, x, donut_top - donut_size, width=donut_size, height=donut_size, mask="auto")
        legend_x = x + donut_size + 16
        legend_max_w = col_w - donut_size - 16
        legend_block_h = len(legend_rows) * row_h
        legend_y = donut_top - max(0, (donut_size - legend_block_h) / 2) - 12
        for label, swatch, line2 in legend_rows:
            c.setFillColor(colors.HexColor(swatch))
            c.rect(legend_x, legend_y - 9, 9, 9, stroke=0, fill=1)
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", label_font_size)
            c.drawString(legend_x + 14, legend_y - 8, label)
            if two_line:
                c.setFillColor(SECONDARY_INK)
                c.setFont("Helvetica", value_font_size)
                c.drawString(legend_x + 14, legend_y - 8 - (label_font_size + 2), line2)
            else:
                c.setFillColor(SECONDARY_INK)
                c.setFont("Helvetica", value_font_size)
                c.drawRightString(x + col_w, legend_y - 8, line2)
            legend_y -= row_h
        return min(donut_top - donut_size, legend_y)

    # Left: 4-bucket donut
    bucket_rows = []
    for cat in CATEGORY_ORDER:
        val = data["allocation_buckets"].get(cat, 0)
        if val <= 0:
            continue
        pct = 100 * val / data["total_value"]
        bucket_rows.append((cat, CATEGORY_COLORS[cat], f"{money(val)} · {pct:.1f}%"))
    col1_bottom = _donut_column(col1_x, donut_path, bucket_rows, row_h=38,
                                 label_font_size=9.5, value_font_size=8, two_line=True)

    # Right: further breakdown by granular classification (Large Cap, Mid
    # Cap, High Yield Bond, etc.), colored as shades of the bucket they
    # belong to via classification_colors().
    breakdown = data.get("allocation_breakdown", [])
    class_colors = classification_colors(breakdown)
    class_rows = [
        (r["label"], class_colors[i], f"{r['pct']:.1f}%")
        for i, r in enumerate(breakdown)
    ]
    col2_bottom = _donut_column(col2_x, classification_path, class_rows, row_h=15.5,
                                 label_font_size=8, value_font_size=8, two_line=False)

    note_y = min(col1_bottom, col2_bottom) - 16
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 8)
    note1_lines = wrap_text(
        "Equities, fixed income, and cash balances are rolled up from individual holdings across all accounts.",
        "Helvetica-Oblique", 8, col_w)
    note2_lines = wrap_text(
        "Shows the largest classifications; smaller ones are grouped as Other.",
        "Helvetica-Oblique", 8, col_w)
    for i, ln in enumerate(note1_lines):
        c.drawString(col1_x, note_y - i * 10, ln)
    for i, ln in enumerate(note2_lines):
        c.drawString(col2_x, note_y - i * 10, ln)

    draw_footer(c, 1, total_pages, generated_date)
    c.showPage()

    # ---------------------------------------------------------- PAGE 2+ ---
    holdings_end_page = draw_holdings_section(c, data, generated_date,
                                               start_page_num=2, total_pages=total_pages,
                                               density=holdings_density)

    # -------------------------------------------- FINAL (PERFORMANCE) PAGE
    final_page_num = holdings_end_page + 1
    draw_header(c, data["client_name"], data["period_start"], data["period_end"], data["advisor"])

    y = PAGE_H - 1.65 * inch
    section_bar(c, 0.75 * inch, y - 18, PAGE_W - 1.5 * inch, 18, "PERFORMANCE VS. BENCHMARKS")

    from PIL import Image as PILImage
    im = PILImage.open(perf_png)
    iw, ih = im.size
    draw_w = PAGE_W - 1.5 * inch
    draw_h = draw_w * ih / iw
    chart_top = y - 18 - 10
    c.drawImage(perf_png, 0.75 * inch, chart_top - draw_h, width=draw_w, height=draw_h, mask="auto")

    has_managed = "managed_total_return_pct" in data
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 7.8)
    legend = data.get("managed_benchmark_legend") if has_managed else data.get("benchmark_legend")
    if legend:
        benchmark_desc = f"the S&P 500, {legend['blend']}, and the {legend['bond']} index"
    else:
        benchmark_desc = "the S&P 500 and selected benchmark indices"
    scope_desc = "managed accounts" if has_managed else "the household"
    caption = (f"Cumulative return for {scope_desc}, {data['period_start']} – {data['period_end']}, "
               f"vs. {benchmark_desc}.")
    # Wrapped: the managed-accounts caption is long enough to run past the
    # right margin as a single line.
    cap_lines = wrap_text(caption, "Helvetica-Oblique", 7.8, PAGE_W - 1.5 * inch)
    for i, ln in enumerate(cap_lines):
        c.drawString(0.75 * inch, chart_top - draw_h - 14 - i * 10, ln)
    caption_y = chart_top - draw_h - 14 - (len(cap_lines) - 1) * 10

    # Self-directed callout(s) -- each account's own return, clearly noted
    # as excluded from the chart and figures above rather than blended in.
    # See "self-directed accounts" skill note.
    note_y = caption_y
    for sd in data.get("self_directed", []):
        note_y -= 12
        ret = sd.get("total_return_pct")
        ret_str = f"{'+' if ret >= 0 else ''}{ret:.2f}%" if ret is not None else "–"
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Oblique", 7.8)
        c.drawString(0.75 * inch, note_y,
                     f"Self-directed — {sd['name']}: {ret_str} for the period (excluded from the figures above).")

    # Orion's own "Performance History" returns row (managed group's table
    # when a managed export is given, so it matches the Portfolio Return).
    bench = data.get("managed_benchmark_returns") if has_managed else data.get("benchmark_returns")
    next_top = note_y - 36
    if bench and bench.get("series"):
        next_top = draw_benchmark_returns(c, note_y - 26, bench, data["client_name"]) - 22
    else:
        why = (data.get("managed_benchmark_returns_issue") if has_managed
               else data.get("benchmark_returns_issue")) or "unknown reason"
        warnings.append("Benchmark returns table left out of the performance page (" + why + ").")

    # Gain / Loss summary
    gain_loss_data = dict(data)
    if has_managed:
        gain_loss_data["gain_loss"] = data.get("managed_gain_loss")
    gl_bottom = draw_gain_loss_section(c, next_top, gain_loss_data)

    # RMD section (optional, manually supplied -- not in the Orion export)
    if rmd:
        draw_rmd_section(c, gl_bottom - 30, rmd)

    # Disclosure
    disc_y = 1.05 * inch
    c.setStrokeColor(RULE)
    c.line(0.75 * inch, disc_y + 14, PAGE_W - 0.75 * inch, disc_y + 14)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7)
    disclosure = ("This summary is provided for informational purposes and is not a substitute for your official "
                  "custodial statements, which you should review this report against. Performance is time-weighted "
                  "and net of fees; past performance does not guarantee future results. White Oak Wealth Partners "
                  "is an investment adviser registered with the North Carolina Securities Division.")
    words = disclosure.split()
    line, lines = "", []
    for w in words:
        test = f"{line} {w}".strip()
        if stringWidth(test, "Helvetica", 7) > (PAGE_W - 1.5 * inch):
            lines.append(line)
            line = w
        else:
            line = test
    lines.append(line)
    ly = disc_y
    for l in lines:
        c.drawString(0.75 * inch, ly, l)
        ly -= 9

    draw_footer(c, final_page_num, total_pages, generated_date)
    c.showPage()

    c.save()

    return {"data": data, "warnings": warnings}


if __name__ == "__main__":
    import tempfile
    inp = sys.argv[1]
    out = sys.argv[2]
    with tempfile.TemporaryDirectory() as wd:
        result = build(inp, out, wd)
    print("Wrote", out, "warnings:", result["warnings"])
