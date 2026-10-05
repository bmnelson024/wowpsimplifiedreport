"""
Data extraction for the White Oak simplified client review.

Pulls the numbers we need out of a standard Orion "Annual Review Report" /
"Client Review" PDF export. Built against the structure of a 41-page export
(Activity Summary + Allocation Overview on page 1, Portfolio Overview /
accounts on page 3, Performance & Benchmark chart around the "Performance"
section header). If Orion changes its layout this will need re-checking,
but the parsing keys off table headers / row labels wherever possible, so
it should be reasonably tolerant of accounts being added/removed.

This is a server-side port of the `white-oak-simplified-review` skill's
extract_data.py. The extraction logic below (money/pct parsing, the Orion
quirk workarounds, holdings/gain-loss/benchmark extraction) is reproduced
verbatim from the verified skill. The one addition is
`locate_and_crop_performance_chart()`, which automates what the skill
previously did by hand each time (rendering a candidate page and visually
measuring the chart's crop box) -- see its docstring for the method and
its limits.
"""
import os
import re
import subprocess

import pdfplumber


def _money(s):
    if s is None:
        return 0.0
    s = s.replace("\n", " ").strip()
    neg = s.startswith("-")
    s = s.replace("$", "").replace(",", "").replace("-", "").strip()
    try:
        val = float(s)
    except ValueError:
        val = 0.0
    return -val if neg else val


def _pct(s):
    if s is None:
        return 0.0
    s = s.replace("\n", " ").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _clean_label(s):
    return " ".join((s or "").replace("\n", " ").split())


def _dedupe_pairs(s):
    """Orion renders bold text (headers, Total rows) with every character
    doubled (e.g. 'PPeerriioodd' -> 'Period'). Used only to *detect* those
    rows for filtering -- never applied to real data, since it would also
    mangle legitimately double-lettered words (e.g. 'AbbVie')."""
    out = []
    i, n = 0, len(s)
    while i < n:
        if i + 1 < n and s[i] == s[i + 1]:
            out.append(s[i])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _is_header_or_total(s, targets):
    d = _dedupe_pairs(s or "").strip().lower()
    return any(d == t.lower() for t in targets)


# Rolls Orion's ~15 granular allocation buckets into the 4 categories a
# client actually wants to see. Extend this map as new labels show up.
CATEGORY_MAP = {
    "large cap": "Equities",
    "mid cap": "Equities",
    "small cap": "Equities",
    "international": "Equities",
    "emerging markets": "Equities",
    "europe stock": "Equities",
    "japan stock": "Equities",
    "focused region": "Equities",
    "pacific/asia ex-japan stk": "Equities",
    "us stock": "Equities",
    "sector": "Equities",
    "high yield bond": "Fixed Income",
    "municipal bond": "Fixed Income",
    "corporate bond": "Fixed Income",
    "government bond": "Fixed Income",
    "us bond": "Fixed Income",
    "international bond": "Fixed Income",
    "cash": "Cash & Equivalents",
    "money market": "Cash & Equivalents",
}


def categorize(label):
    key = label.lower()
    return CATEGORY_MAP.get(key, "Other")


def top_allocation_breakdown(allocation_rows, top_n=7):
    """Orion's raw Allocation Overview rows (Large Cap, Mid Cap, High Yield
    Bond, etc.) sorted by value descending, keeping the largest `top_n`
    individually and folding everything past that into a single 'Other'
    row."""
    rows = sorted(allocation_rows, key=lambda r: r["value"], reverse=True)
    kept, rest = rows[:top_n], rows[top_n:]
    out = [
        {"label": r["label"], "value": r["value"], "pct": r["pct"], "parent": categorize(r["label"])}
        for r in kept
    ]
    if rest:
        out.append({
            "label": "Other",
            "value": sum(r["value"] for r in rest),
            "pct": sum(r["pct"] for r in rest),
            "parent": "Other",
        })
    return out


def _find_section_page(pdf, title):
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.split("\n")[:4]]
        if title in lines:
            return i
    return None


