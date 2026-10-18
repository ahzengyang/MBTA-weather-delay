"""
Station detail page stats and charts for the MBTA weather/delay dashboard.

Everything compares the station with its own usual behaviour, and with the
network as a whole. Delay averages are weighted by observation counts, as in
overview.py.
"""

import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from overview import RUSH_HOURS, _style
from weather_eda import BROWN, CONDITION_ORDER, INK, OLIVE, SAGE, TAN

DELAY = "excess_travel_min_mean"
WEIGHT = "n_travel_obs"
MIN_CONDITION_HOURS = 30  # station-hours needed before a weather category is trusted


def grouped_wmean(df: pd.DataFrame, by) -> pd.DataFrame:
    """Observation-weighted delay and number of station-hours per group (vectorised)."""
    sub = df[df[DELAY].notna() & (df[WEIGHT] > 0)]
    tmp = sub.assign(_dw=sub[DELAY] * sub[WEIGHT])
    g = tmp.groupby(by, observed=True)
    return pd.DataFrame({"delay": g["_dw"].sum() / g[WEIGHT].sum(), "hours": g.size()})


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def station_info(station_df: pd.DataFrame) -> dict:
    first = station_df.iloc[0]
    lines = station_df["ridership_lines"].dropna()
    line_list = [l.strip() for l in re.split(r"[,;/|]", lines.iloc[0]) if l.strip()] if len(lines) else []
    return {
        "underground": bool(first["station_is_underground"]),
        "municipality": first["station_municipality"],
        "lines": line_list,
        "url": first["station_stop_url"],
    }


def badges_html(info: dict) -> str:
    def badge(text, dark=False):
        bg, fg = (INK, "#f1ead8") if dark else (TAN, INK)
        return (f"<span style='display:inline-block;font-size:12.5px;padding:3px 10px;border-radius:99px;"
                f"margin:0 6px 4px 0;background:{bg};color:{fg}'>{text}</span>")

    parts = [badge("Underground" if info["underground"] else "Above ground", dark=True)]
    parts += [badge(l) for l in info["lines"]]
    if pd.notna(info["municipality"]):
        parts.append(badge(info["municipality"]))
    return "<div style='margin:-6px 0 6px'>" + "".join(parts) + "</div>"


def busiest_day(week_df: pd.DataFrame) -> str | None:
    daily = week_df.groupby("date_local")["hourly_entries"].sum(min_count=1).dropna()
    if daily.empty:
        return None
    day = daily.idxmax()
    return f"Busiest day: {pd.Timestamp(day):%a} ({daily.max() / 1000:,.1f}k)"


def station_rank(vs_usual: pd.DataFrame, station: str) -> tuple[int, int] | None:
    """(rank, number of ranked stations) by extra delay vs each station's own normal; 1 = worst."""
    ranked = vs_usual["diff"].dropna().sort_values(ascending=False)
    if station not in ranked.index:
        return None
    return int(ranked.index.get_loc(station)) + 1, len(ranked)


# ---------------------------------------------------------------------------
# Is this week unusual? -- every week for this station
# ---------------------------------------------------------------------------

def weekly_series(station_df: pd.DataFrame, data_start: pd.Timestamp) -> pd.DataFrame:
    out = grouped_wmean(station_df, "week_idx").reset_index()
    out["week_start"] = data_start + pd.to_timedelta(out["week_idx"] * 7, unit="D")
    return out


def build_weekly_chart(weeks: pd.DataFrame, selected_idx: int, network_typical: float) -> go.Figure:
    selected = (weeks["week_idx"] == selected_idx).to_numpy()
    fig = go.Figure(
        go.Bar(
            x=weeks["week_start"], y=weeks["delay"], name="Weekly extra trip time",
            marker=dict(color=np.where(selected, INK, SAGE), cornerradius=2),
            customdata=weeks["week_start"].dt.strftime("%b %-d %Y"),
            hovertemplate="Week of %{customdata}<br>%{y:.1f} extra min per trip<extra></extra>",
            showlegend=False,
        )
    )
    if selected.any():
        row = weeks[selected].iloc[0]
        fig.add_annotation(
            x=row["week_start"], y=row["delay"], text=f"<b>This week {row['delay']:.1f}</b>",
            showarrow=False, yshift=12, font=dict(color=INK, size=12),
        )
    if pd.notna(network_typical):
        fig.add_trace(go.Scatter(
            x=[weeks["week_start"].min(), weeks["week_start"].max()], y=[network_typical] * 2,
            mode="lines", name=f"Network typical week ({network_typical:.1f} min)",
            line=dict(color=INK, dash="dash", width=1.5), hoverinfo="skip",
        ))
    fig.update_yaxes(title="Extra min", rangemode="tozero")
    fig.update_xaxes(tickformat="%b", dtick="M2")
    fig.update_layout(bargap=0.15)
    return _style(fig, height=260)


# ---------------------------------------------------------------------------
# How does this station handle weather?
# ---------------------------------------------------------------------------

def condition_comparison(station_df: pd.DataFrame, network_by_condition: pd.DataFrame) -> pd.DataFrame:
    station = grouped_wmean(station_df, "weather_condition")
    out = pd.DataFrame(index=CONDITION_ORDER)
    out["station"] = station["delay"]
    out["station_hours"] = station["hours"]
    out["network"] = network_by_condition["delay"]
    out["station_hours"] = out["station_hours"].fillna(0).astype(int)
    return out.dropna(subset=["network"])


