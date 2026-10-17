"""
Weather vs. delay EDA charts for the MBTA weather/delay dashboard.

Every function takes an already-filtered dataframe (station / week scope is
handled in app.py) and returns either a summary dataframe or a plotly figure.
Units assume the Open-Meteo hourly fields: temperature °C (shown as °F),
rain mm, snowfall cm, wind gusts km/h, visibility m.
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DELAY_COL = "excess_travel_min_mean"
N_BINS = 10
MIN_OBS = 500  # buckets with fewer hours of data than this are flagged as less reliable

WEATHER_VARS = {
    # column: (display label, unit, plain-language "more of it" phrase)
    "temperature_2m": ("Temperature", "°F", "Warmer weather"),
    "rain": ("Rain", "mm/hr", "Heavier rain"),
    "snowfall": ("Snowfall", "cm/hr", "Heavier snow"),
    "wind_gusts_10m": ("Wind gusts", "km/h", "Stronger wind"),
    "visibility": ("Visibility", "m", "Better visibility"),
}

# Variables that are zero most hours -- zero gets its own bucket so the
# remaining buckets describe actual rain / snow intensity.
ZERO_HEAVY_VARS = {"rain", "snowfall"}

CONDITION_ORDER = ["Clear", "Rain", "Other precip", "Snow", "Freezing precip"]

# Dashboard palette (light -> dark)
CREAM = "#f1ead8"
TAN = "#d5c7ad"
SAGE = "#bec5a4"
OLIVE = "#8a8e75"
BROWN = "#68604d"
INK = "#2d2f22"
GRID = "rgba(104,96,77,0.18)"


def _style(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK, size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hoverlabel=dict(align="left", bgcolor=CREAM, bordercolor=BROWN, font=dict(color=INK)),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=TAN)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=TAN)
    return fig


# ---------------------------------------------------------------------------
# 1. Correlation triage
# ---------------------------------------------------------------------------

def _strength(r: float) -> str:
    a = abs(r)
    if a < 0.05:
        return "no clear link"
    if a < 0.15:
        return "weak link"
    if a < 0.3:
        return "moderate link"
    return "strong link"


def compute_weather_correlations(df: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation of the delay metric with each weather variable, strongest first."""
    cols = [DELAY_COL] + list(WEATHER_VARS)
    sub = df[cols].dropna(subset=[DELAY_COL])
    corr = sub.corr()[DELAY_COL].drop(DELAY_COL)

    out = pd.DataFrame({
        "variable": corr.index,
        "label": [WEATHER_VARS[v][0] for v in corr.index],
        "corr": corr.values,
    })
    out["abs_corr"] = out["corr"].abs()
    out["takeaway"] = [
        f"{WEATHER_VARS[v][2]} → {'longer' if r > 0 else 'shorter'} trips ({_strength(r)})"
        if abs(r) >= 0.05 else f"{_strength(r)}"
        for v, r in zip(out["variable"], out["corr"])
    ]
    return out.sort_values("abs_corr", ascending=False).reset_index(drop=True)


def build_correlation_chart(corr_df: pd.DataFrame) -> go.Figure:
    # plotly draws the first category at the bottom, so reverse to put the
    # strongest link on top
    plot_df = corr_df.iloc[::-1]
    colors = [BROWN if c >= 0 else OLIVE for c in plot_df["corr"]]

    fig = go.Figure(
        go.Bar(
            x=plot_df["abs_corr"],
            y=plot_df["label"],
            orientation="h",
            marker=dict(color=colors, cornerradius=4),
            text=plot_df["takeaway"],
            textposition="outside",
            textfont=dict(color=INK),
            cliponaxis=False,
            customdata=plot_df[["corr"]],
            hovertemplate="%{y}<br>correlation r = %{customdata[0]:+.3f}<extra></extra>",
        )
    )
    max_abs = max(float(corr_df["abs_corr"].max()), 0.05)
    fig.update_xaxes(
        range=[0, max_abs * 2.2], showticklabels=False, showgrid=False,
        title="Strength of link to delays  (longer bar = matters more)",
    )
    fig.update_layout(showlegend=False, bargap=0.35)
    return _style(fig, height=300)


