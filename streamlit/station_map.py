"""
Station map for the MBTA weather/delay dashboard.

Each station is colored by how this week's delay compares with that
station's usual delay, and sized by this week's ridership.

Built with pydeck (not plotly) because Streamlit can report which dot was
clicked on a pydeck map; plotly map clicks don't reach Streamlit.
"""

import numpy as np
import pandas as pd
import pydeck as pdk

from weather_eda import BROWN, CREAM, INK, OLIVE, SAGE

LAT_COL = "station_stop_lat"
LON_COL = "station_stop_lon"
STATION_COL = "station_stop_name"
LAYER_ID = "stations"

# minutes above the station's usual delay
WORSE_MIN = 1.0
SLIGHTLY_WORSE_MIN = 0.25

STATUSES = {
    # status: (legend label, fill color, outline color)
    "worse": ("Worse than usual", BROWN, CREAM),
    "slightly_worse": ("Slightly worse", OLIVE, CREAM),
    "normal": ("Normal or better", SAGE, CREAM),
    "no_data": ("No data this week", CREAM, OLIVE),
}


def _rgb(hex_color: str, alpha: int = 255) -> list[int]:
    h = hex_color.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [alpha]


def _fmt(v, spec):
    return format(v, spec) if pd.notna(v) else "–"


def build_station_map(df: pd.DataFrame, vs_usual: pd.DataFrame) -> pdk.Deck:
    """`vs_usual` is overview.station_vs_usual(): usual, this_week, diff, riders per station."""
    coords = df.drop_duplicates(STATION_COL).set_index(STATION_COL)[[LAT_COL, LON_COL]]
    plot_df = coords.join(vs_usual)

    plot_df["status"] = np.select(
        [plot_df["diff"].isna(), plot_df["diff"] >= WORSE_MIN, plot_df["diff"] >= SLIGHTLY_WORSE_MIN],
        ["no_data", "worse", "slightly_worse"],
        default="normal",
    )
    # area-proportional dot size in pixels; stations without ridership data get a mid size
    riders = plot_df["riders"]
    scale = np.sqrt(riders / riders.max()) if riders.notna().any() else riders
    plot_df["radius"] = (4 + 9 * scale).fillna(6)
    plot_df.loc[plot_df["status"] == "no_data", "radius"] = 5

    plot_df["fill"] = plot_df["status"].map(lambda s: _rgb(STATUSES[s][1]))
    plot_df["outline"] = plot_df["status"].map(lambda s: _rgb(STATUSES[s][2]))

    # pre-formatted strings for the hover card
    plot_df["station"] = plot_df.index
    plot_df["this_week_txt"] = plot_df["this_week"].map(lambda v: _fmt(v, ".1f"))
    plot_df["usual_txt"] = plot_df["usual"].map(lambda v: _fmt(v, ".1f"))
    plot_df["diff_txt"] = plot_df["diff"].map(lambda v: _fmt(v, "+.1f"))
    plot_df["riders_txt"] = plot_df["riders"].map(lambda v: f"{v:,.0f}" if pd.notna(v) else "no data")

    # draw the biggest dots first so small stations stay clickable on top
    plot_df = plot_df.sort_values("radius", ascending=False)
    cols = ["station", LAT_COL, LON_COL, "radius", "fill", "outline",
            "this_week_txt", "usual_txt", "diff_txt", "riders_txt"]

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=plot_df[cols].reset_index(drop=True),
        id=LAYER_ID,
        get_position=f"[{LON_COL}, {LAT_COL}]",
        get_radius="radius",
        radius_units="'pixels'",  # quoted: pydeck treats bare strings as expressions
        get_fill_color="fill",
        get_line_color="outline",
        stroked=True,
        line_width_units="'pixels'",
        get_line_width=1.5,
        pickable=True,
        auto_highlight=True,
        highlight_color=_rgb(INK, 90),
    )

    # TODO: could add subway line grey lines under the dots.
    # However needs GTFS shapes.txt which I would need to pull and join on
    # stations_data

    tooltip = {
        "html": (
            "<b>{station}</b><br/>"
            "This week: {this_week_txt} min extra<br/>"
            "Usual: {usual_txt} min · {diff_txt} min<br/>"
            "Riders: {riders_txt}<br/>"
            "<i>Click to open station page →</i>"
        ),
        "style": {
            "backgroundColor": CREAM, "color": INK, "border": f"1px solid {BROWN}",
            "borderRadius": "8px", "fontSize": "13px", "padding": "8px 10px",
        },
    }

    return pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(latitude=42.3601, longitude=-71.0589, zoom=10.5),
        map_provider="carto",
        map_style=pdk.map_styles.CARTO_LIGHT,
        tooltip=tooltip,
    )


def legend_html() -> str:
    items = ""
    for label, fill, outline in STATUSES.values():
        items += (
            f"<span style='display:inline-flex;align-items:center;gap:6px;margin-right:16px'>"
            f"<span style='width:12px;height:12px;border-radius:50%;background:{fill};"
            f"border:1.5px solid {outline if fill == CREAM else fill}'></span>{label}</span>"
        )
    return f"<div style='font-size:13px;color:{INK};margin:-4px 0 8px'>{items}</div>"


def clicked_station(selection: dict) -> str | None:
    """Station name from a st.pydeck_chart selection event, if a dot was clicked."""
    objects = selection.get("objects", {}).get(LAYER_ID, [])
    return objects[0].get("station") if objects else None
