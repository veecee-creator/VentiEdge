import os, math, sqlite3, time, difflib, unicodedata, email.utils
from datetime import datetime, timezone, timedelta
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

FD_KEY = "bc19319ecd704bf2b350bdf68e7bcdbb"
ODDS_KEY = "1581267b6287da4a9ae169e680a5565b"
OP_KEY = "95fb71a2-5d2b-478c-9d4b-a557a594c130"     # oddspapi.io (Kenyan bookmakers)
OP_TOURN = {"PL": 17, "PD": 8, "SA": 23, "BL1": 35, "FL1": 34}
KE_BOOKS = {"betika": "Betika", "odibets": "Odibets"}
LEAGUES = {"PL": "soccer_epl", "PD": "soccer_spain_la_liga", "SA": "soccer_italy_serie_a",
           "BL1": "soccer_germany_bundesliga", "FL1": "soccer_france_ligue_one"}
SEASONS = (2023, 2024, 2025, 2026)
MODEL_WEIGHT = 0.4      # blend: 40% own model, 60% market consensus (cuts false edges)
MIN_EDGE = 0.03         # value bet threshold
DB = "football.db"
app = FastAPI()

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS matches(id INTEGER PRIMARY KEY, competition TEXT, season INT,
        utc_date TEXT, status TEXT, home_team TEXT, away_team TEXT, home_goals INT, away_goals INT)""")
    c.execute("CREATE TABLE IF NOT EXISTS teams(name TEXT PRIMARY KEY, crest TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS odds(event_id TEXT, league TEXT, commence TEXT, home TEXT,
        away TEXT, book TEXT, oh REAL, od REAL, oa REAL, ts TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS kenya(home TEXT, away TEXT, commence TEXT, book TEXT,
        oh REAL, od REAL, oa REAL, ts TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bets(id INTEGER PRIMARY KEY AUTOINCREMENT, placed TEXT, fixture TEXT,
        pick TEXT, site TEXT, odds REAL, stake REAL, result TEXT DEFAULT 'pending')""")

_NOW = {"t": 0.0, "dt": None, "src": "computer"}
def web_now():
    """Current UTC time read from the web (HTTP Date header), cached 10 min; falls back to the computer clock."""
    if _NOW["dt"] and time.time() - _NOW["t"] < 600:
        return _NOW["dt"] + timedelta(seconds=time.time() - _NOW["t"]), _NOW["src"]
    for url in ("https://www.cloudflare.com", "https://www.google.com", "https://api.football-data.org"):
        try:
            d = email.utils.parsedate_to_datetime(requests.head(url, timeout=5).headers["Date"])
            _NOW.update(t=time.time(), dt=d, src="web"); return d, "web"
        except Exception: continue
    _NOW.update(t=time.time(), dt=datetime.now(timezone.utc), src="computer")
    return _NOW["dt"], "computer"

# ---------- ingestion ----------
@app.post("/api/refresh/matches")
def refresh_matches():
    if not FD_KEY: raise HTTPException(400, "Set FOOTBALL_DATA_KEY first.")
    c = db(); n = 0; skipped = []
    for lg in LEAGUES:
        for s in SEASONS:
            r = requests.get(f"https://api.football-data.org/v4/competitions/{lg}/matches",
                             headers={"X-Auth-Token": FD_KEY}, params={"season": s}, timeout=30)
            if r.status_code in (403, 404): skipped.append(f"{lg} {s}"); time.sleep(6.5); continue
            if r.status_code != 200: raise HTTPException(502, f"football-data.org {r.status_code}: {r.text[:120]}")
            for m in r.json()["matches"]:
                ft = m["score"]["fullTime"]
                c.execute("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?)",
                          (m["id"], lg, s, m["utcDate"], m["status"], m["homeTeam"]["name"],
                           m["awayTeam"]["name"], ft["home"], ft["away"])); n += 1
                for side in ("homeTeam", "awayTeam"):
                    c.execute("INSERT OR REPLACE INTO teams VALUES (?,?)", (m[side]["name"], m[side].get("crest")))
            c.commit(); time.sleep(6.5)   # free tier: 10 requests/minute
    return {"matches": n, "skipped": skipped}

