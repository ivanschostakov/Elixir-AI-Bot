import math
import uuid
from pathlib import Path
from typing import Sequence

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

from config import DATA_DIR


def save_single_plot(x_data: Sequence[float], y_data: Sequence[float], title: str, y_label: str, legend_label: str, filename: str, info: dict | None = None):
    plt.figure(figsize=(12, 6))

    plt.plot(x_data, y_data, linewidth=2.2, label=legend_label)
    plt.fill_between(x_data, y_data, 0, alpha=0.18)

    ax = plt.gca()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_alpha(0.30)
    ax.spines["bottom"].set_alpha(0.30)

    x_min = float(x_data[0]) if x_data else 0.0
    x_max = float(x_data[-1]) if x_data else 0.0
    start_tick = math.floor(x_min / 2.0) * 2.0
    end_tick = math.ceil(x_max / 2.0) * 2.0
    ax.set_xticks([start_tick + 2.0 * index for index in range(int((end_tick - start_tick) / 2.0) + 1)])

    y_max = max(y_data) if y_data else 0.0
    ax.set_ylim(0.0, max(1e-9, 2.0 * float(y_max)))

    ax.set_title(title, fontsize=11, pad=8)
    ax.set_xlabel("Дни от начала", fontsize=7)
    ax.set_ylabel(y_label, fontsize=7)

    ax.tick_params(axis="x", labelsize=6, pad=2)
    ax.tick_params(axis="y", labelsize=6, pad=2)

    ax.grid(True, which="major", linestyle="-", alpha=0.18)
    ax.grid(True, which="minor", linestyle="--", alpha=0.10)
    ax.minorticks_on()
    ax.legend(fontsize=7, loc="upper right", frameon=True, fancybox=True, framealpha=0.85)

    if info:
        tmax_h = info.get("tmax_h")
        tmax_text = f"{float(tmax_h):.1f}ч" if tmax_h is not None else "н/д"
        text = (
            f"Доза: {info.get('dose_mg', 0):g}мг\n"
            f"Интервал: {info.get('interval_days', 0):g}д\n"
            f"Курс: {info.get('weeks', 0):g}нед\n"
            f"Усвоение: {info.get('F', 1.0) * 100:.2f}%\n"
            f"t½: {info.get('t_half_days', 0):.2f}д\n"
            f"Tmax: {tmax_text}"
        )
        ax.text(0.02, 0.98, text, transform=ax.transAxes, va="top", ha="left", fontsize=7, bbox=dict(boxstyle="round,pad=0.35", facecolor="white", alpha=0.82, edgecolor="#cccccc", linewidth=0.8))

    plt.tight_layout()
    plt.savefig(DATA_DIR / filename, dpi=200, bbox_inches="tight")
    plt.close()


def _nice_max(value: float) -> float:
    if value <= 100 or not math.isfinite(value): return 100.0

    exponent = 10 ** int(math.floor(math.log10(value)))
    mantissa = value / exponent
    if mantissa <= 1: nice = 1
    elif mantissa <= 2: nice = 2
    elif mantissa <= 5: nice = 5
    else: nice = 10
    return float(nice * exponent)


def plot_filled_scale(value: float, max_value: float | None = 100, major_step: int = 5, minor_step: int = 1, figsize=(12, 2.0), fill_color: str = "#62b25f", bg_color: str = "#e9eef2", tick_color: str = "#6b6f76", label_color: str = "#3b3f45") -> Path:
    if value < 0: raise ValueError("value must be >= 0")

    is_inf = math.isinf(value)
    if max_value is None: max_value = _nice_max(value if math.isfinite(value) else 100.0)
    if max_value <= 0: raise ValueError("max_value must be > 0")

    v_draw = max_value if is_inf else min(value, max_value)

    fig, ax = plt.subplots(figsize=figsize)
    ax.set_xlim(-0.5, max_value + 0.5)
    ax.set_ylim(0.0, 1.8)
    ax.axis("off")

    bar_y = 1.05
    bar_h = 0.55
    radius = bar_h / 2.0

    ax.add_patch(FancyBboxPatch((0, bar_y), max_value, bar_h, boxstyle=f"round,pad=0,rounding_size={radius}", linewidth=0, facecolor=bg_color))
    ax.add_patch(FancyBboxPatch((0, bar_y), v_draw, bar_h, boxstyle=f"round,pad=0,rounding_size={radius}", linewidth=0, facecolor=fill_color))

    baseline_y = bar_y + bar_h * 0.62
    tick_top = baseline_y
    tick_bottom_major = bar_y + bar_h * 0.18
    tick_bottom_mid = bar_y + bar_h * 0.26
    tick_bottom_minor = bar_y + bar_h * 0.33
    label_y = bar_y + bar_h * 0.08
    ax.plot([0.0, float(max_value)], [baseline_y, baseline_y], color=tick_color, linewidth=1)

    label_step = major_step
    if max_value > 200: label_step = major_step * 2
    if max_value > 500: label_step = major_step * 4

    current = 0.0
    while current <= float(max_value) + 1e-9:
        if major_step > 0 and abs(current % (major_step * 2)) < 1e-9: y0, linewidth = tick_bottom_major, 1.2
        elif major_step > 0 and abs(current % major_step) < 1e-9: y0, linewidth = tick_bottom_mid, 1.1
        else: y0, linewidth = tick_bottom_minor, 0.9

        ax.plot([current, current], [tick_top, y0], color=tick_color, linewidth=linewidth)
        if label_step > 0 and abs(current % label_step) < 1e-9: ax.text(current, label_y, f"{int(round(current))}", ha="center", va="bottom", fontsize=10, color=label_color)
        current += float(minor_step)

    if is_inf: ax.text(max_value, bar_y + bar_h + 0.05, "∞", ha="right", va="bottom", fontsize=12, color=label_color)

    fig.tight_layout(pad=0.6)
    out_path = DATA_DIR / f"{uuid.uuid4().hex}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return out_path
