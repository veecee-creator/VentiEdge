
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "football.db")))

FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDSPAPI_KEY = os.getenv("ODDSPAPI_KEY", "")

LEAGUES = {
    "PL": ("Premier League", "soccer_epl"),
    "PD": ("La Liga", "soccer_spain_la_liga"),
    "SA": ("Serie A", "soccer_italy_serie_a"),
    "BL1": ("Bundesliga", "soccer_germany_bundesliga"),
    "FL1": ("Ligue 1", "soccer_france_ligue_one"),
}

app = FastAPI(title="VentiEdge API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://veecee-creator.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialise_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                competition TEXT,
                season INTEGER,
                utc_date TEXT,
                status TEXT,
                home_team TEXT,
                away_team TEXT,
                home_goals INTEGER,
                away_goals INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds (
                event_id TEXT,
                league TEXT,
                commence TEXT,
                home TEXT,
                away TEXT,
                book TEXT,
                oh REAL,
                od REAL,
                oa REAL,
                ts TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kenya (
                home TEXT,
                away TEXT,
                commence TEXT,
                book TEXT,
                oh REAL,
                od REAL,
                oa REAL,
                ts TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placed TEXT,
                fixture TEXT,
                pick TEXT,
                site TEXT,
                odds REAL,
                stake REAL,
                result TEXT DEFAULT 'pending'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                name TEXT PRIMARY KEY,
                crest TEXT
            )
        """)


initialise_db()


def request_json(url, *, params=None, headers=None, timeout=30):
    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not contact data provider: {exc}",
        ) from exc

    if not response.ok:
        detail = response.text[:300] or response.reason
        raise HTTPException(
            status_code=502,
            detail=f"Provider returned HTTP {response.status_code}: {detail}",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="The data provider returned invalid JSON.",
        ) from exc


@app.get("/")
def index():
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(404, "static/index.html was not found.")
    return FileResponse(path)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "VentiEdge API",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/picks")
def picks():
    with connect_db() as conn:
        odds_rows = conn.execute("""
            SELECT * FROM odds
            WHERE commence > ?
            ORDER BY commence
        """, (datetime.now(timezone.utc).isoformat(),)).fetchall()

        kenya_rows = conn.execute("SELECT * FROM kenya").fetchall()

    grouped = {}
    for row in odds_rows:
        grouped.setdefault(row["event_id"], []).append(dict(row))

    events = []
    for event_id, rows in grouped.items():
        first = rows[0]
        best = [
            max(rows, key=lambda r: r["oh"]),
            max(rows, key=lambda r: r["od"]),
            max(rows, key=lambda r: r["oa"]),
        ]
        prices = [best[0]["oh"], best[1]["od"], best[2]["oa"]]

        if any(not price or price <= 1 for price in prices):
            continue

        # Remove each bookmaker's overround before combining probabilities.
        totals = [0.0, 0.0, 0.0]
        weights = 0.0
        for row in rows:
            if min(row["oh"], row["od"], row["oa"]) <= 1:
                continue
            inv = [1 / row["oh"], 1 / row["od"], 1 / row["oa"]]
            total = sum(inv)
            weight = 3 if "pinnacle" in row["book"].lower() else 1
            for i in range(3):
                totals[i] += (inv[i] / total) * weight
            weights += weight

        if not weights:
            continue

        probabilities = [value / weights for value in totals]
        edges = [
            probabilities[i] * prices[i] - 1
            for i in range(3)
        ]
        best_index = max(range(3), key=lambda i: edges[i])

        matching_kenya = {}
        for kr in kenya_rows:
            if (
                kr["home"] == first["home"]
                and kr["away"] == first["away"]
            ):
                matching_kenya[kr["book"]] = [
                    kr["oh"], kr["od"], kr["oa"]
                ]

        pick = None
        if edges[best_index] >= 0.03:
            pick = {
                "side": ["Home", "Draw", "Away"][best_index],
                "edge": edges[best_index],
                "odds": prices[best_index],
                "book": best[best_index]["book"],
                "stake_pct": min(
                    5,
                    max(
                        0,
                        0.25 * edges[best_index]
                        / (prices[best_index] - 1) * 100,
                    ),
                ),
                "score": edges[best_index] * 100,
                "strength": (
                    "Strong" if edges[best_index] >= 0.06
                    else "Good" if edges[best_index] >= 0.04
                    else "Lean"
                ),
            }

        inverse_sum = sum(1 / price for price in prices)
        arbitrage = None
        if inverse_sum < 1:
            arbitrage = {
                "profit": (1 / inverse_sum - 1) * 100,
                "stakes": [
                    round(100 / price / inverse_sum, 2)
                    for price in prices
                ],
            }

        events.append({
            "league": first["league"],
            "home": first["home"],
            "away": first["away"],
            "commence": first["commence"],
            "books": len(rows),
            "kenya": matching_kenya,
            "home_crest": None,
            "away_crest": None,
            "probs": probabilities,
            "model": None,
            "xg": None,
            "basis": "market consensus",
            "best": [
                {"odds": prices[i], "book": best[i]["book"]}
                for i in range(3)
            ],
            "pick": pick,
            "form": ["", ""],
            "rest": [None, None],
            "goals_lean": None,
            "arb": arbitrage,
        })

    return {
        "events": events,
        "odds_updated": odds_rows[0]["ts"] if odds_rows else None,
        "now": datetime.now(timezone.utc).isoformat(),
        "time_source": "server",
    }


@app.post("/api/refresh/matches")
def refresh_matches():
    if not FOOTBALL_DATA_KEY:
        raise HTTPException(
            503,
            "FOOTBALL_DATA_KEY is missing from Render environment variables.",
        )

    saved = 0
    skipped = []

    for code, (league_name, _) in LEAGUES.items():
        response_data = request_json(
            f"https://api.football-data.org/v4/competitions/{code}/matches",
            params={"season": datetime.now(timezone.utc).year},
            headers={"X-Auth-Token": FOOTBALL_DATA_KEY},
            timeout=45,
        )

        with connect_db() as conn:
            for match in response_data.get("matches", []):
                score = match.get("score", {}).get("fullTime", {})
                home = match.get("homeTeam", {})
                away = match.get("awayTeam", {})

                conn.execute("""
                    INSERT OR REPLACE INTO matches
                    (id, competition, season, utc_date, status,
                     home_team, away_team, home_goals, away_goals)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    match["id"],
                    code,
                    datetime.now(timezone.utc).year,
                    match.get("utcDate"),
                    match.get("status"),
                    home.get("name", "Unknown"),
                    away.get("name", "Unknown"),
                    score.get("home"),
                    score.get("away"),
                ))

                for team in (home, away):
                    if team.get("name"):
                        conn.execute("""
                            INSERT OR REPLACE INTO teams(name, crest)
                            VALUES (?, ?)
                        """, (team["name"], team.get("crest")))
                saved += 1

    return {"matches": saved, "skipped": skipped}


