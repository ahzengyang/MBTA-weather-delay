"""
Weekly overview stats and charts for the MBTA weather/delay dashboard main page.

Delay averages are weighted by the number of observations behind each
station-hour, so a quiet 3am hour doesn't count as much as rush hour.
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from weather_eda import BROWN, CREAM, GRID, INK, OLIVE, SAGE, TAN

RUSH_HOURS = {7, 8, 9, 16, 17, 18}
DAY_COLORS = {"Clear": SAGE, "Rain": OLIVE, "Snow": BROWN}


def wmean(values: pd.Series, weights: pd.Series) -> float:
    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return float("nan")
    return float(np.average(values[mask], weights=weights[mask]))


def c_to_f(c):
    return c * 9 / 5 + 32


# ---------------------------------------------------------------------------
# Headline tiles
# ---------------------------------------------------------------------------

def week_metrics(df: pd.DataFrame) -> dict:
    return {
        "riders": float(df["hourly_entries"].sum()),
        "extra_trip": wmean(df["excess_travel_min_mean"], df["n_travel_obs"]),
        "extra_wait": wmean(df["excess_headway_min_median"], df["n_headway_obs"]),
        "bad_trip": wmean(df["excess_travel_min_p90"], df["n_travel_obs"]),
    }


def typical_week_metrics(df: pd.DataFrame) -> dict:
    """Median of each metric across every week in the data ("a typical week")."""
    per_week = pd.DataFrame([week_metrics(g) for _, g in df.groupby("week_idx")])
    return per_week.median().to_dict()


def ridership_coverage(df: pd.DataFrame) -> tuple[int, int]:
    """(stations with fare-gate data this week, stations in view)."""
    with_data = df.loc[df["hourly_entries"].notna(), "station_stop_name"].nunique()
    return int(with_data), int(df["station_stop_name"].nunique())


# ---------------------------------------------------------------------------
# Weather strip
# ---------------------------------------------------------------------------

def hourly_weather(df: pd.DataFrame) -> pd.DataFrame:
    """Average weather across stations for each hour (weather is near-identical across stations)."""
    return df.groupby("hour_utc").agg(
        date_local=("date_local", "first"),
        temp_c=("temperature_2m", "mean"),
        rain=("rain", "mean"),
        snowfall=("snowfall", "mean"),
        gust=("wind_gusts_10m", "max"),
        raining=("is_raining", "mean"),
        snowing=("is_snowing", "mean"),
    )


def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    """One row per local day: delay, temperature range and the day's weather type."""
    hw = hourly_weather(df)
    days = hw.groupby("date_local").agg(
        high_c=("temp_c", "max"),
        low_c=("temp_c", "min"),
        snow_hours=("snowing", lambda s: int((s >= 0.5).sum())),
        rain_hours=("raining", lambda s: int((s >= 0.5).sum())),
    )
    days["condition"] = np.select(
        [days["snow_hours"] > 0, days["rain_hours"] > 0], ["Snow", "Rain"], default="Clear"
    )
    days["delay"] = df.groupby("date_local").apply(
        lambda g: wmean(g["excess_travel_min_mean"], g["n_travel_obs"]), include_groups=False
    )
    days["high_f"] = c_to_f(days["high_c"])
    days["low_f"] = c_to_f(days["low_c"])
    return days.reset_index()


