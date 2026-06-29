# Architecture Decision Record

A living document of the design choices made in building this app, with the reasoning and tradeoffs behind each one. Useful for understanding *why* the code is the way it is, not just what it does.

---

## D1 — Data source: ESPN public API

**Decision:** Use ESPN's undocumented public soccer API for all match data.

**Why:** No API key, no rate-limit headers, and it returns everything needed: fixture list, live scores, match status, and per-match boxscore stats. The scoreboard endpoint covers the full tournament in one request.

```python
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "fifa.world/scoreboard?limit=200&dates=20260611-20260719"
)
ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "fifa.world/summary?event={}"
)
```

**What was rejected:**

- **FotMob** — requires a JS-generated `x-mas` request token that changes per-session. Even `cloudscraper` (which bypasses Cloudflare) can't generate it without a headless browser. Every endpoint returns 404 without the token.
- **Sofascore** — returns hard 403 for all requests, even with full browser headers and `cloudscraper`. No documented public API.
- **api-football.com** — had no World Cup 2026 match data during testing.
- **PyPI `fotmob-api` wrapper** — requires Python 3.10+ (uses `int | List[int]` union syntax, incompatible with 3.9), and the method names in the package differ from what was documented online.

---

## D2 — Shot stats keyed by team `displayName`, not `homeAway`

**Decision:** In `_fetch_one_summary()`, build a dict keyed by team `displayName`. Resolve home/away in `parse_events()` where that mapping is already known from the scoreboard response.

**Why:** The ESPN summary endpoint includes a `uniform.type` field that looks like it should indicate home/away, but it returns `"home"` for *both* teams — it refers to which kit the team is wearing (their home colours), not their match role. Keying by name sidesteps this entirely.

```python
# _fetch_one_summary: key by display name
by_team = {}
for team_data in r.json().get("boxscore", {}).get("teams", []):
    name = team_data.get("team", {}).get("displayName", "")
    ...
    by_team[name] = team_stats

# parse_events: look up using home/away names from scoreboard data
home_name  = row["Home"]  # already resolved from competitors[homeAway=="home"]
home_extra = by_team.get(home_name, {})
```

---

## D3 — Excitement algorithm design (0–10 scale)

**Decision:** A formula-based score starting at a neutral base, then adjusted by five variables.

**Why formula over ML:** The tournament runs for ~6 weeks. There isn't enough WC data to train anything meaningful, and the output needs to be legible — coaches, journalists, and fans should be able to read the breakdown and argue with it.

### Components

**Base: 5.5**
A neutral midpoint. A match where nothing interesting happened isn't 0 — it happened, teams showed up. 5.5 keeps the scale meaningful at both ends.

**Goal bonus: `min(total_goals × 0.45, 2.5)`**
Goals are the primary excitement driver. The cap at 2.5 prevents a 7-goal game from crowding out everything else.

**Close-game bonus: +0.4 (draw) / +0.2 (1-goal margin)**
A tense finish adds excitement independent of goal count. A 0-0 after 90 minutes of end-to-end play deserves more than the same score as a 0-0 where both goalkeepers were spectators.

**SOT bonus: `min(max(0.0, (total_sot - 6) × 0.1), 1.0)`**
Shot activity reveals match intensity that goals don't capture. Six SOT is a low-baseline "normal" game; every additional SOT adds 0.1 pt, capped at 1.0 (so 16 SOT gives the full bonus).

**Blowout penalty: `max(0.0, (margin - 2) × 0.55)`**
A 4-0 kills suspense even if it has 4 goals. Margin above 2 is penalised progressively — 3-0 costs 0.55, 4-0 costs 1.10, etc.

**Shot-volume bonus: `min(max(0.0, (total_shots − 20) × 0.04), 0.8)`** *(added — see D10)*
A chance-heavy game is open and entertaining even when finishing is poor. Total shots above a ~20 baseline add 0.04 each, capped at 0.8 (full bonus at 40 shots). This is the only thing that lifts a high-shot 0–0 out of "skip".

**Drama bonus** *(added — see D9)*
Reads the goal *sequence* (lead changes, equalizers, meaningful late goals), the one thing the final scoreline can't show.

