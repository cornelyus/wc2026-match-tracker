# WC 2026 Match Excitement Tracker

A Streamlit app that ranks every World Cup 2026 match by an objective **excitement score** (0–10), updated live during the tournament.

**Live demo:** *(add your Streamlit Cloud URL here)*

---

## How it works

- Fetches all fixtures from the **ESPN public API** — no API key required
- For finished matches, pulls per-team shot stats (shots on target, total shots) from the ESPN summary endpoint
- Runs each finished match through an **excitement algorithm** that weighs goals, closeness, shot activity, and dominance
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

---

## Algorithm at a glance

The excitement score starts at a neutral **5.5** and adjusts based on match data:

| Component | Effect | Max impact |
|---|---|---|
| Goal bonus | +0.45 per goal | +2.5 |
| Close-game bonus | +0.4 for draw, +0.2 for 1-goal margin | +0.4 |
| Shot activity bonus | +0.1 per SOT above 6 | +1.0 |
| Blowout penalty | −0.55 per goal of margin above 2 | unbounded |
| Domination penalty | −0.5 × shot imbalance ratio | −0.5 |

Example: a 2–1 thriller with 14 shots on target and an even shot split scores roughly **7.8** ("⚡ Exciting"). A 3–0 with 4 SOT and one team taking 80% of shots scores around **5.0** ("😴 Skip").

| Verdict | Score |
|---|---|
| 🔥 Classic | ≥ 7.8 |
| ⚡ Exciting | ≥ 7.0 |
| ⚖️ Decent | ≥ 6.0 |
| 😴 Skip | < 6.0 |

---

## Roadmap

- **v1.0** — Goals-only excitement score from ESPN fixture data
- **v1.1** *(current)* — Enriched with ESPN shot stats (SOT bonus + domination penalty)
- **v2.0** *(planned)* — xG + player ratings via a local fetch script writing to a GitHub Gist; Streamlit reads from the Gist URL so no redeploy is triggered on data updates