@app.post("/api/refresh/odds")
def refresh_odds():
    if not ODDS_KEY: raise HTTPException(400, "Set ODDS_API_KEY first.")
    rows, ts = [], datetime.now(timezone.utc).isoformat()
    for lg, sk in LEAGUES.items():
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sk}/odds", timeout=30,
                         params=dict(apiKey=ODDS_KEY, regions="eu,uk", markets="h2h", oddsFormat="decimal"))
        if r.status_code != 200: raise HTTPException(502, f"The Odds API {r.status_code}: {r.text[:120]}")
        for ev in r.json():
            for bk in ev["bookmakers"]:
                m = next((m for m in bk["markets"] if m["key"] == "h2h"), None)
                if not m: continue
                o = {x["name"]: x["price"] for x in m["outcomes"]}
                if ev["home_team"] in o and ev["away_team"] in o and "Draw" in o:
                    rows.append((ev["id"], lg, ev["commence_time"], ev["home_team"], ev["away_team"],
                                 bk["title"], o[ev["home_team"]], o["Draw"], o[ev["away_team"]], ts))
    c = db(); c.execute("DELETE FROM odds"); c.executemany("INSERT INTO odds VALUES (?,?,?,?,?,?,?,?,?,?)", rows); c.commit()
    return {"odds_rows": len(rows)}

@app.post("/api/refresh/kenya")
def refresh_kenya():
    """Betika and Odibets 1X2 odds from OddsPapi (free tier: 250 requests/month; this uses 3 per click)."""
    if not OP_KEY: raise HTTPException(400, "Set ODDSPAPI_KEY first.")
    def get(path, **p):
        p["apiKey"] = OP_KEY; time.sleep(1)
        r = requests.get(f"https://api.oddspapi.io/v4/{path}", params=p, timeout=60)
        if r.status_code != 200: raise HTTPException(502, f"OddsPapi {r.status_code}: {r.text[:120]}")
        return r.json()
    today = datetime.now(timezone.utc).date(); ids = set(OP_TOURN.values())
    fx = {f["fixtureId"]: f for f in get("fixtures", **{"sportId": 10, "from": today.isoformat(),
          "to": (today + timedelta(days=8)).isoformat()}) if f.get("tournamentId") in ids}
    rows, ts = [], datetime.now(timezone.utc).isoformat()
    skipped = []
    for slug, title in KE_BOOKS.items():
        try: evs = get("odds-by-tournaments", bookmaker=slug, tournamentIds=",".join(map(str, sorted(ids))))
        except HTTPException as ex:
            if "400" in str(ex.detail): skipped.append(slug); continue   # bookmaker not offered by the API
            raise
        for ev in evs:
            f = fx.get(ev.get("fixtureId"), {})
            h = f.get("participant1Name") or ev.get("participant1Name"); a = f.get("participant2Name") or ev.get("participant2Name")
            try: oc = ev["bookmakerOdds"][slug]["markets"]["101"]["outcomes"]
            except (KeyError, TypeError): continue
            pr = [(oc.get(k, {}).get("players", {}).get("0") or {}).get("price") for k in ("101", "102", "103")]
            if h and a and all(pr): rows.append((h, a, ev["startTime"], title, pr[0], pr[1], pr[2], ts))
    c = db(); c.execute("DELETE FROM kenya"); c.executemany("INSERT INTO kenya VALUES (?,?,?,?,?,?,?,?)", rows); c.commit()
    return {"kenya_rows": len(rows), "fixtures_seen": len(fx), "not_available": skipped}

# ---------- model ----------
DROP = {"fc","afc","cf","ac","as","ssc","us","sv","fsv","vfb","vfl","rc","rcd","ud","sc","1","04","05","1899","1846","calcio","de","club"}
def norm(s):
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s.lower()) if ch.isalnum() or ch == " ")
    return " ".join(w for w in s.split() if w not in DROP)

def match_team(name, cands):
    n = norm(name)
    for c in cands:
        if n == c or (len(n) > 3 and (n in c or c in n)): return cands[c]
    m = difflib.get_close_matches(n, list(cands), 1, 0.6)
    return cands[m[0]] if m else None

HALF_LIFE = 220     # days: how quickly older matches fade
RHO = -0.08         # Dixon-Coles correction for low scores (0-0, 1-0, 0-1, 1-1)
K_SHRINK = 3.0      # pseudo-goals pulling teams with thin data toward average
MODEL_CACHE = {}