**Domination penalty: `max(0.0, dom_ratio − 0.4) × 0.83`** *(deadbanded — see D10)*
Only genuinely one-sided games are punished. `dom_ratio = abs(home_shots − away_shots) / total_shots`. Zero penalty until worse than ~70/30; ~0.17 at 80/20; ~0.50 at a total wipeout. A competitive 60/40 game is no longer dinged.

### Worked example

Argentina 2–1 France: 3 goals, 1-goal margin, 14 SOT (8 vs 6), 24 total shots (14 vs 10), no lead changes/late drama.

```
base        =  5.50
goal_bonus  = +1.35  (3 × 0.45)
close_bonus = +0.20  (1-goal margin)
sot_bonus   = +0.80  ((14 − 6) × 0.1)
volume_bonus= +0.16  ((24 − 20) × 0.04)
drama_bonus = +0.00  (no lead changes / equalizers / late goals)
dom_pen     =  0.00  (ratio 0.17 < 0.4 deadband)
margin_pen  =  0.00  (margin ≤ 2)
─────────────────────
score       =  8.01  → 🔥 Classic
```

---

## D4 — Two-tier caching with `@st.cache_data`

**Decision:** Cache fixtures for 5 minutes (`ttl=300`), cache shot summaries for 1 hour (`ttl=3600`).

**Why two TTLs:** Fixtures change during live matches (score, clock, status). Shot stats only exist after a match ends and never change after that — caching them for an hour avoids re-fetching N summary endpoints on every page load.

```python
@st.cache_data(ttl=300)      # live data — refresh often
def fetch_all_matches(): ...

@st.cache_data(ttl=3600)     # immutable once a match ends
def fetch_match_summaries(event_ids): ...
```

**Why not external storage (database, Redis, Gist):** ESPN's APIs are publicly accessible from Streamlit Cloud's servers — no firewall or IP block. In-memory Streamlit cache is sufficient for the current MVP. External storage would be needed only if fetching from a blocked source (see MVP2 roadmap: FotMob xG via Gist).

**Note on cache keys:** `fetch_match_summaries` takes a `tuple` of event IDs, not a list. Streamlit's cache hashes arguments — lists are not hashable, tuples are.

```python
finished_ids = tuple(
    e["id"] for e in events
    if ... .get("state") == "post"
)
summaries = fetch_match_summaries(finished_ids)
```

---

## D5 — Parallel summary fetching with `ThreadPoolExecutor`

**Decision:** Fetch all per-match summaries concurrently using a thread pool of 10 workers.

**Why:** By the group stage, ~48 matches will be finished. Sequential fetching at ~1s per request = ~48s cold-start latency. With 10 concurrent workers the same 48 requests complete in ~5–6s. The ESPN summary endpoint is read-only and stateless, so concurrent requests are safe.

```python
@st.cache_data(ttl=3600)
def fetch_match_summaries(event_ids):
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_summary, eid): eid for eid in event_ids}
        for f in as_completed(futures):
            eid, stats = f.result()
            results[eid] = stats
    return results
```

`as_completed` yields futures as they resolve rather than in submission order — results arrive as fast as possible rather than waiting for the slowest-first ordering.

---

## D6 — Round detection: `altGameNote` + `season.slug` fallback

**Decision:** Read `altGameNote` from the competition object for group games; fall back to a slug-to-display-name map for knockout rounds.

**Why:** ESPN's `notes` array (the obvious place to look) is always empty for this competition. After inspecting the raw API response, two fields carry round info depending on the stage:

- **Group stage:** `comp.altGameNote` → `"FIFA World Cup, Group A"` etc.
- **Knockout stage:** `evt.season.slug` → `"round-of-16"`, `"quarterfinals"`, etc.

```python
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

# altGameNote uses "3rd-Place Match"; normalise to our display name
round_str = round_str.replace("3rd-Place Match", "3rd Place Final")
```

The `ROUND_ORDER` list controls sort order in the round filter dropdown:

```python
ROUND_ORDER = [
    "Group A", ..., "Group L",
    "Round of 32", "Round of 16", "Quarterfinals",
    "Semifinals", "3rd Place Final", "Final",
]
```

