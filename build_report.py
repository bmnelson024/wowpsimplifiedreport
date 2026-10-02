"""
Builds the simplified White Oak client review PDF from an Orion export.

This is a server-side port of the verified `white-oak-simplified-review`
skill's build_report.py. The layout/drawing code below is reproduced
verbatim from the skill -- section bars, stat tiles, accounts table,
allocation donuts, holdings pages, performance chart, gain/loss summary,
disclosure footer, all unchanged.

Two adaptations were made to run this safely as a concurrent web service
instead of a single interactive session:

  1. Chart/temp-file paths are now parameterized by a per-request `workdir`
     (a fresh temp directory per request) instead of a single shared
     module-level ASSETS folder, so two requests generating reports at the
     same time don't clobber each other's intermediate files.
  2. The performance-chart crop is located automatically via
     `extract_data.locate_and_crop_performance_chart()` instead of requiring
     the manual page_index/crop_box/bottom_trim calibration the skill used
     to do by hand each time. That function works entirely in pixel space
     (render the candidate page once, find the chart's section-header bars,
     crop the same image) specifically to avoid a PDF-coordinate/page-
     rotation mismatch an earlier point-space version had -- see its
     docstring. The manual page_index/crop_box/bottom_trim parameters are
     kept as optional overrides for a one-off correction (using
     `crop_performance_chart()` below, the original point-space approach),
     but default to None to trigger auto-detection.

The only bundled static asset is the White Oak logo, read from STATIC_DIR
(shared, read-only, not per-request).
"""
import os
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from copy import deepcopy