def fit(rows, asof):
    """Joint Poisson fit: goals = mu * attack[scorer] * defence[conceder] * (home advantage if home).
    Strengths are adjusted for opponent quality, and recent matches count more."""
    G = []
    for r in rows:
        age = (asof - datetime.fromisoformat(r["utc_date"].replace("Z", "+00:00"))).days
        if age >= 0:
            G.append((r["home_team"], r["away_team"], r["home_goals"], r["away_goals"], 0.5 ** (age / HALF_LIFE)))
    if len(G) < 60: return None
    T = {t for g in G for t in g[:2]}
    att = dict.fromkeys(T, 1.0); dfn = dict.fromkeys(T, 1.0); mu, adv = 1.2, 1.25
    for _ in range(25):
        na, da, nd, dd = ({t: 0.0 for t in T} for _ in range(4))
        for h, a, hg, ag, w in G:
            na[h] += w*hg; da[h] += w*mu*adv*dfn[a]
            na[a] += w*ag; da[a] += w*mu*dfn[h]
            nd[h] += w*ag; dd[h] += w*mu*att[a]
            nd[a] += w*hg; dd[a] += w*mu*adv*att[h]
        att = {t: (na[t] + K_SHRINK) / (da[t] + K_SHRINK) for t in T}
        dfn = {t: (nd[t] + K_SHRINK) / (dd[t] + K_SHRINK) for t in T}
        ma, md = sum(att.values()) / len(T), sum(dfn.values()) / len(T)
        att = {t: v / ma for t, v in att.items()}; dfn = {t: v / md for t, v in dfn.items()}
        mu = sum(w*(hg+ag) for h, a, hg, ag, w in G) / sum(w*(adv*att[h]*dfn[a] + att[a]*dfn[h]) for h, a, hg, ag, w in G)
        adv = sum(w*hg for h, a, hg, ag, w in G) / sum(w*mu*att[h]*dfn[a] for h, a, hg, ag, w in G)
    return dict(mu=mu, adv=adv, att=att, dfn=dfn)

def get_model(lg):
    rows = db().execute("SELECT utc_date,home_team,away_team,home_goals,away_goals FROM matches WHERE competition=? "
                        "AND status='FINISHED' AND home_goals IS NOT NULL", (lg,)).fetchall()
    key = (lg, len(rows))
    if key not in MODEL_CACHE: MODEL_CACHE[key] = fit(rows, datetime.now(timezone.utc))
    return MODEL_CACHE[key]

def pois(k, l): return math.exp(-l) * l**k / math.factorial(k)

def predict(m, h, a):
    lh = m["mu"]*m["adv"]*m["att"][h]*m["dfn"][a]; la = m["mu"]*m["att"][a]*m["dfn"][h]
    tau = {(0, 0): 1 - lh*la*RHO, (0, 1): 1 + lh*RHO, (1, 0): 1 + la*RHO, (1, 1): 1 - RHO}
    ph = pd = pa = o25 = tot = 0
    for i in range(9):
        for j in range(9):
            p = pois(i, lh) * pois(j, la) * tau.get((i, j), 1); tot += p
            if i > j: ph += p
            elif i == j: pd += p
            else: pa += p
            if i + j > 2: o25 += p
    return [ph/tot, pd/tot, pa/tot], round(lh, 2), round(la, 2), round(o25/tot, 3)

def rest_days(c, team, kickoff):
    d = c.execute("SELECT MAX(utc_date) d FROM matches WHERE (home_team=? OR away_team=?) AND status='FINISHED' AND utc_date<?",
                  (team, team, kickoff)).fetchone()["d"]
    if not d: return None
    return (datetime.fromisoformat(kickoff.replace("Z", "+00:00")) - datetime.fromisoformat(d.replace("Z", "+00:00"))).days

# ---------- picks ----------
def same_team(a, b):
    x, y = norm(a), norm(b)
    return x == y or (len(x) > 3 and len(y) > 3 and (x in y or y in x)) or difflib.SequenceMatcher(None, x, y).ratio() >= 0.75

def kenya_odds(rows, home, away, commence):
    out = {}; t0 = datetime.fromisoformat(commence.replace("Z", "+00:00"))
    for r in rows:
        try: gap = abs((datetime.fromisoformat(r["commence"].replace("Z", "+00:00")) - t0).total_seconds())
        except Exception: continue
        if gap < 43200 and same_team(r["home"], home) and same_team(r["away"], away):
            out[r["book"]] = [r["oh"], r["od"], r["oa"]]
    return out

def form(c, lg, team):
    rows = c.execute("SELECT home_team,home_goals,away_goals FROM matches WHERE competition=? AND status='FINISHED' "
                     "AND (home_team=? OR away_team=?) ORDER BY utc_date DESC LIMIT 5", (lg, team, team)).fetchall()
    out = ""
    for r in rows:
        gf, ga = (r["home_goals"], r["away_goals"]) if r["home_team"] == team else (r["away_goals"], r["home_goals"])
        out += "W" if gf > ga else "D" if gf == ga else "L"
    return out[::-1]   # oldest to newest

