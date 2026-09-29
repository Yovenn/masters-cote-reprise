import os, re, statistics, requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def score_result(r, brand, model, year, target_km):
    title = r.get("title", "")
    snippet = r.get("snippet", "")
    text = f"{title} {snippet}".lower()

    score = 0

    if norm(brand) in norm(title):
        score += 25
    elif norm(brand) in norm(text):
        score += 10

    model_norm = norm(model)
    title_norm = norm(title)
    text_norm = norm(text)

    # Le modèle exact doit apparaître dans le titre
if model_norm in title_norm:
    score += 55
else:
    return 0

    if str(year) in title:
        score += 15
    elif str(year) in text:
        score += 5

    kms = [
        int(re.sub(r"\D", "", x))
        for x in re.findall(
            r"\b\d{1,3}(?:[ .]\d{3})?\s*km\b",
            text
        )
    ]

    if kms:
        d = min(abs(k - target_km) for k in kms)
        score += max(0, 10 - min(10, d / 5000))

    return score

def parse_price_km(r):
    text = f"{r.get('title','')} {r.get('snippet','')}"
    prices = []

    for m in re.findall(
        r"(\d{2,3}(?:[ .]\d{3})+|\d{4,6})\s*€", text
    ):
        try:
            v = int(m.replace(" ", "").replace(".", ""))
            if 10000 <= v <= 150000:
                prices.append(v)
        except:
            pass

    kms = []

    for m in re.findall(
        r"(\d{1,3}(?:[ .]\d{3})+|\d{3,6})\s*km",
        text.lower()
    ):
        try:
            v = int(m.replace(" ", "").replace(".", ""))
            if 0 <= v <= 300000:
                kms.append(v)
        except:
            pass

    return (
        prices[0] if prices else None,
        kms[0] if kms else None
    )

@app.get("/")
def home():
    return send_from_directory("static", "index.html")

@app.post("/api/cote")
def cote():
    data = request.get_json(force=True)

    brand = data.get("brand", "").strip()
    model = data.get("model", "").strip()
    year = int(data.get("year"))
    km = int(data.get("km"))

    if not SERPER_API_KEY:
        return jsonify({
            "error": "SERPER_API_KEY manquante sur le serveur."
        }), 500

    queries = [
        f'"{brand} {model}" {year} camping-car occasion',
        f'"{brand} {model}" {year} {km} km',
        f'site:leboncoin.fr "{brand} {model}" {year}',
        f'site:paruvendu.fr "{brand} {model}" {year}',
        f'site:hunyvers.com "{brand} {model}" {year}',
    ]

    results = []

    for q in queries:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "q": q,
                "gl": "fr",
                "hl": "fr",
                "num": 10
            },
            timeout=20
        )

        resp.raise_for_status()

        for x in resp.json().get("organic", []):
            results.append(x)

    uniq = {}

    for r in results:
        if r.get("link"):
            uniq[r["link"]] = r

    rows = []

    for r in uniq.values():
        price, rkm = parse_price_km(r)
        sc = score_result(r, brand, model, year, km)

        if price and sc >= 60:
            adj = price

            if rkm is not None:
                adj = price + ((rkm - km) * 0.12)

            rows.append({
                "title": r.get("title"),
                "url": r.get("link"),
                "snippet": r.get("snippet"),
                "price": price,
                "km": rkm,
                "adjusted": round(adj),
                "score": round(sc)
            })

    rows.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    rows = rows[:15]

    vals = [
        x["adjusted"]
        for x in rows
        if x["adjusted"]
    ]

    if len(vals) < 3:
        return jsonify({
            "status": "insufficient",
            "comparables": rows,
            "message": "Moins de 3 comparables suffisamment fiables."
        })

    med = statistics.median(vals)

    if len(vals) >= 5:
        filt = [
            v for v in vals
            if med * 0.88 <= v <= med * 1.12
        ]
    else:
        filt = vals

    market = round(
        statistics.median(filt) / 100
    ) * 100

    return jsonify({
        "status": "ok",
        "comparables": rows,
        "market": market,
        "trade": max(0, market - 8000),
        "confidence": (
            "Bonne" if len(filt) >= 7
            else "Correcte" if len(filt) >= 5
            else "Faible"
        ),
        "count": len(filt)
    })

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080"))
    )
