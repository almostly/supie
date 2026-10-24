"""Diagrams for the flow walkthrough, in the style of supie's reliability replay."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from supie.flow import Flow

BG = "#121214"
PANEL = "#1c1c20"
LINE = "#3a3a42"
TEXT = "#e8e8ec"
MUTED = "#9696a0"
GREEN = "#3ecf8e"
AMBER = "#f0b43c"
RED = "#eb5757"
BLUE = "#6ea0ff"
GRADIENT = LinearSegmentedColormap.from_list("almostly", ["#a78bfa", "#6ee7b7"])

STATUS = {"succeeded": GREEN, "retried": AMBER, "failed": RED, "skipped": MUTED}


@dataclass
class Attempt:
    """One attempt of one step, in seconds since the run started."""

    step: str
    attempt: int
    start: float
    end: float
    ok: bool


def _font() -> str:
    """Inter's family name if a static Inter file is installed, else a stock sans."""
    for folder in (
        Path.home() / "Library/Fonts",
        Path("/Library/Fonts"),
        Path.home() / ".local/share/fonts",
    ):
        for path in sorted(folder.glob("Inter*-Regular.ttf")):
            font_manager.fontManager.addfont(str(path))
            return font_manager.FontProperties(fname=str(path)).get_name()
    return "DejaVu Sans"


def style() -> None:
    """Dark figures, with Inter where it's installed."""
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    plt.rcParams.update(
        {
            "figure.facecolor": BG,
            "axes.facecolor": BG,
            "savefig.facecolor": BG,
            "text.color": TEXT,
            "font.family": _font(),
            "font.size": 11,
            "figure.dpi": 100,
        }
    )


def _canvas(width: float, height: float, title: str) -> tuple[Figure, Axes]:
    """A figure with a title and the almostly gradient under it."""
    fig, ax = plt.subplots(figsize=(width, height))
    ax.set_axis_off()
    fig.text(0.03, 0.93, title, fontsize=14, color=TEXT, va="center")
    bar = fig.add_axes((0.03, 0.875, 0.22, 0.008))
    bar.imshow([[i / 99 for i in range(100)]], aspect="auto", cmap=GRADIENT)
    bar.set_axis_off()
    return fig, ax


def _levels(flow: Flow) -> dict[str, int]:
    """Each step's depth: one more than its deepest dependency."""
    level: dict[str, int] = {}
    for step in flow.order:
        level[step.name] = 1 + max((level[d] for d in step.depends_on), default=-1)
    return level


def _condition(pattern: dict[str, Any] | None) -> str:
    """A when= pattern as a short label, such as route = approve."""
    if not pattern:
        return ""
    parts = []
    for value in pattern.values():
        if isinstance(value, dict):
            parts += [f"{k} = {v}" for k, v in value.items()]
    return ", ".join(parts)