def weather_summary(df: pd.DataFrame) -> dict:
    hw = hourly_weather(df)
    days = daily_summary(df)
    snow_days = days[days["condition"] == "Snow"]
    rain_days = days[days["condition"] == "Rain"]
    clear_days = days[days["condition"] == "Clear"]

    if not clear_days.empty and not snow_days.empty:
        n = len(snow_days)
        insight = (
            f"On the <b>{n} snowy day{'s' if n > 1 else ''}</b>, trips ran "
            f"<b>{snow_days['delay'].mean():.1f} min</b> late, compared with "
            f"<b>{clear_days['delay'].mean():.1f} min</b> on clear days."
        )
    elif not clear_days.empty and not rain_days.empty:
        n = len(rain_days)
        insight = (
            f"On the <b>{n} rainy day{'s' if n > 1 else ''}</b>, trips ran "
            f"<b>{rain_days['delay'].mean():.1f} min</b> late, compared with "
            f"<b>{clear_days['delay'].mean():.1f} min</b> on clear days."
        )
    elif clear_days.empty:
        insight = "Rain or snow fell every day this week."
    else:
        insight = "No rain or snow this week."

    return {
        "avg_high_f": c_to_f(days["high_c"].mean()),
        "avg_low_f": c_to_f(days["low_c"].mean()),
        "rain_mm": float(hw["rain"].sum()),
        "rain_hours": int((hw["raining"] >= 0.5).sum()),
        "snow_cm": float(hw["snowfall"].sum()),
        "snow_days": len(snow_days),
        "max_gust": float(hw["gust"].max()),
        "insight": insight,
    }


def weather_strip_html(w: dict) -> str:
    def cell(icon, label, value):
        return (
            f"<div><div style='font-size:13px;opacity:.75'>{icon} {label}</div>"
            f"<div style='font-size:20px;font-weight:600'>{value}</div></div>"
        )

    cells = "".join([
        cell("🌡", "Avg high / low", f"{w['avg_high_f']:.0f}° / {w['avg_low_f']:.0f}°F"),
        cell("🌧", "Rain", f"{w['rain_mm']:.0f} mm · {w['rain_hours']} hrs"),
        cell("❄", "Snow", f"{w['snow_cm']:.0f} cm · {w['snow_days']} day{'s' if w['snow_days'] != 1 else ''}"),
        cell("💨", "Strongest gust", f"{w['max_gust']:.0f} km/h"),
    ])
    return (
        f"<div style='background:{SAGE};color:{INK};border-radius:12px;padding:16px 20px;margin-bottom:8px'>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:14px 20px'>{cells}</div>"
        f"<div style='margin-top:12px;padding-top:12px;border-top:1px solid rgba(45,47,34,.2);font-size:15px'>"
        f"{w['insight']}</div></div>"
    )


# ---------------------------------------------------------------------------
# Worst stations
# ---------------------------------------------------------------------------

def station_vs_usual(week_df: pd.DataFrame, all_df: pd.DataFrame) -> pd.DataFrame:
    """Per station: this week's delay, its usual delay, the difference and riders."""
    def _agg(df):
        return df.groupby("station_stop_name").apply(
            lambda g: wmean(g["excess_travel_min_mean"], g["n_travel_obs"]), include_groups=False
        )

    out = pd.DataFrame({"usual": _agg(all_df)})
    out["this_week"] = _agg(week_df)
    out["diff"] = out["this_week"] - out["usual"]
    out["riders"] = week_df.groupby("station_stop_name")["hourly_entries"].sum(min_count=1)
    return out


def worst_stations_html(vs_usual: pd.DataFrame, n: int = 5) -> str:
    # only stations that were noticeably worse (rounds to at least +0.1 min)
    worst = vs_usual[vs_usual["diff"] >= 0.05].sort_values("diff", ascending=False).head(n)
    if worst.empty:
        return f"<div style='color:{INK};font-size:14.5px'>No station ran noticeably worse than usual this week.</div>"
    rows = ""
    for i, (name, r) in enumerate(worst.iterrows(), start=1):
        color = "#8b4a2b" if r["diff"] > 0 else "#556b3a"
        # explicit borders/background override Streamlit's default markdown table styling
        border = "border:none;" + ("" if i == len(worst) else f"border-bottom:1px solid {TAN};")
        cell = f"padding:9px 4px;background:transparent;{border}"
        rows += (
            f"<tr style='background:transparent;border:none'><td style='{cell}'>{i}. {name}</td>"
            f"<td style='{cell}text-align:right;font-weight:600;color:{color}'>"
            f"{r['diff']:+.1f} min</td></tr>"
        )
    return (
        f"<div style='background:#f7f2e5;border:1px solid {TAN};border-radius:12px;padding:8px 16px'>"
        f"<table style='width:100%;border-collapse:collapse;border:none;margin:0;font-size:14.5px;color:{INK}'>{rows}</table></div>"
    )


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _style(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK, size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hoverlabel=dict(align="left", bgcolor=CREAM, bordercolor=BROWN, font=dict(color=INK)),
        bargap=0.35,
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=TAN)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=TAN)
    return fig


