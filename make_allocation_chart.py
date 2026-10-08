"""Builds the simplified asset-allocation donut chart."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

NAVY = "#1B3557"
GOLD = "#C9A669"
STEEL = "#6E93A5"
GRAY = "#CFCCC3"

CATEGORY_COLORS = {
    "Equities": NAVY,
    "Fixed Income": GOLD,
    "Cash & Equivalents": STEEL,
    "Other": GRAY,
}
CATEGORY_ORDER = ["Equities", "Fixed Income", "Cash & Equivalents", "Other"]


def build_donut(buckets, out_path, total_value):
    labels = [c for c in CATEGORY_ORDER if buckets.get(c, 0) > 0]
    values = [buckets[c] for c in labels]
    colors = [CATEGORY_COLORS[c] for c in labels]

    fig, ax = plt.subplots(figsize=(4.4, 4.4), dpi=300)
    wedges, _ = ax.pie(
        values,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.38, edgecolor="#FFFFFF", linewidth=3),
    )
    ax.set_aspect("equal")

    ax.text(0, 0.10, "Total Portfolio", ha="center", va="center",
             fontsize=13, color="#52514E", family="sans-serif")
    ax.text(0, -0.08, f"${total_value:,.0f}", ha="center", va="center",
             fontsize=17, color=NAVY, weight="bold", family="sans-serif")

    plt.tight_layout(pad=0.2)
    fig.savefig(out_path, transparent=True)
    plt.close(fig)


def _lighten(hex_color, amount):
    """Mixes a hex color toward white by `amount` (0 = unchanged, 1 =
    white). Used to shade a granular classification's color as a tint of
    its parent bucket's color, so e.g. Large/Mid/Small Cap read as a family
    of navy shades under the same Equities navy used on the 4-bucket
    donut, rather than introducing a whole new unrelated palette."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r = r + (255 - r) * amount
    g = g + (255 - g) * amount
    b = b + (255 - b) * amount
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def classification_colors(rows):
    """Assigns each row (from extract_data.top_allocation_breakdown, sorted
    largest-first) a shade of its parent bucket's color -- darkest for the
    largest row in that bucket, lighter for smaller ones in the same
    bucket -- so the chart reads as a further breakdown of the 4-bucket
    donut rather than a disconnected palette."""
    shade_steps = [0.0, 0.30, 0.50, 0.65, 0.75]
    seen_in_parent = {}
    colors = []
    for r in rows:
        base = CATEGORY_COLORS.get(r["parent"], GRAY)
        n = seen_in_parent.get(r["parent"], 0)
        seen_in_parent[r["parent"]] = n + 1
        colors.append(_lighten(base, shade_steps[min(n, len(shade_steps) - 1)]))
    return colors


def build_breakdown_donut(rows, out_path, center_lines=("Asset", "Classification")):
    """Second, more granular donut (Large Cap, Mid Cap, High Yield Bond,
    etc. -- `rows` from extract_data.top_allocation_breakdown), colored as
    shades of the 4-bucket donut's own colors via `classification_colors`.
    No dollar total in the center (that's already shown on the 4-bucket
    donut right next to this one) -- just a short label, stacked on two
    lines so it fits inside the hole, so it doesn't sit empty."""
    values = [r["value"] for r in rows]
    colors = classification_colors(rows)

    fig, ax = plt.subplots(figsize=(4.4, 4.4), dpi=300)
    ax.pie(
        values,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.38, edgecolor="#FFFFFF", linewidth=3),
    )
    ax.set_aspect("equal")
    ax.text(0, 0.08, center_lines[0], ha="center", va="center",
             fontsize=12.5, color="#52514E", family="sans-serif")
    ax.text(0, -0.08, center_lines[1], ha="center", va="center",
             fontsize=12.5, color="#52514E", family="sans-serif")

    plt.tight_layout(pad=0.2)
    fig.savefig(out_path, transparent=True)
    plt.close(fig)
