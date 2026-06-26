import streamlit as st
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "fifa.world/scoreboard?limit=200&dates=20260611-20260719"
)
ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "fifa.world/summary?event={}"
)

ROUND_ORDER = [
    "Group A", "Group B", "Group C", "Group D", "Group E", "Group F",
    "Group G", "Group H", "Group I", "Group J", "Group K", "Group L",
    "Round of 32", "Round of 16", "Quarterfinals",
    "Semifinals", "3rd Place Final", "Final",
]

# ── Algorithm ─────────────────────────────────────────────────────────────────
def calculate_excitement(home_goals, away_goals,
                         home_sot=None, away_sot=None,
                         home_shots=None, away_shots=None):
    margin      = abs(home_goals - away_goals)
    total_goals = home_goals + away_goals
    has_sot     = home_sot is not None and away_sot is not None
    has_shots   = home_shots is not None and away_shots is not None

    base        = 5.5
    goal_bonus  = min(total_goals * 0.45, 2.5)
    close_bonus = 0.4 if margin == 0 else (0.2 if margin == 1 else 0.0)
    margin_pen  = max(0.0, (margin - 2) * 0.55)

    # SOT bonus: every SOT above 6 adds 0.1 pt, capped at 1.0
    if has_sot:
        total_sot = home_sot + away_sot
        sot_bonus = round(min(max(0.0, (total_sot - 6) * 0.1), 1.0), 2)
    else:
        total_sot = None
        sot_bonus = 0.0

    # Domination penalty: one-sided shot share reduces score by up to 0.5
    if has_shots:
        total_shots = home_shots + away_shots
        dom_ratio   = abs(home_shots - away_shots) / total_shots if total_shots else 0.0
        dom_pen     = round(dom_ratio * 0.5, 2)
    else:
        total_shots = None
        dom_pen     = 0.0

    score = max(0.0, min(10.0, round(
        base + goal_bonus + close_bonus + sot_bonus - dom_pen - margin_pen, 2
    )))

    return score, {
        "base":        base,
        "goal_bonus":  round(goal_bonus,  2),
        "close_bonus": round(close_bonus, 2),
        "margin_pen":  round(margin_pen,  2),
        "sot_bonus":   sot_bonus,
        "dom_pen":     dom_pen,
        "total_goals": total_goals,
        "margin":      margin,
        "total_sot":   total_sot,
        "home_sot":    home_sot,
        "away_sot":    away_sot,
        "total_shots": total_shots,
        "home_shots":  home_shots,
        "away_shots":  away_shots,
        "has_sot":     has_sot,
        "has_shots":   has_shots,
    }

def get_verdict(score):
    if score >= 7.8: return "🔥 Classic"
    if score >= 7.0: return "⚡ Exciting"
    if score >= 6.0: return "⚖️ Decent"
    return "😴 Skip"

# ── Data fetching ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def fetch_all_matches():
    try:
        r = requests.get(ESPN_SCOREBOARD, timeout=15)
        r.raise_for_status()
        return r.json().get("events", []), None
    except Exception as e:
        return None, str(e)

def _fetch_one_summary(event_id):
    try:
        r = requests.get(ESPN_SUMMARY.format(event_id), timeout=15)
        r.raise_for_status()
        # Key stats by team displayName — home/away resolved in parse_events
        by_team = {}
        for team_data in r.json().get("boxscore", {}).get("teams", []):
            name = team_data.get("team", {}).get("displayName", "")
            if not name:
                continue
            team_stats = {}
            for s in team_data.get("statistics", []):
                stat_name = s.get("name")
                val       = s.get("displayValue")
                if val is None:
                    continue
                if stat_name == "shotsOnTarget":
                    team_stats["sot"] = int(val)
                elif stat_name == "totalShots":
                    team_stats["shots"] = int(val)
            by_team[name] = team_stats
        return event_id, by_team
    except Exception:
        return event_id, {}

@st.cache_data(ttl=3600)
def fetch_match_summaries(event_ids):
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_summary, eid): eid for eid in event_ids}
        for f in as_completed(futures):
            eid, stats = f.result()
            results[eid] = stats
    return results

def parse_events(events, summaries):
    rows = []
    components_store = {}

    for evt in events:
        comp        = (evt.get("competitions") or [{}])[0]
        status_type = comp.get("status", {}).get("type", {})
        state       = status_type.get("state", "pre")
        competitors = comp.get("competitors", [])

        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        home_goals = int(home.get("score") or 0)
        away_goals = int(away.get("score") or 0)

        try:
            dt = datetime.fromisoformat(evt["date"].replace("Z", "+00:00"))
            formatted_date = dt.strftime("%b %d, %H:%M")
        except Exception:
            dt = datetime.min.replace(tzinfo=timezone.utc)
            formatted_date = ""

        round_str = ""
        for note in comp.get("notes", []):
            text = note.get("headline", "")
            if text:
                round_str = text.replace("FIFA World Cup, ", "").strip()
                break

        if state == "post":
            score_display = f"{home_goals} – {away_goals}"
        elif state == "in":
            clock = comp.get("status", {}).get("displayClock", "")
            score_display = f"🔴 {home_goals}–{away_goals}" + (f" {clock}" if clock else "")
        else:
            score_display = "vs"

        row = {
            "_id":    evt.get("id"),
            "_dt":    dt,
            "_state": state,
            "Round":  round_str,
            "Date":   formatted_date,
            "Home":   home.get("team", {}).get("displayName", ""),
            "Score":  score_display,
            "Away":   away.get("team", {}).get("displayName", ""),
            "Excitement": None,
            "Verdict": "—",
        }

        if state == "post":
            mid        = evt.get("id")
            by_team    = summaries.get(mid, {})
            home_name  = row["Home"]
            away_name  = row["Away"]
            home_extra = by_team.get(home_name, {})
            away_extra = by_team.get(away_name, {})
            score, comps = calculate_excitement(
                home_goals, away_goals,
                home_sot=home_extra.get("sot"),
                away_sot=away_extra.get("sot"),
                home_shots=home_extra.get("shots"),
                away_shots=away_extra.get("shots"),
            )
            components_store[mid] = comps
            row.update({"Excitement": score, "Verdict": get_verdict(score)})

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
    "Data-driven ranking of all WC2026 matches by excitement — "
    "goals, score margin, and shot activity combined. "
    "**Click any row** to see the full score breakdown."
)

