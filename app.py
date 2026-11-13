    if not S:
        raise HTTPException(400, "No matches could be tested yet.")

    def combo(wf, whp):
        t1 = t2 = 0
        ll = 0.0
        for lh, la, hf, hc, hn, af, ac, an, n, hv, av, hg, ag in S:
            f = wf if hn >= 3 and an >= 3 else 0.0
            w = min(4*whp, whp*n)
            x = (
                (1-f-w)*lh +
                f*((hf+ac)/2 if f else lh) +
                w*(hv if n else lh)
            )
            y = (
                (1-f-w)*la +
                f*((af+hc)/2 if f else la) +
                w*(av if n else la)
            )
            px, py = pmf(x), pmf(y)
            tau = {
                (0, 0): 1-x*y*RHO,
                (0, 1): 1+x*RHO,
                (1, 0): 1+y*RHO,
                (1, 1): 1-RHO
            }
            tot = (
                sum(px)*sum(py) +
                sum(px[i]*py[j]*(v-1) for (i, j), v in tau.items())
            )
            top = heapq.nlargest(
                2,
                (
                    (px[i]*py[j]*tau.get((i, j), 1)/tot, i, j)
                    for i in range(8) for j in range(8)
                )
            )
            t1 += (top[0][1], top[0][2]) == (hg, ag)
            t2 += (hg, ag) in (
                (top[0][1], top[0][2]),
                (top[1][1], top[1][2])
            )
            p = (
                px[hg]*py[ag]*tau.get((hg, ag), 1)/tot
                if hg < 8 and ag < 8 else 1e-4
            )
            ll += math.log(max(p, 1e-9))

        N = len(S)
        return dict(
            wf=wf, whp=whp,
            top1=t1/N, top2=t2/N,
            logscore=ll/N
        )

    res = sorted(
        (
            combo(a, b)
            for a in (0, .1, .2, .3, .4)
            for b in (0, .025, .05, .075, .1)
        ),
        key=lambda d: -d["logscore"]
    )
    return dict(
        matches=len(S),
        results=res[:5],
        best=res[0],
        current=combo(SET["wf"], SET["whp"]),
        model_only=combo(0, 0),
        in_use=dict(SET)
    )


class Weights(BaseModel):
    wf: float
    whp: float


@app.post("/api/settings/weights")
def set_weights(w: Weights):
    if not (0 <= w.wf <= 0.5 and 0 <= w.whp <= 0.125):
        raise HTTPException(400, "Weights out of range.")
    c = db()
    for k, v in (("wf", w.wf), ("whp", w.whp)):
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, v))
    c.commit()
    load_settings()
    return dict(SET)


@app.get("/")
def index():
    index_file = os.path.join(BASE_DIR, "index.html")
    if os.path.isfile(index_file):
        return FileResponse(index_file, media_type="text/html")
    return {
        "app": "VentiEdge",
        "status": "online",
        "message": "API is running. Open /docs to see available endpoints.",
    }


@app.get("/api/health")
def health():
    return {
        "app": "VentiEdge",
        "status": "ok",
        "api_keys_configured": {
            "football_data": bool(FD_KEY),
            "odds_api": bool(ODDS_KEY),
            "oddspapi": bool(OP_KEY),
        },
    }
