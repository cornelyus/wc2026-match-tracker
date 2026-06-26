import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
API_BASE     = "https://v3.football.api-sports.io"
WC_LEAGUE_ID = 1
WC_SEASON    = 2026

ROUND_ORDER = [
    "Group Stage - 1", "Group Stage - 2", "Group Stage - 3",
    "Round of 32", "Round of 16", "Quarter-finals",
    "Semi-finals", "3rd Place Final", "Final",
]

# ── Algorithm ─────────────────────────────────────────────────────────────────
def calculate_excitement(home_goals, away_goals, home_xg, away_xg, total_sot):
    """
    Returns (score 0–10, components dict).
    xG is the primary quality signal: a high-xG 0-0 outranks a low-xG 3-0.
    """
    has_xg      = home_xg is not None and away_xg is not None
    total_xg    = (home_xg or 0) + (away_xg or 0)
    xg_diff     = abs((home_xg or 0) - (away_xg or 0))
    margin      = abs(home_goals - away_goals)
    total_goals = home_goals + away_goals

    base       = 6.0
    xg_bonus   = min((total_xg - 1.5) * 0.35, 1.4)  if has_xg             else 0.0
    xg_penalty = xg_diff * 0.18                       if has_xg             else 0.0
    goal_bonus = min(total_goals * 0.15, 0.6)
    margin_pen = max(0.0, (margin - 1) * 0.3)
    sot_bonus  = max(0.0, (total_sot - 8) * 0.05)    if total_sot is not None else 0.0

    score = max(0.0, min(10.0, round(
        base + xg_bonus - xg_penalty + goal_bonus - margin_pen + sot_bonus, 2
    )))

    return score, {
        "base":        base,
        "xg_bonus":    round(xg_bonus,   2),
        "xg_penalty":  round(xg_penalty, 2),
        "goal_bonus":  round(goal_bonus, 2),
        "margin_pen":  round(margin_pen, 2),
        "sot_bonus":   round(sot_bonus,  2),
        "home_xg":     home_xg,
        "away_xg":     away_xg,
        "total_xg":    round(total_xg, 2) if has_xg else None,
        "xg_diff":     round(xg_diff,  2) if has_xg else None,
        "total_goals": total_goals,
        "margin":      margin,
        "total_sot":   total_sot,
    }

def get_verdict(score):
    if score >= 8.0: return "🔥 Classic"
    if score >= 7.0: return "⚡ Exciting"
    if score >= 6.0: return "⚖️ Decent"
    return "😴 Skip"

# ── API ───────────────────────────────────────────────────────────────────────
def _headers(api_key):
    return {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key":  api_key,
    }

