import re
import math
import streamlit as st
import streamlit.components.v1 as components
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
def _soft_cap(raw, knee=9.0, ceiling=10.0):
    """Ease scores above the knee asymptotically toward the ceiling.

    Below the knee the raw additive total is returned unchanged (so the breakdown
    still sums to the score for the vast majority of games). Above it, excess is
    compressed so even a perfect storm of bonuses only approaches 10 — the ceiling
    stays reserved and elite games keep their ordering instead of all pinning at 10.
    """
    if raw <= knee:
        return raw
    span = ceiling - knee
    return knee + span * (1 - math.exp(-(raw - knee) / span))

def calculate_excitement(home_goals, away_goals,
                         home_sot=None, away_sot=None,
                         home_shots=None, away_shots=None,
                         lead_changes=0, equalizers=0, late_goals=0):
    margin      = abs(home_goals - away_goals)
    total_goals = home_goals + away_goals
    has_sot     = home_sot is not None and away_sot is not None
    has_shots   = home_shots is not None and away_shots is not None

    base        = 4.5
    goal_bonus  = min(total_goals * 0.45, 2.5)
    close_bonus = 0.4 if margin == 0 else (0.2 if margin == 1 else 0.0)
    margin_pen  = max(0.0, (margin - 2) * 0.55)

    # Drama bonus: rewards how the goals arrived, not just the final scoreline.
    # Lead changes and comeback equalizers and late goals capture the story a
    # static scoreline misses (a swingy 3-3 vs a 5-1 that was over by half-time).
    drama_bonus = round(min(
        lead_changes * 0.4 + equalizers * 0.3 + late_goals * 0.2, 1.2
    ), 2)

    # SOT signal: a continuous gauge centered on a 6-SOT "average" game. Above 6,
    # each extra SOT adds 0.1 pt (capped +1.0); below 6, each missing SOT subtracts
    # 0.15 pt (capped -0.6) so a goalless grind or a few-chances win isn't rated
    # like an open game. Penalizing the dull game is cleaner than inflating the busy
    # one — a 3-SOT 1-1 should rank below an 8-SOT 0-0.
    if has_sot:
        total_sot   = home_sot + away_sot
        sot_bonus   = round(min(max(0.0, (total_sot - 6) * 0.1), 1.0), 2)
        low_sot_pen = round(min(max(0.0, (6 - total_sot) * 0.15), 0.6), 2)
    else:
        total_sot   = None
        sot_bonus   = 0.0
        low_sot_pen = 0.0

    # Shot-volume bonus: a chance-heavy game is open and entertaining even when
    # finishing is poor (a 37-shot 0-0 isn't the same as a quiet one). Total shots
    # above a ~20 baseline add 0.04 each, capped at 0.8.
    if has_shots:
        total_shots  = home_shots + away_shots
        volume_bonus = round(min(max(0.0, (total_shots - 20) * 0.04), 0.8), 2)
        # Domination penalty: only genuinely one-sided games (worse than ~70/30)
        # are punished — a deadband so competitive 60/40 games aren't dinged.
        dom_ratio = abs(home_shots - away_shots) / total_shots if total_shots else 0.0
        dom_pen   = round(max(0.0, dom_ratio - 0.4) * 0.83, 2)
    else:
        total_shots  = None
        volume_bonus = 0.0
        dom_pen      = 0.0

    raw = (base + goal_bonus + close_bonus + sot_bonus + drama_bonus + volume_bonus
           - dom_pen - margin_pen - low_sot_pen)
    score = round(_soft_cap(max(0.0, raw)), 2)

    return score, {
        "raw_score":   round(raw, 2),
        "base":        base,
        "goal_bonus":  round(goal_bonus,  2),
        "close_bonus": round(close_bonus, 2),
        "margin_pen":  round(margin_pen,  2),
        "sot_bonus":    sot_bonus,
        "low_sot_pen":  low_sot_pen,
        "dom_pen":      dom_pen,
        "volume_bonus": volume_bonus,
        "drama_bonus":  drama_bonus,
        "lead_changes": lead_changes,
        "equalizers":   equalizers,
        "late_goals":   late_goals,
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

def _parse_drama(key_events):
    """Reconstruct the goal sequence from keyEvents into a 'drama' signal.

    Reads the running score embedded in each goal's text ("Goal! Algeria 3 - 3
    Austria.", home listed first) to count lead changes (the lead changed hands)
    and equalizers (a trailing team drew level), plus late goals (minute >= 80).
    A late goal only counts if it leaves the game within one goal AFTERWARDS — a
    late winner, leveller, or a strike that pulls it back to a one-score game — but
    NOT a lead-extender like 1-0 -> 2-0 that kills the contest, nor a consolation
    piled onto a decided blowout. Shootout events are ignored — not open-play drama.
    """
    lead_changes = equalizers = late_goals = 0
    prev = None             # last non-zero lead sign (+1 home ahead, -1 away ahead)
    for e in key_events:
        if not e.get("scoringPlay") or e.get("shootout"):
            continue
        m = re.search(r"(\d+)\s*,\s*\D*?(\d+)\s*\.", e.get("text", "") or "")
        if not m:
            continue
        h, a = int(m.group(1)), int(m.group(2))
        diff = h - a
        sign = (diff > 0) - (diff < 0)
        if prev is not None:
            if sign == 0 and prev != 0:
                equalizers += 1
            elif sign != 0 and prev != 0 and sign != prev:
                lead_changes += 1
        if sign != 0:
            prev = sign
        elif prev is None:
            prev = 0

        clock = (e.get("clock") or {}).get("displayValue", "") or ""
        mm = re.match(r"\s*(\d+)", clock)
        # margin AFTER the goal: a late goal is only "drama" if it leaves the game
        # within one (winner/leveller/comeback), not if it stretches a lead.
        if mm and int(mm.group(1)) >= 80 and abs(h - a) <= 1:
            late_goals += 1

    return {
        "lead_changes": lead_changes,
        "equalizers":   equalizers,
        "late_goals":   late_goals,
    }

def _fetch_one_summary(event_id):
    try:
        r = requests.get(ESPN_SUMMARY.format(event_id), timeout=15)
        r.raise_for_status()
        payload = r.json()
        # Key stats by team displayName — home/away resolved in parse_events
        by_team = {}
        for team_data in payload.get("boxscore", {}).get("teams", []):
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
        drama = _parse_drama(payload.get("keyEvents", []))
        return event_id, {"teams": by_team, "drama": drama}
    except Exception:
        return event_id, {"teams": {}, "drama": {}}

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

        alt_note    = comp.get("altGameNote", "") or ""
        season_slug = evt.get("season", {}).get("slug", "")
        if alt_note:
            round_str = alt_note.replace("FIFA World Cup, ", "").strip()
        else:
            round_str = {
                "round-of-32":    "Round of 32",
                "round-of-16":    "Round of 16",
                "quarterfinals":  "Quarterfinals",
                "semifinals":     "Semifinals",
                "3rd-place-match":"3rd Place Final",
                "final":          "Final",
            }.get(season_slug, "")
        round_str = round_str.replace("3rd-Place Match", "3rd Place Final")

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
            "SOT":    None,
            "Excitement": None,
            "Verdict": "—",
        }

        if state == "post":
            mid        = evt.get("id")
            entry      = summaries.get(mid, {})
            by_team    = entry.get("teams", {})
            drama      = entry.get("drama", {})
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
                lead_changes=drama.get("lead_changes", 0),
                equalizers=drama.get("equalizers", 0),
                late_goals=drama.get("late_goals", 0),
            )
            components_store[mid] = comps
            row.update({
                "SOT":        comps["total_sot"] if comps["has_sot"] else None,
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

# ── Analytics (self-hosted Umami) ─────────────────────────────────────────────
# Inject the Umami tracker into the *parent* document — components.html() renders
# in a sandboxed iframe, so a naive tag would track the iframe, not the real app
# URL. The id-guard keeps Streamlit's per-interaction reruns from re-injecting it.
# Configure via secrets (Community Cloud → app settings → Secrets); no-op if unset.
try:
    _UMAMI_SRC = st.secrets.get("UMAMI_SRC", "")
    _UMAMI_ID  = st.secrets.get("UMAMI_ID", "")
except Exception:
    _UMAMI_SRC = _UMAMI_ID = ""

if _UMAMI_SRC and _UMAMI_ID:
    components.html(
        f"""
        <script>
          const doc = window.parent.document;
          if (!doc.getElementById("umami-tracker")) {{
            const s = doc.createElement("script");
            s.id = "umami-tracker";
            s.defer = true;
            s.src = "{_UMAMI_SRC}";
            s.setAttribute("data-website-id", "{_UMAMI_ID}");
            doc.head.appendChild(s);
          }}
        </script>
        """,
        height=0,
    )

st.title("🏆 World Cup 2026 — Match Excitement Tracker")
st.markdown(
    "Data-driven ranking of all WC2026 matches by excitement — "
    "goals, score margin, and shot activity combined. "
    "**Click any row** to see the full score breakdown."
)

# On mobile the two columns stack vertically; reverse the order so the
# detail panel appears above the table instead of below it.
st.markdown("""
<style>
@media (max-width: 640px) {
    [data-testid="stHorizontalBlock"] { flex-direction: column-reverse; }
}
</style>
""", unsafe_allow_html=True)

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
DISPLAY_COLS = ["Round", "Date", "Home", "Score", "Away", "SOT", "Excitement", "Verdict"]

col_table, col_detail = st.columns([3, 2])

with col_table:
    sel_event = st.dataframe(
        view[DISPLAY_COLS],
        selection_mode="single-row",
        on_select="rerun",
        column_config={
            "SOT": st.column_config.NumberColumn(
                "SOT",
                format="%d",
                help="Total shots on target (both teams). Sortable — click a row for the full breakdown.",
            ),
            "Excitement": st.column_config.NumberColumn("Excitement ⭐", format="%.2f"),
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

                if comps.get("low_sot_pen", 0) > 0:
                    lines.append(
                        f"- **Low Shot Activity Penalty ({comps['total_sot']} on target · "
                        f"below the 6 baseline):** -{comps['low_sot_pen']:.2f} pts"
                    )

                if comps.get("volume_bonus", 0) > 0:
                    lines.append(
                        f"- **Shot Volume ({comps['total_shots']} total shots · "
                        f"{comps['home_shots']} vs {comps['away_shots']}):** +{comps['volume_bonus']:.2f} pts"
                    )

                if comps.get("drama_bonus", 0) > 0:
                    parts = []
                    if comps["lead_changes"]:
                        parts.append(f"{comps['lead_changes']} lead change"
                                     f"{'s' if comps['lead_changes'] != 1 else ''}")
                    if comps["equalizers"]:
                        parts.append(f"{comps['equalizers']} equalizer"
                                     f"{'s' if comps['equalizers'] != 1 else ''}")
                    if comps["late_goals"]:
                        parts.append(f"{comps['late_goals']} late goal"
                                     f"{'s' if comps['late_goals'] != 1 else ''}")
                    lines.append(
                        f"- **Drama ({' · '.join(parts)}):** +{comps['drama_bonus']:.2f} pts"
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

                if score is not None and comps.get("raw_score", 0) - score > 0.01:
                    lines.append(
                        f"- *Raw total {comps['raw_score']:.2f} eased toward the 10 ceiling "
                        f"→ {score:.2f}*"
                    )

                st.markdown("\n".join(lines))