def _all_holdings(pdf):
    """Pulls every individual security out of the Portfolio Appraisal
    tables (pages between the report start and the 'Performance' section)."""
    performance_start = _find_section_page(pdf, "Performance")
    end = performance_start if performance_start is not None else len(pdf.pages)

    holdings = []
    current_section = None
    for i in range(0, end):
        for t in pdf.pages[i].extract_tables():
            for row in t:
                if not row or len(row) != 10 or row[0] is None:
                    continue
                label = _clean_label(row[0])
                if not label:
                    continue
                if all(c is None for c in row[1:]):
                    current_section = _dedupe_pairs(label)
                    continue
                if row[1] is None or row[1] == "":
                    continue
                if _dedupe_pairs(label).lower().startswith("total"):
                    continue
                if "$" not in str(row[4] or ""):
                    continue
                holdings.append({
                    "security": label,
                    "market_value": _money(row[4]),
                    "allocation_pct": _pct(row[5]),
                    "section": current_section,
                })
    return holdings


# Maps Orion's Portfolio Appraisal section labels to the same 4 buckets
# used for the Asset Allocation donut, so holdings lists line up with it.
SECTION_CATEGORY_MAP = {
    "Equity": "Equities",
    "Bond": "Fixed Income",
    "Money Market": "Cash & Equivalents",
}


def extract_top_holdings(pdf, top_n=20):
    holdings = _all_holdings(pdf)
    holdings.sort(key=lambda h: h["market_value"], reverse=True)
    return holdings[:top_n]


def extract_holdings_by_category(pdf, top_n=10):
    holdings = _all_holdings(pdf)
    by_cat = {}
    for h in holdings:
        cat = SECTION_CATEGORY_MAP.get(h["section"], "Other")
        by_cat.setdefault(cat, []).append(h)

    result = {}
    for cat, items in by_cat.items():
        items.sort(key=lambda h: h["market_value"], reverse=True)
        result[cat] = {
            "top": items[:top_n],
            "count": len(items),
            "total_value": sum(h["market_value"] for h in items),
        }
    return result


def _money_loose(s):
    """Like _money, but tolerant of stray footnote-marker characters Orion
    sometimes appends after the number."""
    if not s:
        return 0.0
    m = re.search(r"(-?)\$?([\d,]+\.\d{2})", s)
    if not m:
        return 0.0
    val = float(m.group(2).replace(",", ""))
    return -val if m.group(1) == "-" else val


def extract_gain_loss_summary(pdf):
    """Realized and unrealized gain/loss totals -- see the skill's notes on
    the Unrealized Gain/Loss column bug: unrealized is computed as
    Market Value - Cost Basis rather than trusting Orion's own (duplicate)
    column."""
    realized_start = _find_section_page(pdf, "Realized Gain/Loss")
    unrealized_start = _find_section_page(pdf, "Unrealized Gain/Loss")
    end = len(pdf.pages)

    def _grand_total_row(start, stop):
        if start is None:
            return None
        for i in range(start, stop):
            for t in pdf.pages[i].extract_tables():
                for row in t:
                    if not row or row[0] is None or len(row) < 7:
                        continue
                    label = _dedupe_pairs(_clean_label(row[0])).lower()
                    if label == "total:":
                        return row
        return None

    result = {"realized_gain": None, "unrealized_gain": None}

    realized_row = _grand_total_row(realized_start, unrealized_start or end)
    if realized_row:
        result["realized_gain"] = _money_loose(_dedupe_pairs(realized_row[6] or ""))

    unrealized_row = _grand_total_row(unrealized_start, end)
    if unrealized_row:
        cost_basis = _money_loose(_dedupe_pairs(unrealized_row[4] or ""))
        market_value = _money_loose(_dedupe_pairs(unrealized_row[5] or ""))
        result["unrealized_gain"] = market_value - cost_basis

    return result


def extract_total_return(pdf):
    """Household-level total return % for the period."""
    start = end = None
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.split("\n")[:4]]
        if "Performance" in lines and start is None:
            start = i
        if "Realized Gain/Loss" in lines and end is None:
            end = i
    if start is None:
        return None
    if end is None:
        end = len(pdf.pages)

    pattern = re.compile(
        r"^Total:\s*\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})\s+(-?[\d.]+)\s*%$"
    )
    for i in range(start, end):
        text = pdf.pages[i].extract_text() or ""
        for line in text.split("\n"):
            ls = line.strip()
            if ls.startswith("TToottaall::"):
                deduped = _dedupe_pairs(ls)
                m = pattern.match(deduped)
                if m:
                    return float(m.group(4))
    return None