# ---------------------------------------------------------------------------
# 2. Delay by weather magnitude
# ---------------------------------------------------------------------------

def compute_delay_by_bin(df: pd.DataFrame, var: str, n_bins: int = N_BINS) -> pd.DataFrame:
    """Bucket `var` into ~n_bins equal-width bins and summarise delay per bucket.

    The top 0.5% of values are folded into the last bucket so a few extreme
    readings don't squash every other bucket into the left of the chart.
    """
    sub = df[[var, DELAY_COL]].dropna()
    values = sub[var]
    if var == "temperature_2m":
        values = values * 9 / 5 + 32
    if sub.empty:
        return pd.DataFrame(columns=["bin_label", "x", "mean", "p90", "n_obs"])

    hi = values.quantile(0.995)
    if hi <= values.min():
        hi = values.max()
    clipped = values.clip(upper=hi)

    zero_heavy = var in ZERO_HEAVY_VARS and (values == 0).mean() > 0.5
    if zero_heavy:
        positive = clipped[clipped > 0]
        n_pos_bins = max(n_bins - 1, 1)
        if positive.empty:
            edges = np.array([0.0])
        else:
            edges = np.linspace(0, positive.max(), n_pos_bins + 1)
        bins = np.concatenate([[-np.inf], edges]) if len(edges) > 1 else np.array([-np.inf, 0.0])
        # (-inf, 0] is the "no rain / no snow" bucket
        cats = pd.cut(clipped, bins=bins, include_lowest=True)
    else:
        cats = pd.cut(clipped, bins=n_bins, include_lowest=True)

    grouped = sub.groupby(cats, observed=True)[DELAY_COL]
    out = pd.DataFrame({
        "mean": grouped.mean(),
        "p90": grouped.quantile(0.9),
        "n_obs": grouped.size(),
    }).reset_index(names="interval")

    is_last = out.index == len(out) - 1
    labels, xs = [], []
    for interval, last in zip(out["interval"], is_last):
        if np.isneginf(interval.left):
            labels.append("0 (none)")
            xs.append(0.0)
            continue
        left = max(interval.left, 0.0) if zero_heavy else interval.left
        right_txt = f"{interval.right:,.1f}+" if last and hi < values.max() else f"{interval.right:,.1f}"
        labels.append(f"{left:,.1f} – {right_txt}")
        xs.append((left + interval.right) / 2)
    out["bin_label"] = labels
    out["x"] = xs
    return out.drop(columns="interval")


def build_magnitude_chart(bin_df: pd.DataFrame, var: str, min_obs: int = MIN_OBS) -> go.Figure:
    label, unit, _ = WEATHER_VARS[var]
    low = (bin_df["n_obs"] < min_obs).to_numpy()

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.76, 0.24], vertical_spacing=0.06
    )
    custom = np.column_stack([
        bin_df["bin_label"],
        bin_df["n_obs"],
        np.where(low, "<br><i>Few hours of data — less reliable</i>", ""),
    ])

    series = [
        ("p90", "Bad trip (worst 10%)", OLIVE, "dot"),
        ("mean", "Typical trip", INK, "solid"),
    ]
    for col, name, color, dash in series:
        # legend-only entry so the legend swatch never takes the hollow low-data style
        fig.add_trace(
            go.Scatter(
                x=[None], y=[None], name=name, mode="lines+markers", legendgroup=name,
                line=dict(color=color, width=2.5, dash=dash), marker=dict(size=9, color=color),
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=bin_df["x"], y=bin_df[col], name=name, mode="lines+markers",
                legendgroup=name, showlegend=False,
                line=dict(color=color, width=2.5, dash=dash),
                marker=dict(
                    size=9,
                    color=np.where(low, CREAM, color),
                    line=dict(color=np.where(low, SAGE, color), width=2),
                ),
                customdata=custom,
                hovertemplate=(
                    f"<b>{label}: %{{customdata[0]}} {unit}</b><br>"
                    f"{name}: %{{y:.1f}} extra min"
                    "%{customdata[2]}<extra></extra>"
                ),
            ),
            row=1, col=1,
        )

    fig.add_trace(
        go.Bar(
            x=bin_df["x"], y=bin_df["n_obs"], name="Hours of data",
            marker=dict(color=np.where(low, CREAM, SAGE), line=dict(color=SAGE, width=1), cornerradius=3),
            customdata=custom, showlegend=False,
            hovertemplate=f"{label}: %{{customdata[0]}} {unit}<br>%{{y:,}} hours of data<extra></extra>",
        ),
        row=2, col=1,
    )

    fig.update_yaxes(title="Extra minutes", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title="Data", showticklabels=False, showgrid=False, row=2, col=1)
    fig.update_xaxes(title=f"{label} ({unit})", row=2, col=1)
    fig.update_layout(title=dict(text=f"<b>{label}</b>", x=0, font=dict(size=15)))
    return _style(fig, height=360)


