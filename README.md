# WC 2026 Match Excitement Tracker

A Streamlit app that ranks every World Cup 2026 match by an objective **excitement score** (0–10), updated live during the tournament.

**Live demo:** *(add your Streamlit Cloud URL here)*

---

## How it works

- Fetches all fixtures from the **ESPN public API** — no API key required
- For finished matches, pulls per-team shot stats (shots on target, total shots) from the ESPN summary endpoint
- Runs each finished match through an **excitement algorithm** that weighs goals, closeness, shot activity, shot volume, goal-timing drama, and one-sidedness
- Displays results in a sortable table with a detail panel showing the full score breakdown

---

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

No environment variables or secrets needed.

---

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) and connect your repo
3. Set the main file path to `app.py`
4. Deploy — no secrets required

**Optional analytics:** the app can report page views to a self-hosted [Umami](https://umami.is) instance. It's a no-op unless you set both `UMAMI_SRC` (your tracker `script.js` URL) and `UMAMI_ID` (website id) in the app's **Settings → Secrets**.

---

## Algorithm at a glance

The excitement score starts at a calibrated base of **4.5** (a constant tuned so the typical match lands mid-table and "Classic" stays rare — see `DECISIONS.md` D13) and adjusts based on match data:

| Component | Effect | Max impact |
|---|---|---|
| Goal bonus | +0.45 per goal | +2.5 |
| Close-game bonus | +0.4 for draw, +0.2 for 1-goal margin | +0.4 |
| Shot activity (SOT) | +0.1 per shot on target above 6 | +1.0 |
| Shot volume | +0.04 per total shot above 20 | +0.8 |
| Drama | lead changes, equalizers, meaningful late goals | +1.2 |
| Low-SOT penalty | −0.15 per shot on target below 6 | −0.6 |
| Blowout penalty | −0.55 per goal of margin above 2 | unbounded |
| Domination penalty | for shot splits worse than ~70/30 | ~−0.5 |

Scores above 9 are eased asymptotically toward a 10 ceiling, which stays reserved for a hypothetical perfect game (so nothing pins at 10.00).

Example: a 2–1 game with 14 shots on target and an even shot split scores roughly **7.0** ("⚡ Exciting"). A 3–0 with 4 SOT and one team taking ~80% of shots scores around **4.8** ("😴 Skip").

| Verdict | Score |
|---|---|
| 🔥 Classic | ≥ 7.8 |
| ⚡ Exciting | ≥ 7.0 |
| ⚖️ Decent | ≥ 6.0 |
| 😴 Skip | < 6.0 |

---

## Roadmap

- **v1.0** — Goals-only excitement score from ESPN fixture data
- **v1.1** — Enriched with ESPN shot stats (SOT bonus + domination penalty)
- **v1.2** — Drama index (goal timing) + shot-volume bonus + soft-capped ceiling
- **v1.3** — Low-SOT penalty + sortable SOT column
- **v1.4** *(current)* — Re-anchored base for a calibrated distribution + late-goal drama fix
- **v2.0** *(planned)* — xG + player ratings via a local fetch script writing to a GitHub Gist; Streamlit reads from the Gist URL so no redeploy is triggered on data updates