def extract_benchmark_legend(pdf, client_name):
    """Finds the legend line on the "Performance And Benchmark" chart page
    and splits it into the three benchmark labels. Returns None if not
    found, so the caller falls back to a generic caption rather than
    guessing."""
    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.split("\n"):
            if "Morningstar US Core Bond" in line and "S&P 500" in line:
                rest = line.replace(client_name, "").strip()
                m = re.search(r"(Morningstar US Core Bond)\s*(S&P 500\s*\([^)]*\))\s*(.+)", rest)
                if m:
                    return {
                        "bond": m.group(1).strip(),
                        "market": m.group(2).strip(),
                        "blend": m.group(3).strip(),
                    }
    return None


def extract_benchmark_returns(pdf, period_start, period_end):
    """Reads the "Performance History" table that Orion prints on the
    "Performance And Benchmark" chart page: one row per series (the
    household first, then each benchmark) with its return for the report's
    review period in the "Period" column. These are Orion's own reported
    numbers -- nothing is measured off the chart.

    Returns ({"period_start", "period_end", "series": [{"label","pct"}...]},
    None), or (None, reason) when the table isn't in this export (older
    exports / Orion report templates without it), so the caller can leave
    the table out instead of guessing."""
    idx = _find_candidate_chart_page(pdf)
    if idx is None:
        return None, "performance chart page not found"

    pct_re = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%$")
    # The table sits under the chart; allow for it spilling to the next page.
    for page in pdf.pages[idx: idx + 2]:
        for table in page.extract_tables():
            if not table or not table[0] or "performance history" not in (table[0][0] or "").lower():
                continue
            rows = []
            for row in table[1:]:
                if not row or len(row) < 2:
                    continue
                label = " ".join((row[0] or "").split())
                m = pct_re.match((row[1] or "").strip())
                if not label or not m:
                    continue  # header row, or the doubled-text "Total:" row
                if _dedupe_pairs(label).lower().startswith("total"):
                    continue
                rows.append({"label": label, "pct": float(m.group(1))})
            if len(rows) >= 2:
                return {"period_start": period_start, "period_end": period_end, "series": rows}, None
            return None, "Performance History table had no readable rows"
    return None, "no Performance History table in this export"


# ---------------------------------------------------------------------------
# Automatic performance-chart page/crop detection.
#
# The skill originally had a human (or a Claude session) render candidate
# pages to PNG, look for the "Performance And Benchmark" chart (distinct
# from the single-line "Performance" summary chart on page 1), and measure
# its crop box by eye. This automates both steps:
#
#   1. Page selection: search each page's first few lines of text for a
#      section title that starts with "Performance" and also mentions
#      "Benchmark" -- this is deliberately a little looser than an exact
#      string match on "Performance And Benchmark", in case Orion's exact
#      wording varies slightly between report versions, while still
#      excluding the bare "Performance" summary section.
#   2. Crop measurement: renders the candidate page to PNG and scans for
#      rows that are mostly the navy section-header color (~RGB(1,69,107))
#      -- the same landmark the skill's manual instructions used. The chart
#      sits between the bottom of its own header bar and either the top of
#      the next header bar on the same page, or the page bottom if none is
#      found.
#
# IMPORTANT: an earlier version of this did the bar-row scan on a rendered
# PNG (pixel space) but then converted those pixel rows to PDF points and
# applied them as a pypdf crop box in the *source page's own coordinate
# space*. That mixes two different frames whenever the page has a /Rotate
# transform (common for a landscape chart embedded in an otherwise-portrait
# export): pdftoppm renders the page as a human sees it (rotation applied),
# but pypdf's cropbox/mediabox operate on the page's raw, pre-rotation
# coordinates -- so a crop box measured from the rendered image landed in
# the wrong place on the raw page, pulling in the page's own running header
# above the chart and a chunk of the next table below it. Confirmed against
# a real client export (McSwain) where the auto-generated report's page 3
# included "Client Review - White Oak / Page 8 of 23" above the chart and a
# "Performance Summary" holdings table below it -- exactly this failure
# mode.
#
# Fixed by never leaving pixel space: the candidate page is rendered once,
# the navy bars are found in that same image, and the chart is cropped
# directly out of that same PNG with PIL -- no PDF-point conversion, no
# pypdf cropbox, so page rotation can't introduce a mismatch.
# ---------------------------------------------------------------------------

NAVY_RGB = (1, 69, 107)
NAVY_TOL = 45
CHART_CROP_DPI = 300


def _find_candidate_chart_page(pdf):
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.split("\n")[:4]]
        for line in lines:
            if line.lower().startswith("performance") and "benchmark" in line.lower():
                return i
    # Fall back to the bare "Performance" section if no more specific title
    # is found -- better to point at *a* performance-related page than fail
    # outright, and the caller surfaces a warning either way when this
    # fallback path is used.
    return _find_section_page(pdf, "Performance")