@app.get("/api/picks")
def picks():
    c = db(); now_dt, src = web_now(); now = now_dt.isoformat()
    rows = c.execute("SELECT * FROM odds WHERE commence > ? ORDER BY commence", (now,)).fetchall()
    ke_rows = c.execute("SELECT * FROM kenya").fetchall()
    events = {}
    for r in rows: events.setdefault(r["event_id"], []).append(r)
    models, teams, out = {}, {}, []
    crests = {r["name"]: r["crest"] for r in c.execute("SELECT name, crest FROM teams")}
    for eid, rs in events.items():
        r0 = rs[0]; lg = r0["league"]
        if lg not in models:
            models[lg] = get_model(lg)
            names = [t["home_team"] for t in c.execute("SELECT DISTINCT home_team FROM matches WHERE competition=?", (lg,))]
            teams[lg] = {norm(n): n for n in names}
        best = [max(rs, key=lambda r: r[k]) for k in ("oh", "od", "oa")]
        bo = [best[0]["oh"], best[1]["od"], best[2]["oa"]]
        fair = [0, 0, 0]; W = sum(3 if "pinnacle" in r["book"].lower() else 1 for r in rs)   # sharp books count 3x
        for r in rs:
            wt = 3 if "pinnacle" in r["book"].lower() else 1
            inv = [1/r["oh"], 1/r["od"], 1/r["oa"]]; s = sum(inv)
            for i in range(3): fair[i] += inv[i]/s*wt/W
        probs, xg, mp, tag = fair, None, None, "market"
        h = match_team(r0["home"], teams[lg]); a = match_team(r0["away"], teams[lg])
        if models[lg] and h in models[lg]["att"] and a in models[lg]["att"] and h != a:
            mp, lh, la, o25 = predict(models[lg], h, a)
            probs = [MODEL_WEIGHT*mp[i] + (1-MODEL_WEIGHT)*fair[i] for i in range(3)]
            xg, tag = [lh, la, o25], "model+market"
        edges = [probs[i]*bo[i] - 1 for i in range(3)]
        k = max(range(3), key=lambda i: edges[i])
        inv_sum = sum(1/o for o in bo)
        out.append(dict(league=lg, home=r0["home"], away=r0["away"], commence=r0["commence"], books=len(rs),
            kenya=kenya_odds(ke_rows, r0["home"], r0["away"], r0["commence"]),
            home_crest=crests.get(h), away_crest=crests.get(a), probs=probs, model=mp, xg=xg, basis=tag, best=[dict(odds=bo[i], book=best[i]["book"]) for i in range(3)],
            pick=dict(side=["Home","Draw","Away"][k], edge=edges[k], odds=bo[k], book=best[k]["book"],
                      stake_pct=max(0, 0.25*edges[k]/(bo[k]-1)*100), score=round(edges[k]*math.sqrt(probs[k])*100, 2),
                      strength=("Strong" if mp and mp[k] > fair[k] and edges[k] >= 0.06 else
                                "Good" if mp and mp[k] > fair[k] and edges[k] >= 0.04 else "Lean")) if edges[k] >= MIN_EDGE else None,
            form=[form(c, lg, h) if h else "", form(c, lg, a) if a else ""],
            rest=[rest_days(c, h, r0["commence"]) if h else None, rest_days(c, a, r0["commence"]) if a else None],
            goals_lean=("Over 2.5" if xg and xg[2] >= 0.62 else "Under 2.5" if xg and xg[2] <= 0.38 else None),
            arb=dict(profit=(1/inv_sum-1)*100, stakes=[round(100/o/inv_sum, 2) for o in bo]) if inv_sum < 1 else None))
    return dict(events=out, odds_updated=rows[0]["ts"] if rows else None, now=now, time_source=src)