@app.post("/api/refresh/odds")
def refresh_odds():
    if not ODDS_API_KEY:
        raise HTTPException(
            503,
            "ODDS_API_KEY is missing from Render environment variables.",
        )

    rows = []
    timestamp = datetime.now(timezone.utc).isoformat()

    for code, (_, sport_key) in LEAGUES.items():
        data = request_json(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu,uk",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            timeout=45,
        )

        for event in data:
            for bookmaker in event.get("bookmakers", []):
                market = next(
                    (
                        item for item in bookmaker.get("markets", [])
                        if item.get("key") == "h2h"
                    ),
                    None,
                )
                if not market:
                    continue

                outcomes = {
                    item.get("name"): item.get("price")
                    for item in market.get("outcomes", [])
                }
                home = event.get("home_team")
                away = event.get("away_team")

                if not all(
                    name in outcomes for name in (home, away, "Draw")
                ):
                    continue

                rows.append((
                    event.get("id"),
                    code,
                    event.get("commence_time"),
                    home,
                    away,
                    bookmaker.get("title", "Unknown"),
                    outcomes[home],
                    outcomes["Draw"],
                    outcomes[away],
                    timestamp,
                ))

    with connect_db() as conn:
        conn.execute("DELETE FROM odds")
        conn.executemany("""
            INSERT INTO odds
            (event_id, league, commence, home, away, book,
             oh, od, oa, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

    return {"odds_rows": len(rows)}


@app.post("/api/refresh/kenya")
def refresh_kenya():
    if not ODDSPAPI_KEY:
        raise HTTPException(
            503,
            "ODDSPAPI_KEY is missing from Render environment variables.",
        )

    today = datetime.now(timezone.utc).date()
    tournament_ids = [17, 8, 23, 35, 34]
    timestamp = datetime.now(timezone.utc).isoformat()

    fixtures = request_json(
        "https://api.oddspapi.io/v4/fixtures",
        params={
            "apiKey": ODDSPAPI_KEY,
            "sportId": 10,
            "from": today.isoformat(),
            "to": today.isoformat(),
        },
        timeout=60,
    )

    # Keep this endpoint honest: fixture availability alone is not odds data.
    # A provider-specific odds response must be parsed successfully before
    # replacing the stored Kenyan odds.
    if not isinstance(fixtures, list):
        raise HTTPException(
            502, "OddsPapi returned an unexpected fixtures response."
        )

    return {
        "kenya_rows": 0,
        "fixtures_seen": len(fixtures),
        "message": (
            "Fixtures were retrieved, but Kenyan bookmaker odds have not "
            "been refreshed by this endpoint yet."
        ),
    }


@app.get("/api/teams")
def team_table(league: str = "PL"):
    if league not in LEAGUES:
        raise HTTPException(400, "Unknown league code.")

    with connect_db() as conn:
        rows = conn.execute("""
            SELECT home_team, away_team, home_goals, away_goals
            FROM matches
            WHERE competition = ? AND status = 'FINISHED'
            ORDER BY utc_date
        """, (league,)).fetchall()

        crests = {
            row["name"]: row["crest"]
            for row in conn.execute("SELECT name, crest FROM teams")
        }

    table = {}
    for row in rows:
        if row["home_goals"] is None or row["away_goals"] is None:
            continue

        pairs = [
            (row["home_team"], row["home_goals"], row["away_goals"]),
            (row["away_team"], row["away_goals"], row["home_goals"]),
        ]

        for name, goals_for, goals_against in pairs:
            item = table.setdefault(name, {
                "team": name, "p": 0, "w": 0, "d": 0, "l": 0,
                "gf": 0, "ga": 0, "pts": 0, "form": "",
                "attack": None, "defense": None,
            })
            item["p"] += 1
            item["gf"] += goals_for
            item["ga"] += goals_against

            if goals_for > goals_against:
                item["w"] += 1
                item["pts"] += 3
                item["form"] += "W"
            elif goals_for == goals_against:
                item["d"] += 1
                item["pts"] += 1
                item["form"] += "D"
            else:
                item["l"] += 1
                item["form"] += "L"

    result = list(table.values())
    for item in result:
        item["crest"] = crests.get(item["team"])
        item["form"] = item["form"][-5:]

    return sorted(
        result,
        key=lambda item: (-item["pts"], -(item["gf"] - item["ga"])),
    )


@app.get("/api/backtest")
def backtest(league: str = "PL", days: int = 365):
    if league not in LEAGUES:
        raise HTTPException(400, "Unknown league code.")

    with connect_db() as conn:
        count = conn.execute("""
            SELECT COUNT(*) AS n FROM matches
            WHERE competition = ? AND status = 'FINISHED'
        """, (league,)).fetchone()["n"]

    if count < 20:
        raise HTTPException(
            400,
            "Not enough historical results. Click Update results first.",
        )

    # This is a data-readiness check, not a verified predictive backtest.
    return {
        "matches": count,
        "accuracy": 0,
        "baseline_accuracy": 0,
        "brier": 0,
        "logloss": 0,
        "baseline_logloss": 0,
        "calibration": [],
        "message": (
            "Historical results are present. A full walk-forward model "
            "backtest is not implemented in this deployment version."
        ),
    }


class Bet(BaseModel):
    fixture: str
    pick: str = ""
    site: str
    odds: float
    stake: float


@app.get("/api/bets")
def list_bets():
    with connect_db() as conn:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM bets ORDER BY id DESC")
        ]


@app.post("/api/bets")
def add_bet(bet: Bet):
    if bet.odds <= 1 or bet.stake <= 0:
        raise HTTPException(
            400, "Odds must be above 1 and stake must be above zero."
        )

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO bets (placed, fixture, pick, site, odds, stake)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            bet.fixture,
            bet.pick,
            bet.site,
            bet.odds,
            bet.stake,
        ))

    return {"ok": True}


@app.post("/api/bets/{bet_id}/{result}")
def settle_bet(bet_id: int, result: str):
    if result not in ("won", "lost", "void"):
        raise HTTPException(400, "Result must be won, lost, or void.")

    with connect_db() as conn:
        cursor = conn.execute(
            "UPDATE bets SET result = ? WHERE id = ?",
            (result, bet_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Bet not found.")

    return {"ok": True}


@app.delete("/api/bets/{bet_id}")
def delete_bet(bet_id: int):
    with connect_db() as conn:
        cursor = conn.execute(
            "DELETE FROM bets WHERE id = ?", (bet_id,)
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Bet not found.")

    return {"ok": True}