# ── Load fixtures ─────────────────────────────────────────────────────────────
with st.spinner("Loading World Cup 2026 fixtures…"):
    events, err = fetch_all_matches()

if err or not events:
    st.error(f"Could not load fixtures: {err or 'empty response from ESPN API'}")
    st.stop()

# ── Fetch shot stats for finished matches (parallel, cached 1 hr) ─────────────
finished_ids = tuple(
    e["id"] for e in events
    if (e.get("competitions") or [{}])[0]
    .get("status", {}).get("type", {}).get("state") == "post"
)
with st.spinner(f"Loading shot stats for {len(finished_ids)} finished games…"):
    summaries = fetch_match_summaries(finished_ids) if finished_ids else {}

rows, components_store = parse_events(events, summaries)
df = pd.DataFrame(rows)

# ── Sort: finished by Excitement desc → live → upcoming by date ───────────────
post_df   = df[df["_state"] == "post"].sort_values("Excitement", ascending=False)
live_df   = df[df["_state"] == "in"].sort_values("_dt")
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

# ── Table + Detail panel ──────────────────────────────────────────────────────
DISPLAY_COLS = ["Round", "Date", "Home", "Score", "Away", "Excitement", "Verdict"]

col_table, col_detail = st.columns([3, 2])

with col_table:
    sel_event = st.dataframe(
        view[DISPLAY_COLS],
        selection_mode="single-row",
        on_select="rerun",
        column_config={
            "Excitement": st.column_config.NumberColumn("Excitement ⭐", format="%.1f"),
        },
        hide_index=True,
        use_container_width=True,
        height=560,
        key="match_table",
    )
    has_shot_data = any(bool(v) for v in summaries.values())
    stats_note = " + Shot Stats" if has_shot_data else ""
    st.caption(
        f"Showing {len(view)} of {len(df)} matches · "
        f"Data: ESPN{stats_note} · Refreshes every 5 min"
    )

with col_detail:
    if not sel_event.selection.rows:
        st.info("Select a match from the table to see the full excitement breakdown.")
    else:
        sel   = view.iloc[sel_event.selection.rows[0]]
        state = sel["_state"]
        mid   = sel["_id"]

        if state == "pre":
            st.info(
                f"**{sel['Home']} vs {sel['Away']}** · {sel['Round']} · {sel['Date']}\n\n"
                "Match hasn't been played yet — check back after kick-off."
            )
        elif state == "in":
            st.warning(
                f"**{sel['Home']} vs {sel['Away']}** is currently in progress. "
                "Excitement score will be available after the final whistle."
            )
        else:
            comps   = components_store.get(mid, {})
            score   = sel["Excitement"]
            verdict = sel["Verdict"]

            st.markdown(f"### {sel['Home']} vs {sel['Away']}")
            st.caption(f"{sel['Round']} · {sel['Date']}")
            st.markdown(f"**Final score:** {sel['Score']}")
            st.divider()

            if score is not None:
                st.metric("Match Excitement Index Score", f"{score} / 10")
                if score >= 7.8:
                    st.success(f"{verdict} — End-to-end cinema.")
                elif score >= 7.0:
                    st.info(f"{verdict} — Balanced, tense, and entertaining.")
                elif score >= 6.0:
                    st.warning(f"{verdict} — Decent, but not unmissable.")
                else:
                    st.error(f"{verdict} — One-sided or low-event. Skip the replay.")

            if comps:
                st.markdown("**Algorithm Score Adjustment Details:**")
                m = comps["margin"]

                close_label = (
                    "Draw bonus" if m == 0 else
                    "Close game bonus (1-goal margin)" if m == 1 else
                    None
                )

                lines = [
                    f"- **Base Score:** {comps['base']:.2f}",
                    f"- **Goal Excitement ({comps['total_goals']} goals):** +{comps['goal_bonus']:.2f} pts",
                ]

                if close_label:
                    lines.append(f"- **{close_label}:** +{comps['close_bonus']:.2f} pts")

                if comps["has_sot"]:
                    lines.append(
                        f"- **Shot Activity ({comps['total_sot']} on target · "
                        f"{comps['home_sot']} vs {comps['away_sot']}):** +{comps['sot_bonus']:.2f} pts"
                    )

                if comps["margin_pen"] > 0:
                    lines.append(
                        f"- **Blowout Penalty (margin {m}):** -{comps['margin_pen']:.2f} pts"
                    )

                if comps["has_shots"] and comps["dom_pen"] > 0:
                    lines.append(
                        f"- **Domination Penalty ({comps['home_shots']} vs {comps['away_shots']} shots):** "
                        f"-{comps['dom_pen']:.2f} pts"
                    )

                if not comps["has_sot"]:
                    lines.append("- *Shot data unavailable — score based on goals only*")

                st.markdown("\n".join(lines))