@app.get("/api/teams")
def team_table(league: str = "PL"):
    c = db(); season = c.execute("SELECT MAX(season) m FROM matches WHERE competition=?", (league,)).fetchone()["m"]
    rows = c.execute("SELECT * FROM matches WHERE competition=? AND season=? AND status='FINISHED' ORDER BY utc_date",
                     (league, season)).fetchall(); T = {}; model = get_model(league)
    for r in rows:
        for t, gf, ga in ((r["home_team"], r["home_goals"], r["away_goals"]), (r["away_team"], r["away_goals"], r["home_goals"])):
            x = T.setdefault(t, dict(team=t, p=0, w=0, d=0, l=0, gf=0, ga=0, form=""))
            x["p"] += 1; x["gf"] += gf; x["ga"] += ga
            res = "W" if gf > ga else "D" if gf == ga else "L"; x[res.lower()] += 1; x["form"] += res
    crests = {r["name"]: r["crest"] for r in c.execute("SELECT name, crest FROM teams")}
    for x in T.values():
        x["crest"] = crests.get(x["team"])
        x["pts"] = 3*x["w"] + x["d"]; x["form"] = x["form"][-5:]
        ok = bool(model) and x["team"] in model["att"]
        x["attack"] = round(model["att"][x["team"]], 2) if ok else None
        x["defense"] = round(model["dfn"][x["team"]], 2) if ok else None   # lower = tighter defence
    return sorted(T.values(), key=lambda x: (-x["pts"], -(x["gf"]-x["ga"])))

EDG = [(0, .4, "Under 40%"), (.4, .5, "40-50%"), (.5, .6, "50-60%"), (.6, .7, "60-70%"), (.7, 1.01, "70%+")]

@app.get("/api/backtest")
def backtest(league: str = "PL", days: int = 365):
    """Replays past matches: for each 2-week block, fit only on earlier matches, then predict the block."""
    rows = [dict(r) for r in db().execute("SELECT utc_date,home_team,away_team,home_goals,away_goals FROM matches "
            "WHERE competition=? AND status='FINISHED' AND home_goals IS NOT NULL ORDER BY utc_date", (league,))]
    if len(rows) < 300: raise HTTPException(400, "Not enough past results. Click Update results first.")
    for r in rows: r["dt"] = datetime.fromisoformat(r["utc_date"].replace("Z", "+00:00"))
    last = rows[-1]["dt"]; cs = last - timedelta(days=days)
    n = hit = home_hit = 0; brier = ll = 0.0; freq = [0, 0, 0]; bins = {}
    while cs <= last:
        ce = cs + timedelta(days=14)
        test = [r for r in rows if cs <= r["dt"] < ce]
        m = fit([r for r in rows if r["dt"] < cs], cs) if test else None
        for r in (test if m else []):
            if r["home_team"] not in m["att"] or r["away_team"] not in m["att"]: continue
            p = predict(m, r["home_team"], r["away_team"])[0]
            k = 0 if r["home_goals"] > r["away_goals"] else 1 if r["home_goals"] == r["away_goals"] else 2
            top = max(p); ok = p.index(top) == k
            n += 1; freq[k] += 1; hit += ok; home_hit += k == 0
            brier += sum((p[i] - (i == k)) ** 2 for i in range(3)); ll += -math.log(max(p[k], 1e-9))
            b = bins.setdefault(next(l for lo, hi, l in EDG if lo <= top < hi), [0, 0, 0.0])
            b[0] += 1; b[1] += ok; b[2] += top
        cs = ce
    if not n: raise HTTPException(400, "No matches could be tested yet.")
    return dict(matches=n, accuracy=hit/n, baseline_accuracy=home_hit/n, brier=brier/n, logloss=ll/n,
                baseline_logloss=-sum(f/n*math.log(f/n) for f in freq if f),
                calibration=[dict(range=l, n=b[0], predicted=b[2]/b[0], actual=b[1]/b[0]) for _, _, l in EDG if (b := bins.get(l))])

# ---------- my bets ----------
class Bet(BaseModel):
    fixture: str
    pick: str = ""
    site: str
    odds: float
    stake: float

@app.get("/api/bets")
def list_bets(): return [dict(r) for r in db().execute("SELECT * FROM bets ORDER BY id DESC")]

@app.post("/api/bets")
def add_bet(b: Bet):
    if b.odds <= 1 or b.stake <= 0: raise HTTPException(400, "Odds must be above 1 and stake above 0.")
    c = db(); c.execute("INSERT INTO bets(placed,fixture,pick,site,odds,stake) VALUES (?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), b.fixture, b.pick, b.site, b.odds, b.stake)); c.commit()
    return {"ok": True}

@app.post("/api/bets/{bid}/{result}")
def settle_bet(bid: int, result: str):
    if result not in ("won", "lost", "void"): raise HTTPException(400, "Result must be won, lost or void.")
    c = db(); c.execute("UPDATE bets SET result=? WHERE id=?", (result, bid)); c.commit(); return {"ok": True}

@app.delete("/api/bets/{bid}")
def delete_bet(bid: int):
    c = db(); c.execute("DELETE FROM bets WHERE id=?", (bid,)); c.commit(); return {"ok": True}

@app.get("/")
def index(): return FileResponse("static/index.html")