def weather_sentence(cmp: pd.DataFrame, station: str) -> str:
    trusted = cmp[cmp["station_hours"] >= MIN_CONDITION_HOURS]
    if "Clear" not in trusted.index:
        return "Not enough clear-weather data at this station to compare."
    for cond, icon, noun in [("Snow", "❄", "Snow"), ("Rain", "🌧", "Rain")]:
        if cond in trusted.index:
            here = trusted.loc[cond, "station"] - trusted.loc["Clear", "station"]
            net = cmp.loc[cond, "network"] - cmp.loc["Clear", "network"]
            if net > 0 and here > net * 1.15:
                verdict = "<b>more weather-sensitive</b> than most"
            elif net > 0 and here < net * 0.85:
                verdict = "<b>less weather-sensitive</b> than most"
            else:
                verdict = "about as weather-sensitive as most stations"
            return (f"{icon} {noun} adds <b>{here:+.1f} min</b> here vs <b>{net:+.1f} min</b> network-wide, "
                    f"so {station} is {verdict}.")
    return "Not enough rainy or snowy hours at this station to compare."


def build_condition_chart(cmp: pd.DataFrame, station: str) -> go.Figure:
    low = (cmp["station_hours"] < MIN_CONDITION_HOURS).to_numpy()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=cmp.index, y=cmp["station"], name=station,
        marker=dict(color=np.where(low, "rgba(45,47,34,0.25)", INK), cornerradius=4),
        customdata=np.column_stack([cmp["station_hours"], np.where(low, "<br><i>Few hours of data — less reliable</i>", "")]),
        hovertemplate="%{x}<br>%{y:.1f} extra min<br>%{customdata[0]:,} hours%{customdata[1]}<extra>" + station + "</extra>",
    ))
    fig.add_trace(go.Bar(
        x=cmp.index, y=cmp["network"], name="All stations",
        marker=dict(color=SAGE, cornerradius=4),
        hovertemplate="%{x}<br>%{y:.1f} extra min<extra>All stations</extra>",
    ))
    fig.update_yaxes(title="Extra min", rangemode="tozero")
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
    return _style(fig, height=300)


# ---------------------------------------------------------------------------
# When in the day? / Riders by hour
# ---------------------------------------------------------------------------

def _hour(df: pd.DataFrame) -> pd.Series:
    return df["hour_local"].str[11:13].astype(int)


def hourly_delay(week_df: pd.DataFrame, station_df: pd.DataFrame) -> pd.DataFrame:
    week = grouped_wmean(week_df.assign(hour=_hour(week_df)), "hour")["delay"].rename("this_week")
    usual = grouped_wmean(station_df.assign(hour=_hour(station_df)), "hour")["delay"].rename("usual")
    return pd.concat([week, usual], axis=1).sort_index().reset_index(names="hour")


def hourly_riders(week_df: pd.DataFrame) -> pd.DataFrame:
    riders = week_df.assign(hour=_hour(week_df)).groupby("hour")["hourly_entries"].mean()
    return riders.dropna().rename("riders").reset_index()


def _hour_labels(hours: pd.Series) -> list[str]:
    return [pd.Timestamp(2000, 1, 1, int(h)).strftime("%-I%p").lower() for h in hours]


def _hour_axis(fig: go.Figure, labels: list[str]) -> None:
    # half-width charts: label every 3rd hour so the labels stay horizontal
    fig.update_xaxes(categoryorder="array", categoryarray=labels,
                     tickmode="array", tickvals=labels[::3], tickangle=0)


def build_hourly_delay_chart(hourly: pd.DataFrame) -> go.Figure:
    labels = _hour_labels(hourly["hour"])
    rush = hourly["hour"].isin(RUSH_HOURS).to_numpy()
    fig = go.Figure()
    for is_rush, name, color in [(False, "Off-peak", SAGE), (True, "Rush hour", BROWN)]:
        mask = rush == is_rush
        fig.add_trace(go.Bar(
            x=np.array(labels)[mask], y=hourly.loc[mask, "this_week"], name=f"This week ({name.lower()})",
            marker=dict(color=color, cornerradius=3),
            hovertemplate="%{x}<br>This week: %{y:.1f} extra min<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=labels, y=hourly["usual"], name="Usual for this station", mode="lines",
        line=dict(color=INK, dash="dash", width=1.6, shape="spline"),
        hovertemplate="%{x}<br>Usual: %{y:.1f} extra min<extra></extra>",
    ))
    _hour_axis(fig, labels)
    fig.update_yaxes(title="Extra min")
    fig.update_layout(barmode="overlay", bargap=0.2)
    return _style(fig, height=280)


def build_riders_chart(riders: pd.DataFrame) -> go.Figure:
    labels = _hour_labels(riders["hour"])
    rush = riders["hour"].isin(RUSH_HOURS).to_numpy()
    fig = go.Figure()
    for is_rush, name, color in [(False, "Off-peak", TAN), (True, "Rush hour", OLIVE)]:
        mask = rush == is_rush
        fig.add_trace(go.Bar(
            x=np.array(labels)[mask], y=riders.loc[mask, "riders"], name=name,
            marker=dict(color=color, cornerradius=3),
            hovertemplate="%{x}<br>%{y:,.0f} riders per hour<extra></extra>",
        ))
    _hour_axis(fig, labels)
    fig.update_yaxes(title="Riders / hour", rangemode="tozero")
    fig.update_layout(barmode="overlay", bargap=0.2)
    return _style(fig, height=280)