# ---------------------------------------------------------------------------
# 3. Delay distribution by weather condition
# ---------------------------------------------------------------------------

def derive_weather_condition(df: pd.DataFrame) -> pd.Series:
    """One category per row: freezing precip > snow > rain > other precip > clear."""
    conditions = [
        df["is_freezing_precip"] == 1,
        df["is_snowing"] == 1,
        df["is_raining"] == 1,
        df["is_precipitating"] == 1,
    ]
    choices = ["Freezing precip", "Snow", "Rain", "Other precip"]
    return pd.Series(
        pd.Categorical(np.select(conditions, choices, default="Clear"), categories=CONDITION_ORDER, ordered=True),
        index=df.index,
        name="weather_condition",
    )


def compute_box_stats(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """Tukey box-plot stats per group.

    Plotting 1M+ raw points as a box plot is very slow in the browser, so the
    quartiles / whiskers are computed here and handed to plotly directly.
    Whiskers extend to the furthest point within 1.5 * IQR; outliers are not drawn.
    """
    sub = df.dropna(subset=[DELAY_COL])

    def _stats(s: pd.Series) -> pd.Series:
        q1, med, q3 = s.quantile([0.25, 0.5, 0.75])
        iqr = q3 - q1
        lo = s[s >= q1 - 1.5 * iqr].min()
        hi = s[s <= q3 + 1.5 * iqr].max()
        return pd.Series({
            "q1": q1, "median": med, "q3": q3, "lower": lo, "upper": hi,
            "mean": s.mean(), "n_obs": len(s),
        })

    stats = sub.groupby(group_cols, observed=True)[DELAY_COL].apply(_stats).unstack()
    return stats.reset_index()


def _box_trace(stats: pd.DataFrame, name: str, fill: str, show_legend: bool) -> go.Box:
    return go.Box(
        x=stats["weather_condition"].astype(str),
        q1=stats["q1"], median=stats["median"], q3=stats["q3"],
        lowerfence=stats["lower"], upperfence=stats["upper"],
        name=name, showlegend=show_legend,
        fillcolor=fill, line=dict(color=INK, width=1.5),
        boxpoints=False,
    )


def build_condition_box(stats: pd.DataFrame) -> go.Figure:
    fig = go.Figure(_box_trace(stats, "All stations", TAN, show_legend=False))
    _finish_box(fig, stats, "All stations")
    return fig


def build_condition_box_by_underground(stats: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for flag, name, fill in [(0, "Above ground", SAGE), (1, "Underground", BROWN)]:
        part = stats[stats["station_is_underground"] == flag]
        if not part.empty:
            fig.add_trace(_box_trace(part, name, fill, show_legend=True))
    fig.update_layout(boxmode="group")
    _finish_box(fig, stats, "Above ground vs underground")
    return fig


def _finish_box(fig: go.Figure, stats: pd.DataFrame, title: str) -> None:
    present = [c for c in CONDITION_ORDER if c in set(stats["weather_condition"].astype(str))]
    fig.update_xaxes(categoryorder="array", categoryarray=present)
    fig.update_yaxes(title="Extra minutes per trip")
    fig.update_layout(title=dict(text=f"<b>{title}</b>", x=0, font=dict(size=15)))
    _style(fig, height=420)