def dag(
    flow: Flow, rows: dict[str, dict[str, Any]] | None = None, title: str = ""
) -> Figure:
    """The flow's steps and dependencies, colored by status when rows are given."""
    level = _levels(flow)
    columns: dict[int, list[str]] = {}
    for name, depth in level.items():
        columns.setdefault(depth, []).append(name)
    pos: dict[str, tuple[float, float]] = {}
    for depth, names in columns.items():
        for i, name in enumerate(names):
            pos[name] = (depth * 3.0, (len(names) - 1) / 2 - i)
    fig, ax = _canvas(10.5, 3.6, title or f"The {flow.name} flow")
    w, h = 1.5, 0.52
    for step in flow.order:
        x, y = pos[step.name]
        for dep in step.depends_on:
            dx, dy = pos[dep]
            arrow = FancyArrowPatch(
                (dx + w / 2, dy),
                (x - w / 2, y),
                arrowstyle="-|>",
                mutation_scale=11,
                color=LINE,
                lw=1.6,
                connectionstyle="arc3,rad=0",
            )
            ax.add_patch(arrow)
            label = _condition(step.when)
            if label:
                ax.text(
                    (dx + w / 2 + x - w / 2) / 2,
                    (dy + y) / 2,
                    label,
                    color=MUTED,
                    fontsize=8.5,
                    ha="center",
                    va="center",
                    bbox={
                        "boxstyle": "round,pad=0.25",
                        "facecolor": BG,
                        "edgecolor": LINE,
                        "lw": 0.8,
                    },
                )
    for step in flow.order:
        x, y = pos[step.name]
        row = (rows or {}).get(step.name)
        status = _status(row)
        edge = STATUS.get(status, LINE)
        box = FancyBboxPatch(
            (x - w / 2, y - h / 2),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.12",
            facecolor=PANEL,
            edgecolor=edge,
            lw=1.8,
            linestyle="--" if status == "skipped" else "-",
        )
        ax.add_patch(box)
        ax.text(
            x,
            y + 0.07,
            step.name,
            ha="center",
            va="center",
            fontsize=11,
            color=MUTED if status == "skipped" else TEXT,
        )
        ax.text(
            x,
            y - 0.13,
            _caption(step, row),
            ha="center",
            va="center",
            fontsize=8.5,
            color=MUTED,
        )
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 1.0, max(xs) + 1.0)
    ax.set_ylim(min(ys) - 0.75, max(ys) + 0.75)
    if rows:
        _legend(fig)
    return fig


def _status(row: dict[str, Any] | None) -> str | None:
    """succeeded, retried, failed or skipped; None before the run."""
    if row is None:
        return None
    if row["status"] == "succeeded" and row["attempts"] > 1:
        return "retried"
    return str(row["status"])


def _caption(step: Any, row: dict[str, Any] | None) -> str:
    """Retries before the run, attempts after it."""
    if row is None:
        return f"retries {step.retries}" if step.retries else ""
    if row["status"] == "skipped":
        return "skipped"
    n = row["attempts"]
    return f"{n} attempts" if n > 1 else "1 attempt"


def _legend(fig: Figure) -> None:
    """Status colors, top right."""
    handles = [
        Line2D([], [], marker="o", linestyle="", markersize=7, color=color, label=label)
        for label, color in STATUS.items()
    ]
    fig.legend(
        handles=handles,
        loc="upper right",
        bbox_to_anchor=(0.98, 0.99),
        ncol=4,
        frameon=False,
        labelcolor=MUTED,
        fontsize=9,
        handletextpad=0.2,
        columnspacing=1.2,
    )


def timeline(attempts: list[Attempt], order: list[str], title: str) -> Figure:
    """When each step's attempts ran, one row per step."""
    names = [n for n in order if any(a.step == n for a in attempts)]
    fig, ax = _canvas(9.5, 0.75 + 0.48 * len(names), title)
    ax.set_axis_on()
    end = max(a.end for a in attempts)
    for row, name in enumerate(names):
        y = len(names) - 1 - row
        ax.text(-0.035 * end, y, name, ha="right", va="center", fontsize=10, color=TEXT)
        for a in (a for a in attempts if a.step == name):
            width = a.end - a.start
            ax.plot(
                [a.start, a.end],
                [y, y],
                color=GREEN if a.ok else RED,
                lw=11,
                solid_capstyle="round",
                alpha=0.9,
            )
            if not a.ok:
                ax.text(
                    a.start + width / 2,
                    y + 0.33,
                    f"attempt {a.attempt} failed",
                    ha="center",
                    va="center",
                    fontsize=8.5,
                    color=RED,
                )
    ax.set_xlim(-0.02 * end, end * 1.04)
    ax.set_ylim(-0.6, len(names) - 0.4)
    ax.set_yticks([])
    for side in ("left", "right", "top"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(LINE)
    ax.tick_params(axis="x", colors=MUTED, labelsize=9)
    ax.set_xlabel("seconds since the run started", color=MUTED, fontsize=9)
    fig.subplots_adjust(
        left=0.13, right=0.97, top=0.8, bottom=0.22 if len(names) < 5 else 0.15
    )
    return fig