---

## D7 — Mobile UX: `flex-direction: column-reverse`

**Decision:** Inject a CSS media query that reverses the Streamlit column order on screens ≤ 640px wide.

**Why:** Streamlit renders `st.columns([3, 2])` as a CSS flex row. On mobile it collapses to vertical stacking — left column first, right column below. The layout is: table on the left, detail panel on the right. On mobile, a user taps a row in the table and then has to scroll *down* past the full table to find the detail panel. Reversing the stack order puts the detail panel above the table on mobile, so it's immediately visible after a tap.

```python
st.markdown("""
<style>
@media (max-width: 640px) {
    [data-testid="stHorizontalBlock"] { flex-direction: column-reverse; }
}
</style>
""", unsafe_allow_html=True)
```

`[data-testid="stHorizontalBlock"]` is the Streamlit-internal selector for the flex container wrapping `st.columns`. It has been stable across multiple Streamlit versions. On desktop (> 640px) `flex-direction` remains `row` — no change.

---

## D8 — No authentication or secrets needed

**Decision:** The app ships with no secrets, no API keys, and no environment variables.

**Why:** All ESPN endpoints are public and unauthenticated. The app can be forked and deployed to Streamlit Cloud by anyone with no setup beyond linking the repo.

`.streamlit/secrets.toml` is gitignored as a precaution — it's the standard location for Streamlit secrets and could accidentally be committed if created locally for future use (e.g. a GitHub Gist URL for MVP2 enriched stats). The file doesn't need to exist for the app to run.

---

## D9 — Drama Index from `keyEvents` (goal-timing bonus)

**Decision:** Add a `drama_bonus` to `calculate_excitement()` derived from the *sequence* of goals, not just the final scoreline.

**Why:** Every other component (goals, margin, shots) reads only the end state. Two 6-goal games can be wildly different experiences — Algeria 3–3 Austria (three equalizers, a 90'+3' go-ahead, a 90'+6' leveller) vs Netherlands 5–1 Sweden (4–0 by half-time). The scoreline-based formula rated them almost identically; the goal timeline is the missing signal.

**Where the data comes from:** The `keyEvents` array is already in the `/summary` response the app fetches for shot stats — so this costs **zero extra HTTP requests**. `_parse_drama()` walks the scoring plays (skipping `shootout` events), reconstructs the running home/away score from each goal's text (`"Goal! Algeria 3 - 3 Austria."`, home listed first), and counts three things:

- **lead_changes** — the lead changed hands (tracked via the last non-zero lead sign, so it counts an actual swing from one team leading to the other, not the intermediate equalizer)
- **equalizers** — a trailing team drew level
- **late_goals** — goals at minute ≥ 80 (clock's leading integer, incl. stoppage) **that arrived while the game was still within one goal**. `_parse_drama()` tracks the running score and only counts a late goal if the margin *before* it was ≤ 1 — a late winner/leveller/insurance, not a consolation piled onto a decided blowout.

**Formula:**
```python
drama_bonus = min(lead_changes * 0.4 + equalizers * 0.3 + late_goals * 0.2, 1.2)
```

**Weight rationale** — ordered by rarity × dramatic punch, intuition-calibrated (not data-fit — too little data), scaled so a genuine thriller saturates the cap:
- **lead_changes (0.4, highest):** rarest, strongest. A lead can't flip on a single goal (score must pass through level first), so it guarantees a real back-and-forth swing.
- **equalizers (0.3):** comeback tension; trailing team draws level. Common in tight games, less decisive than an actual swing.
- **late_goals (0.2, lowest):** drama near the whistle, the weakest/noisiest signal — so it's filtered to *meaningful* late goals only (see above); a late goal in a decided 5–1 is piling on, not drama.

**Worked examples (live WC2026 data):**

```
Algeria 3–3 Austria    (lc 1, eq 3, late 2): 0.4 + 0.9 + 0.4 = 1.7 → cap +1.20 → raw 9.88, eased to 9.59 🔥 (D11)
Netherlands 5–1 Sweden (lc 0, eq 0, late 0): the 89' goal came at 4–1, excluded → +0.00
```

The cap (1.2) keeps a single thriller from saturating the scale; a 0–0 has no goal events, so its drama bonus is always 0. The metric is purely additive (a bonus, never a penalty) and lifts back-and-forth games above flat ones at the same scoreline without moving one-sided or goalless matches.

---

## D10 — Shot-volume bonus + domination-penalty deadband

**Decision:** Add a `volume_bonus` for total shots, and deadband the existing `dom_pen` so it only fires on genuinely one-sided games.

**Why (the problem case):** Colombia 0–0 Portugal — 37 total shots (24 vs 13), 8 on target — scored **5.95 "Skip"**, the same neighbourhood as a quiet 0–0. Two flaws combined:

1. **Shot volume was invisible.** Only shots *on target above 6* scored; the 37-shot chance-fest and a dull 0–0 got the same shot credit. And the drama bonus can't help a goalless game (no goals to sequence) — a volume term is the *only* lever that lifts a high-shot 0–0.
2. **The domination penalty over-fired.** `dom_pen = dom_ratio × 0.5` rose linearly from *zero* imbalance, so Colombia's competitive 65/35 split was penalised −0.15 — and the heavier-shooting side got dinged hardest. The intent ("one team barely existed") only holds at extreme splits.

**Changes:**
```python
volume_bonus = min(max(0.0, (total_shots - 20) * 0.04), 0.8)   # +0.04/shot >20, cap 0.8
dom_pen      = max(0.0, dom_ratio - 0.4) * 0.83                 # 0 until ~70/30, ~0.5 at wipeout
```

**Effect:** Colombia 0–0 Portugal → **6.78 ⚖️ Decent** (+0.68 volume, dom now 0). The deadband leaves only true blowouts penalised — Canada 6–0 Qatar (32 vs 2 shots, −0.40), England 0–0 Ghana (19 vs 2, −0.34), Spain 4–0 Saudi Arabia (22 vs 3). The two new shot terms also self-balance in lopsided high-volume games (e.g. NZ 1–5 Belgium, 41 shots but 6 vs 35: +0.80 volume partly offset by −0.26 domination).

**Tradeoff:** raw shot count can flatter long-range pot-shots — the on-target (`sot_bonus`) term keeps quality in the mix, and the +0.8 cap limits the damage. Adding a positive component also pushed the best games into the old 10.00 hard clamp; that's now handled by the soft cap (see D11) rather than by trimming volume.

---

## D11 — Soft cap on the score ceiling

**Decision:** Replace the hard `min(10.0, …)` clamp with `_soft_cap()` — scores above a 9.0 knee are eased asymptotically toward 10 instead of being clipped.

**Why:** Once drama + volume stacked on top of goal/shot bonuses, the realistic maximum sum exceeded 11, so the best games piled up at exactly 10.00 — England 4–2 Croatia and Morocco 4–2 Haiti both pinned there, indistinguishable. They're thrillers, but 10 should stay reserved for a hypothetical perfect storm, not be reachable by any very good game.

**How:**
```python
def _soft_cap(raw, knee=9.0, ceiling=10.0):
    if raw <= knee:
        return raw                       # most games: score == the literal sum
    span = ceiling - knee
    return knee + span * (1 - math.exp(-(raw - knee) / span))
```
The curve is continuous at the knee (derivative 1, so no kink) and monotonic (ordering preserved), and approaches 10 without reaching it — raw 10.3 → ~9.7, raw 12 → ~9.99. Only the handful of games scoring 9.0+ are touched; everything below is unchanged, so the breakdown still sums to the score for the vast majority. When the cap is active the detail panel adds a line (`Raw total X eased toward the 10 ceiling → Y`) so the non-additivity is explicit.

**Effect:** no game now sits at 10.00 — current top is Morocco 4–2 Haiti at **9.73**, with England 4–2 Croatia 9.72 and Algeria 3–3 Austria 9.59 cleanly separated instead of tied at the ceiling.

The knee (9.0) is the single knob: raise it to compress fewer games, lower it for more headroom among elite games.