@st.cache_data(ttl=300)
def fetch_fixtures(api_key):
    try:
        r = requests.get(
            f"{API_BASE}/fixtures",
            headers=_headers(api_key),
            params={"league": WC_LEAGUE_ID, "season": WC_SEASON},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            return None, str(data["errors"])
        return data.get("response", []), None
    except Exception as e:
        return None, str(e)

@st.cache_data(ttl=86400)
def fetch_match_stats(fixture_id, api_key):
    try:
        r = requests.get(
            f"{API_BASE}/fixtures/statistics",
            headers=_headers(api_key),
            params={"fixture": fixture_id},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("response", [])
    except:
        return []

def _stat(team_data, *names):
    """Extract a numeric stat from a team's statistics list by name."""
    for s in team_data.get("statistics", []):
        if s["type"].lower() in {n.lower() for n in names}:
            v = s["value"]
            if v is None:
                return None
            try:
                return float(str(v).replace("%", "").strip())
            except:
                return None
    return None

# ── Data builder ──────────────────────────────────────────────────────────────
def build_rows(raw_fixtures, api_key):
    rows = []
    components_store = {}  # fixture_id -> components dict

    for f in raw_fixtures:
        fix    = f["fixture"]
        league = f["league"]
        teams  = f["teams"]
        goals  = f["goals"]
        status = fix["status"]["short"]

        fixture_id  = fix["id"]
        is_finished = status in ("FT", "AET", "PEN")
        is_live     = status in ("1H", "HT", "2H", "ET", "BT", "P", "LIVE")

        home_goals = goals.get("home") or 0
        away_goals = goals.get("away") or 0

        try:
            dt = datetime.fromisoformat(fix["date"].replace("Z", "+00:00"))
            date_str = dt.strftime("%b %d, %H:%M")
        except Exception:
            dt = datetime.min.replace(tzinfo=timezone.utc)
            date_str = ""

        if is_finished:
            score_display = f"{home_goals} – {away_goals}"
        elif is_live:
            elapsed = fix["status"].get("elapsed") or ""
            score_display = f"🔴 {home_goals}–{away_goals}" + (f" {elapsed}'" if elapsed else "")
        else:
            score_display = "vs"

        row = {
            "_id":      fixture_id,
            "_dt":      dt,
            "_state":   "post" if is_finished else ("live" if is_live else "pre"),
            "_home_id": teams["home"]["id"],
            "Round":    league.get("round", ""),
            "Date":     date_str,
            "Home":     teams["home"]["name"],
            "Score":    score_display,
            "Away":     teams["away"]["name"],
            "xG":       "—",
            "SOT":      None,
            "Excitement": None,
            "Verdict":  "—",
        }

        if is_finished:
            stats = fetch_match_stats(fixture_id, api_key)
            if len(stats) >= 2:
                home_id   = teams["home"]["id"]
                home_data = next((s for s in stats if s["team"]["id"] == home_id), stats[0])
                away_data = next((s for s in stats if s["team"]["id"] != home_id), stats[1])

                home_xg  = _stat(home_data, "expected_goals", "Expected Goals")
                away_xg  = _stat(away_data, "expected_goals", "Expected Goals")
                home_sot = _stat(home_data, "shots on goal", "Shots on Goal", "Shots on Target")
                away_sot = _stat(away_data, "shots on goal", "Shots on Goal", "Shots on Target")

                total_sot = (
                    int(home_sot or 0) + int(away_sot or 0)
                    if home_sot is not None or away_sot is not None
                    else None
                )

                score, comps = calculate_excitement(
                    home_goals, away_goals, home_xg, away_xg, total_sot
                )
                components_store[fixture_id] = comps

                xg_display = (
                    f"{home_xg:.2f} – {away_xg:.2f}"
                    if home_xg is not None and away_xg is not None
                    else "N/A"
                )

                row.update({
                    "xG":         xg_display,
                    "SOT":        total_sot,
                    "Excitement": score,
                    "Verdict":    get_verdict(score),
                })

        rows.append(row)

    return rows, components_store

# ── App ───────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="WC 2026 Excitement Tracker",
    page_icon="🏆",
    layout="wide",
)

st.title("🏆 World Cup 2026 — Match Excitement Tracker")
st.markdown(
    "Data-driven ranking of all WC2026 matches by excitement. "
    "Goals matter, but xG, competitive balance, and shot activity all count. "
    "**Click any row** to see the full score breakdown."
)

# ── API key gate ───────────────────────────────────────────────────────────────
api_key = st.secrets.get("API_FOOTBALL_KEY", "")
if not api_key:
    st.error("**API key not configured.**")
    st.markdown(
        "Add your free [API-Football](https://rapidapi.com/api-sports/api/api-football) "
        "key to `.streamlit/secrets.toml`:"
    )
    st.code('API_FOOTBALL_KEY = "your_key_here"', language="toml")
    st.stop()

# ── Load data ─────────────────────────────────────────────────────────────────
with st.spinner("Loading fixture list…"):
    raw_fixtures, err = fetch_fixtures(api_key)

if err or not raw_fixtures:
    st.error(f"Could not load fixtures: {err or 'empty response'}")
    st.stop()

finished_count = sum(
    1 for f in raw_fixtures
    if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")
)

with st.spinner(
    f"Loading statistics for {finished_count} finished matches "
    "(first load may take a moment — results are cached for 24h)…"
):
    rows, components_store = build_rows(raw_fixtures, api_key)

df = pd.DataFrame(rows)

# ── Sort: finished by Excitement desc → live → upcoming by date ───────────────
post_df   = df[df["_state"] == "post"].sort_values("Excitement", ascending=False)
live_df   = df[df["_state"] == "live"].sort_values("_dt")
pre_df    = df[df["_state"] == "pre"].sort_values("_dt")
sorted_df = pd.concat([post_df, live_df, pre_df], ignore_index=True)

# ── Filters ───────────────────────────────────────────────────────────────────
fc1, fc2, fc3 = st.columns([2, 2, 3])
with fc1:
    status_filter = st.radio("Status", ["All", "Finished", "Upcoming"], horizontal=True)
with fc2:
    all_rounds = sorted_df["Round"].dropna().unique().tolist()
    rounds_sorted = sorted(
        all_rounds,
        key=lambda r: ROUND_ORDER.index(r) if r in ROUND_ORDER else 99,
    )
    round_filter = st.selectbox("Round", ["All Rounds"] + rounds_sorted)
with fc3:
    team_search = st.text_input("", placeholder="Search team name…")

view = sorted_df.copy()
if status_filter == "Finished":
    view = view[view["_state"] == "post"]
elif status_filter == "Upcoming":
    view = view[view["_state"] != "post"]
if round_filter != "All Rounds":
    view = view[view["Round"] == round_filter]
if team_search.strip():
    t = team_search.strip().lower()
    view = view[
        view["Home"].str.lower().str.contains(t, na=False)
        | view["Away"].str.lower().str.contains(t, na=False)
    ]

view = view.reset_index(drop=True)

# ── Table ─────────────────────────────────────────────────────────────────────
DISPLAY_COLS = ["Round", "Date", "Home", "Score", "Away", "xG", "SOT", "Excitement", "Verdict"]

event = st.dataframe(
    view[DISPLAY_COLS],
    selection_mode="single-row",
    on_select="rerun",
    column_config={
        "Excitement": st.column_config.NumberColumn("Excitement ⭐", format="%.1f"),
        "SOT":        st.column_config.NumberColumn("SOT"),
    },
    hide_index=True,
    use_container_width=True,
    height=520,
    key="match_table",
)

st.caption(
    f"Showing {len(view)} of {len(df)} matches · "
    "Data: API-Football · Fixture list refreshes every 5 min · Match stats cached 24 h"
)

# ── Detail panel ──────────────────────────────────────────────────────────────
if event.selection.rows:
    sel   = view.iloc[event.selection.rows[0]]
    state = sel["_state"]
    fid   = sel["_id"]

    st.divider()

    if state == "pre":
        st.info(
            f"**{sel['Home']} vs {sel['Away']}** · {sel['Round']} · {sel['Date']}\n\n"
            "Match hasn't been played yet — check back after kick-off."
        )
    elif state == "live":
        st.warning(
            f"**{sel['Home']} vs {sel['Away']}** is currently in progress. "
            "Excitement score will be available after the final whistle."
        )
    else:
        comps   = components_store.get(fid, {})
        score   = sel["Excitement"]
        verdict = sel["Verdict"]

        d1, d2 = st.columns([1, 1.5])

        with d1:
            st.markdown(f"### {sel['Home']} vs {sel['Away']}")
            st.caption(f"{sel['Round']} · {sel['Date']}")
            st.markdown(f"**Final score:** {sel['Score']}")

            if score is not None:
                st.metric("Match Excitement Index Score", f"{score} / 10")
                if score >= 8.0:
                    st.success(f"{verdict} — End-to-end cinema.")
                elif score >= 7.0:
                    st.info(f"{verdict} — Balanced, tense, and entertaining.")
                elif score >= 6.0:
                    st.warning(f"{verdict} — Decent, but not unmissable.")
                else:
                    st.error(f"{verdict} — One-sided or low-event. Skip the replay.")
            else:
                st.info("Statistics not available for this match.")

        with d2:
            if comps:
                st.markdown("**Algorithm Score Adjustment Details:**")

                home_xg  = comps.get("home_xg")
                away_xg  = comps.get("away_xg")
                total_xg = comps.get("total_xg")

                lines = [f"- **Base Score:** {comps['base']:.2f}"]

                if home_xg is not None:
                    lines.append(
                        f"- **xG Volume (total xG: {total_xg:.2f}):** "
                        f"+{comps['xg_bonus']:.2f} pts"
                    )
                    lines.append(
                        f"- **xG Balance Penalty (|{home_xg:.2f} – {away_xg:.2f}|):** "
                        f"-{comps['xg_penalty']:.2f} pts"
                    )
                else:
                    lines.append("- **xG:** Not available for this match")

                lines.append(
                    f"- **Goal Excitement ({comps['total_goals']} goals):** "
                    f"+{comps['goal_bonus']:.2f} pts"
                )

                m = comps["margin"]
                if comps["margin_pen"] > 0:
                    lines.append(
                        f"- **Blowout Penalty (margin {m}):** "
                        f"-{comps['margin_pen']:.2f} pts"
                    )
                else:
                    lines.append(f"- **Blowout Penalty (margin {m}):** none")

                sot = comps.get("total_sot")
                if sot is not None:
                    lines.append(
                        f"- **Shot Action Bonus ({sot} shots on target):** "
                        f"+{comps['sot_bonus']:.2f} pts"
                    )
                else:
                    lines.append("- **Shot Action Bonus:** N/A")

                st.markdown("\n".join(lines))