def build_day_by_day(days: pd.DataFrame, typical_day: float) -> go.Figure:
    icons = {"Snow": " ❄", "Rain": " 🌧", "Clear": ""}
    x = [pd.Timestamp(d).strftime("%a %-d") + icons[c] for d, c in zip(days["date_local"], days["condition"])]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.08)

    for cond in ["Clear", "Rain", "Snow"]:
        mask = (days["condition"] == cond).to_numpy()
        if not mask.any():
            continue
        fig.add_trace(
            go.Bar(
                x=np.array(x)[mask], y=days.loc[mask, "delay"], name=cond,
                marker=dict(color=DAY_COLORS[cond], cornerradius=4),
                text=[f"{v:.1f}" for v in days.loc[mask, "delay"]], textposition="outside",
                textfont=dict(color=INK), cliponaxis=False,
                hovertemplate="%{x}<br>%{y:.1f} extra min per trip<extra></extra>",
            ),
            row=1, col=1,
        )
    if pd.notna(typical_day):
        # drawn as a line trace (not an annotation) so its label sits in the legend
        fig.add_trace(
            go.Scatter(
                x=[x[0], x[-1]], y=[typical_day, typical_day], mode="lines",
                name=f"Typical day ({typical_day:.1f} min)",
                line=dict(color=INK, dash="dash", width=1.5), hoverinfo="skip",
            ),
            row=1, col=1,
        )

    fig.add_trace(
        go.Bar(
            x=x, y=days["high_f"] - days["low_f"], base=days["low_f"], name="Temperature range",
            marker=dict(color=TAN, line=dict(color=BROWN, width=1), cornerradius=6),
            width=0.25, showlegend=False,
            text=[f"{h:.0f}° / {l:.0f}°" for h, l in zip(days["high_f"], days["low_f"])],
            textposition="outside", textfont=dict(color=INK, size=11), cliponaxis=False,
            customdata=np.column_stack([days["high_f"], days["low_f"]]),
            hovertemplate="%{x}<br>High %{customdata[0]:.0f}°F · Low %{customdata[1]:.0f}°F<extra></extra>",
        ),
        row=2, col=1,
    )
    # categories must keep the original day order across the traces
    fig.update_xaxes(categoryorder="array", categoryarray=x)
    fig.update_yaxes(title="Extra min", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title="°F", row=2, col=1)
    fig.update_layout(barmode="overlay")
    return _style(fig, height=430)


def hourly_profile(df: pd.DataFrame) -> pd.DataFrame:
    hours = df["hour_local"].str[11:13].astype(int)
    out = df.assign(hour=hours).groupby("hour").apply(
        lambda g: wmean(g["excess_travel_min_mean"], g["n_travel_obs"]), include_groups=False
    )
    return out.rename("delay").reset_index()


def build_rush_hour(profile: pd.DataFrame) -> go.Figure:
    labels = [pd.Timestamp(2000, 1, 1, h).strftime("%-I%p").lower() for h in profile["hour"]]
    rush = profile["hour"].isin(RUSH_HOURS).to_numpy()
    fig = go.Figure()
    for is_rush, name, color in [(False, "Off-peak", SAGE), (True, "Rush hour", BROWN)]:
        mask = rush == is_rush
        fig.add_trace(go.Bar(
            x=np.array(labels)[mask], y=profile.loc[mask, "delay"], name=name,
            marker=dict(color=color, cornerradius=3),
            hovertemplate="%{x}<br>%{y:.1f} extra min per trip<extra></extra>",
        ))
    fig.update_xaxes(categoryorder="array", categoryarray=labels)
    fig.update_yaxes(title="Extra min")
    fig.update_layout(barmode="overlay", bargap=0.2)
    return _style(fig, height=260)
