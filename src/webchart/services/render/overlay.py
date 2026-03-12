"""Lightweight overlay generation for interaction indicators."""

import io

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")


def render_crosshair(
    width: int,
    height: int,
    x_pixel: float,
    y_pixel: float,
    color: str = "#ff0000",
    line_width: float = 1.0,
) -> bytes:
    """Render a crosshair overlay as a transparent PNG."""
    dpi = 100
    fig, ax = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    ax.set_alpha(0)
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    ax.set_xlim(0, width)
    ax.set_ylim(0, height)

    # Vertical line
    ax.axvline(x=x_pixel, color=color, linewidth=line_width, alpha=0.7)
    # Horizontal line
    ax.axhline(y=height - y_pixel, color=color, linewidth=line_width, alpha=0.7)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True, bbox_inches="tight", pad_inches=0)
    plt.close(fig)

    buf.seek(0)
    return buf.read()