from extract_data import extract, locate_and_crop_performance_chart
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
    if os.path.exists(logo_path):
        c.drawImage(logo_path, 0.75 * inch, PAGE_H - 1.05 * inch,
                    width=1.9 * inch, height=0.55 * inch,
                    preserveAspectRatio=True, mask="auto")

    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 20)
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
    household registration."""
    groups, order = {}, []
    for a in accounts:
        key = a["registration"]
        if key not in groups:
            groups[key] = {"registration": key, "type": a["type"], "value": 0.0, "count": 0}
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

    n_accounts = len(grouped)
    row_gap = 18 if n_accounts <= 4 else max(9, 18 - 3 * (n_accounts - 4))

    for g in grouped:
        name_lines = wrap_text(g["registration"], "Helvetica", 9.5, account_col_w)
        c.setFillColor(INK)
        c.setFont("Helvetica", 9.5)
        for i, ln in enumerate(name_lines):
            c.drawString(col_x[0], row_y - i * 11, ln)
        if g["count"] > 1:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 8)
            c.drawString(col_x[0], row_y - len(name_lines) * 11, f"{g['count']} accounts combined")
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


def draw_gain_loss_section(c, top_y, data):
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
    c.drawString(0.75 * inch, note_y,
                 "Realized reflects gains and losses on positions sold this period; unrealized reflects gains "
                 "and losses on positions still held, based on original cost.")
    return note_y - 10


def draw_footer(c, page_num, total_pages, generated_date):
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(0.75 * inch, 0.55 * inch, f"White Oak Wealth Partners · Prepared {generated_date}")
    c.drawRightString(PAGE_W - 0.75 * inch, 0.55 * inch, f"Page {page_num} of {total_pages}")


def build(input_pdf, output_pdf, workdir, registration_overrides=None,
          perf_page_index=None, perf_crop_box=None, perf_bottom_trim=None):
    """registration_overrides: optional {account_number: display_registration}
    for one-off, client-specific label corrections.

    perf_page_index / perf_crop_box / perf_bottom_trim: leave all as None
    (the default) to auto-detect the performance chart's page and crop,
    entirely in pixel space (see extract_data.locate_and_crop_performance_
    chart). Pass explicit perf_page_index + perf_crop_box to override
    auto-detection for a specific client -- this uses the original,
    point-space `crop_performance_chart()` below, same convention as the
    skill's manual calibration parameters.

    workdir: a directory for this request's intermediate files (charts,
    cropped PDF). Must be unique per concurrent request.

    Returns {"data": <extracted data dict>, "warnings": [str, ...]}.
    """
    data = extract(input_pdf)
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

    if perf_page_index is None or perf_crop_box is None:
        perf_png, detect_warnings = locate_and_crop_performance_chart(input_pdf, workdir)
        warnings.extend(detect_warnings)
        if perf_png is None:
            # Total detection failure (e.g. no performance page found at
            # all) -- fall back to a blank placeholder so the rest of the
            # report still builds rather than erroring out entirely.
            from PIL import Image
            perf_png = os.path.join(workdir, "performance_chart.png")
            Image.new("RGB", (1650, 500), "white").save(perf_png)
            warnings.append("Performance chart could not be generated for this report.")
    else:
        perf_png = os.path.join(workdir, "performance_chart.png")
        crop_performance_chart(input_pdf, page_index=perf_page_index, crop_box=list(perf_crop_box),
                                out_png=perf_png, workdir=workdir, bottom_trim=(perf_bottom_trim or 0.0))

    c = canvas.Canvas(output_pdf, pagesize=letter)

    # ---------------------------------------------------------- PAGE 1 ----
    draw_header(c, data["client_name"], data["period_start"], data["period_end"], data["advisor"])

    act = data["activity"]
    top = PAGE_H - 1.65 * inch
    tile_w = (PAGE_W - 1.5 * inch) / 4
    return_pct = data.get("total_return_pct")
    return_str = f"{'+' if return_pct >= 0 else ''}{return_pct:.2f}%" if return_pct is not None else "–"
    return_color = (GOOD_GREEN if return_pct >= 0 else colors.HexColor("#B3261E")) if return_pct is not None else INK
    tiles = [
        ("Portfolio Value", money(act.get("Ending Market Value w/ Bond Accrual", data["total_value"])),
         NAVY,
         f"{'up' if act.get('Market Value Change', 0) >= 0 else 'down'} from "
         f"{money(act.get('Beginning Market Value', 0))} on {data['period_start']}"),
        ("Portfolio Return", return_str, return_color, "for the period"),
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

    accounts_bottom_y = draw_accounts_section(c, rule_y - 55, data)

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

    bucket_rows = []
    for cat in CATEGORY_ORDER:
        val = data["allocation_buckets"].get(cat, 0)
        if val <= 0:
            continue
        pct = 100 * val / data["total_value"]
        bucket_rows.append((cat, CATEGORY_COLORS[cat], f"{money(val)} · {pct:.1f}%"))
    col1_bottom = _donut_column(col1_x, donut_path, bucket_rows, row_h=38,
                                 label_font_size=9.5, value_font_size=8, two_line=True)

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

    draw_footer(c, 1, 3, generated_date)
    c.showPage()

    # ---------------------------------------------------------- PAGE 2 ----
    draw_header(c, data["client_name"], data["period_start"], data["period_end"], data["advisor"])

    hy = PAGE_H - 1.65 * inch
    section_bar(c, 0.75 * inch, hy - 18, PAGE_W - 1.5 * inch, 18, "HOLDINGS BY ASSET CLASS")

    col_value_right = 6.4 * inch
    col_pct_right = PAGE_W - 0.75 * inch

    y = hy - 18 - 20
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(col_value_right, y, "MARKET VALUE")
    c.drawRightString(col_pct_right, y, "% OF PORTFOLIO")
    y -= 12

    holdings_by_cat = data.get("holdings_by_category", {})
    for cat in CATEGORY_ORDER:
        info = holdings_by_cat.get(cat)
        if not info or not info.get("top"):
            continue

        y -= 10
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
        y -= 22

        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(0.75 * inch, y, PAGE_W - 0.75 * inch, y)
        y -= 15

        for h in info["top"]:
            name_lines = wrap_text(h["security"], "Helvetica", 9.5, col_value_right - 0.75 * inch - 0.3 * inch)
            c.setFillColor(INK)
            c.setFont("Helvetica", 9.5)
            c.drawString(0.75 * inch, y, name_lines[0])
            c.setFillColor(SECONDARY_INK)
            c.drawRightString(col_value_right, y, money(h["market_value"]))
            c.drawRightString(col_pct_right, y, f"{h['allocation_pct']:.2f} %")
            y -= 15.5

    draw_footer(c, 2, 3, generated_date)
    c.showPage()

    # ---------------------------------------------------------- PAGE 3 ----
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

    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 7.8)
    legend = data.get("benchmark_legend")
    if legend:
        benchmark_desc = f"the S&P 500, {legend['blend']}, and the {legend['bond']} index"
    else:
        benchmark_desc = "the S&P 500 and selected benchmark indices"
    c.drawString(0.75 * inch, chart_top - draw_h - 14,
                 f"Cumulative return, {data['period_start']} – {data['period_end']}, vs. {benchmark_desc}.")

    draw_gain_loss_section(c, chart_top - draw_h - 14 - 36, data)

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

    draw_footer(c, 3, 3, generated_date)
    c.showPage()

    c.save()

    return {"data": data, "warnings": warnings}