SCAN_MAX_DIM = 900  # longest side, in px, of the downsampled copy used for
                     # bar-detection math -- keeps the numpy work cheap on a
                     # memory-constrained host regardless of render DPI.


def _scan_navy_bars(im):
    """Returns (bars, img_h_px, img_w_px) in `im`'s own (full-resolution)
    pixel coordinates. `bars` is a list of (start_row, end_row) pixel ranges
    where a horizontal band of the image is mostly the navy section-header
    color.

    `im` is the already-opened, full-resolution PIL Image (never re-opened
    here, so only one decoded copy of the page ever exists in memory at
    once). The color-matching math itself runs on a small downsampled copy
    -- a full-resolution 300dpi page upcast for a signed color-difference
    comparison can easily be 150-200MB+ for one temporary array, which is
    enough on its own to exceed a memory-constrained host's limit. Detected
    row ranges are scaled back up to the caller's full-resolution coordinate
    space before returning, so the crop itself still uses the real pixels."""
    import numpy as np

    img_w, img_h = im.size
    scale = min(1.0, SCAN_MAX_DIM / max(img_w, img_h))
    small = im.resize((max(1, round(img_w * scale)), max(1, round(img_h * scale)))) if scale < 1.0 else im

    # int16 is plenty for a signed RGB difference (range -255..255) and is
    # a quarter the size of numpy's default platform int (int64).
    arr = np.asarray(small, dtype=np.int16)
    target = np.array(NAVY_RGB, dtype=np.int16)
    close = (np.abs(arr - target) <= NAVY_TOL).all(axis=2)
    row_frac = close.mean(axis=1)
    bar_rows = np.where(row_frac > 0.5)[0]

    bars_small = []
    if len(bar_rows):
        start = prev = bar_rows[0]
        for r in bar_rows[1:]:
            if r - prev > 3:
                bars_small.append((start, prev))
                start = r
            prev = r
        bars_small.append((start, prev))

    if scale < 1.0:
        inv_scale = img_h / small.size[1]
        bars = [(int(s * inv_scale), int(e * inv_scale) + 1) for s, e in bars_small]
    else:
        bars = bars_small
    return bars, img_h, img_w


def locate_and_crop_performance_chart(pdf_path, workdir, dpi=CHART_CROP_DPI):
    """Finds the performance chart page, renders it, and crops out just the
    chart + legend band, entirely in pixel space (see the module note above
    for why). Returns (out_png_path or None, warnings)."""
    warnings = []
    with pdfplumber.open(pdf_path) as pdf:
        page_index = _find_candidate_chart_page(pdf)
    if page_index is None:
        warnings.append(
            "Could not find a 'Performance' chart page in this export -- "
            "page 3's performance chart may be missing or misplaced."
        )
        return None, warnings

    png_prefix = os.path.join(workdir, "_perf_full")
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", "-f", str(page_index + 1),
         "-l", str(page_index + 1), pdf_path, png_prefix],
        check=True, capture_output=True,
    )
    # pdftoppm's single-page output filename varies by version/platform --
    # it may be "<prefix>.png", "<prefix>-<page>.png" with no padding, or
    # "<prefix>-<page>.png" zero-padded to the digit width of the *last*
    # page number requested (e.g. "-08" vs "-8" vs "-23"), and the padding
    # width isn't reliably predictable from page_index alone. Rather than
    # guess every exact filename, glob for whatever got written next to the
    # prefix -- there's only ever one PNG for a single requested page.
    import glob
    matches = sorted(glob.glob(f"{png_prefix}*.png"))
    full_png = matches[0] if matches else None
    if full_png is None:
        warnings.append("Could not render the candidate chart page.")
        return None, warnings

    from PIL import Image
    im = Image.open(full_png).convert("RGB")
    img_w, img_h = im.size

    bars, _, _ = _scan_navy_bars(im)
    out_png = os.path.join(workdir, "performance_chart.png")

    if not bars:
        warnings.append(
            "Could not detect the section header bar on the performance chart "
            "page -- using the full page as the chart crop, which may include "
            "extra header/footer content."
        )
        im.save(out_png)
        return out_png, warnings

    top_px = bars[0][1] + 2  # just below the first (this chart's own) bar

    if len(bars) > 1:
        bottom_px = bars[1][0] - 2  # just above the next section's bar
    else:
        # No second header bar on this page -- assume the chart runs most of
        # the remaining page, leaving a conservative margin for a footer.
        bottom_px = img_h - int(img_h * 0.15)
        warnings.append(
            "Only one section header found on the performance chart page -- "
            "assumed the chart extends to near the bottom of the page. "
            "Worth a one-time spot check against the source PDF."
        )

    if bottom_px <= top_px:
        warnings.append(
            "Performance chart crop measurement looked inverted or too small -- "
            "using the full page as the crop."
        )
        im.save(out_png)
        return out_png, warnings

    im.crop((0, top_px, img_w, bottom_px)).save(out_png)
    return out_png, warnings


def extract(pdf_path):
    data = {}
    with pdfplumber.open(pdf_path) as pdf:
        page1 = pdf.pages[0]
        tables = page1.extract_tables()

        # Table 0: Household / Period / Advisor
        meta = tables[0]
        meta_map = {row[0].strip(): row[1] for row in meta if row and row[0] and len(row) > 1 and row[1]}
        data["client_name"] = meta_map.get("Household:", "Client").strip()
        period_raw = meta_map.get("Period:", "").strip()
        data["period_raw"] = period_raw
        m = re.match(r"(\d+/\d+/\d+)\s*to\s*(\d+/\d+/\d+)", period_raw)
        data["period_start"] = m.group(1) if m else ""
        data["period_end"] = m.group(2) if m else ""
        data["advisor"] = meta_map.get("Financial Advisor:", "").strip()

        # Table 1: Activity Summary -- use the "Period" column (index 1)
        activity = {}
        for row in tables[1]:
            label = _clean_label(row[0])
            if not row[0] or label in ("",) or "Period" in "".join(str(c) for c in row if c):
                continue
            if len(row) >= 2 and row[1] and "$" in str(row[1]):
                activity[label] = _money(row[1])
        data["activity"] = activity

        # Table 2: Allocation Overview -- rows are [None, label, value, pct]
        allocation_rows = []
        for row in tables[2]:
            if len(row) < 4:
                continue
            label, value, pct = row[1], row[2], row[3]
            if not label or not value or not pct:
                continue
            label = _clean_label(label)
            if _is_header_or_total(label, ["Total:", "Total"]):
                continue
            if "$" not in str(value):
                continue
            allocation_rows.append({
                "label": label,
                "value": _money(value),
                "pct": _pct(pct),
            })
        data["allocation_detail"] = allocation_rows

        buckets = {}
        for row in allocation_rows:
            cat = categorize(row["label"])
            buckets[cat] = buckets.get(cat, 0.0) + row["value"]
        data["allocation_buckets"] = buckets
        data["allocation_breakdown"] = top_allocation_breakdown(allocation_rows, top_n=7)

        # Page 3: Portfolio Overview (accounts)
        page3 = pdf.pages[2]
        accounts = []
        for t in page3.extract_tables():
            if not t or not t[0] or not t[0][0]:
                continue
            if "Portfolio Overview" not in str(t[0][0]):
                continue
            for row in t[1:]:
                if not row or not row[0]:
                    continue
                acct_no = _clean_label(row[0])
                if _is_header_or_total(acct_no, ["Total:", "Total", "Account Number"]):
                    continue
                accounts.append({
                    "account_number": acct_no,
                    "registration": _clean_label(row[1]),
                    "type": _clean_label(row[2]),
                    "style": _clean_label(row[3]),
                    "value": _money(row[4]),
                })
        data["accounts"] = accounts
        data["total_return_pct"] = extract_total_return(pdf)
        data["top_holdings"] = extract_top_holdings(pdf, top_n=20)
        data["holdings_by_category"] = extract_holdings_by_category(pdf, top_n=10)
        data["gain_loss"] = extract_gain_loss_summary(pdf)
        data["benchmark_legend"] = extract_benchmark_legend(pdf, data["client_name"])
        try:
            data["benchmark_returns"], data["benchmark_returns_issue"] = extract_benchmark_returns(
                pdf, data["period_start"], data["period_end"])
        except Exception as e:  # never let this optional table break the report
            data["benchmark_returns"], data["benchmark_returns_issue"] = None, f"error: {e}"

    data["total_value"] = data["activity"].get("Ending Market Value w/ Bond Accrual") \
        or sum(a["value"] for a in data["accounts"])

    return data
