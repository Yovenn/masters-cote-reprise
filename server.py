import os, re, statistics, json, requests
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
DICA_EDITION_YEAR = 2026
DICA_REF_KM_PER_YEAR = 12000
DICA_OVER_KM_RATE = 0.20
DICA_UNDER_KM_RATE = 0.10
DICA_REPRISE_FACTOR = 0.85
MASTERS_FRAIS = 8000
BASE_DIR = os.path.dirname(__file__)
with open(os.path.join(BASE_DIR, "dica32_camping_cars.json"), encoding="utf-8") as f:
    _dica = json.load(f)
DICA = []
for row in _dica["records"]:
    if isinstance(row, list):
        year, brand, gamme, model, motorisation, type_, neuf, revente, reprise, page = row
        DICA.append({
            "year": year, "brand": brand, "brand_norm": re.sub(r"[^a-z0-9]","",str(brand).lower()),
            "gamme": gamme, "model": model, "model_norm": re.sub(r"[^a-z0-9]","",str(model).lower()),
            "motorisation": motorisation, "type": type_, "neuf": neuf,
            "revente": revente, "reprise": reprise, "page": page
        })
    else:
        row["brand_norm"] = re.sub(r"[^a-z0-9]","",(row.get("brand","") or "").lower())
        row["model_norm"] = re.sub(r"[^a-z0-9]","",(row.get("model","") or "").lower())
        DICA.append(row)
NEW_WORDS=("neuf","neuve","0 km","0km","jamais immatriculé","jamais immatricule","véhicule neuf","vehicule neuf","stock neuf","déstockage","destockage")
AGGREGATOR_WORDS=("page 2","page 3","page 4","page 5","tous les véhicules","toutes les annonces","résultats de recherche","resultats de recherche","annonces similaires")
def norm(s): return re.sub(r"[^a-z0-9]","",(s or "").lower())
def clean_num(v): return int(re.sub(r"[^0-9]","",str(v)))
def extract_kms(text):
    out=[]
    for m in re.finditer(r"\b(\d{1,3}(?:[ .]\d{3})|\d{3,6})\s*km\b",text.lower()):
        try:
            v=clean_num(m.group(1))
            if 0<=v<=300000: out.append(v)
        except ValueError: pass
    return out
def extract_prices(text):
    out=[]
    for p in [r"(\d{2,3}(?:[ .]\d{3})+|\d{4,6})\s*€",r"€\s*(\d{2,3}(?:[ .]\d{3})+|\d{4,6})"]:
        for m in re.finditer(p,text):
            try:
                v=clean_num(m.group(1))
                if 10000<=v<=150000 and v not in out: out.append(v)
            except ValueError: pass
    return out
def parse_price_km(r):
    text=f"{r.get('title','')} {r.get('snippet','')}"
    ps=extract_prices(text); ks=extract_kms(text)
    return (ps[0] if ps else None),(ks[0] if ks else None)
def extract_hp(text):
    out=[]
    for m in re.finditer(r'\b(\d{2,3})\s*(?:ch|cv|chevaux)\b', (text or '').lower()):
        v=int(m.group(1))
        if 50<=v<=500: out.append(v)
    return out
def motor_hp(motorisation):
    vals=extract_hp(motorisation)
    if vals:
        return vals[0]
    # Dans la DICA, certaines motorisations indiquent la puissance
    # en dernier nombre sans écrire "ch/cv" (ex. "2,2 l 120").
    m=re.search(r'(?<![0-9])([0-9]{2,3})\s*
def options_value(options):
    total=0
    details=[]
    for o in options or []:
        try:
            name=str(o.get("name","")).strip()
            price=float(o.get("price",0))
            year=int(o.get("year",0))
        except (TypeError,ValueError):
            continue
        if not name or price<=0 or year<1900 or year>DICA_EDITION_YEAR:
            continue
        age=DICA_EDITION_YEAR-year
        rate=0.50 if age<=5 else max(0,0.50-0.05*(age-5))
        value=round(price*rate)
        details.append({"name":name,"price":round(price),"year":year,"age":age,"rate":rate,"value":value})
        total+=value
    return total,details

def dica_matches(brand,model,year,km,hp=None,options_value_total=0):
    b,m=norm(brand),norm(model); ref=max(0,(DICA_EDITION_YEAR-year)*DICA_REF_KM_PER_YEAR); out=[]
    for r in DICA:
        if r["year"]!=year or r["brand_norm"]!=b: continue
        rm=r["model_norm"]; rg=norm(r.get("gamme",""))
        composite=norm(f"{r.get('gamme','')} {r.get('model','')}")
        if m==rm:
            model_ok=True
        elif m==composite:
            model_ok=True
        elif rg and rg in m and rm in m:
            model_ok=True
        elif not rg and (rm in m or m in rm):
            model_ok=True
        else:
            model_ok=False
        if not model_ok: continue
        rhp=motor_hp(r.get("motorisation",""))
        if hp is not None and rhp is not None and rhp != hp: continue
        if km>ref: corr=(km-ref)*DICA_OVER_KM_RATE; rev=r["revente"]-corr
        else: corr=(ref-km)*DICA_UNDER_KM_RATE; rev=r["revente"]+corr
        rev=round(rev)
        out.append({"year":r["year"],"horsepower":rhp,"brand":r["brand"],"gamme":r["gamme"],"model":r["model"],"motorisation":r["motorisation"],"type":r["type"],"neuf":r["neuf"],"revente":r["revente"],"reprise":r["reprise"],"reference_km":ref,"km_correction":round(corr),"revente_corrigee":rev,"reprise_corrigee":round(rev*DICA_REPRISE_FACTOR),"options_value":options_value_total,"revente_avec_options":round(rev+options_value_total),"reprise_avec_options":round(rev*DICA_REPRISE_FACTOR+options_value_total),"page":r["page"]})
    return out
def dica_near_matches(brand,model,year,km,hp=None,options_value_total=0):
    b,m=norm(brand),norm(model); ref=max(0,(DICA_EDITION_YEAR-year)*DICA_REF_KM_PER_YEAR); candidates=[]
    for r in DICA:
        if r["year"]!=year or r["brand_norm"]!=b: continue
        rm=r["model_norm"]
        if not (rm==m or rm in m or m in rm): continue
        rhp=motor_hp(r.get("motorisation",""))
        if hp is not None and rhp is not None and rhp != hp: continue
        if km>ref: corr=(km-ref)*DICA_OVER_KM_RATE; rev=r["revente"]-corr
        else: corr=(ref-km)*DICA_UNDER_KM_RATE; rev=r["revente"]+corr
        score=20
        if hp is not None and rhp==hp: score+=15
        if norm(r.get("gamme","")) and norm(r.get("gamme","")) in m: score+=10
        candidates.append((score,{"year":r["year"],"horsepower":rhp,"brand":r["brand"],"gamme":r["gamme"],"model":r["model"],"motorisation":r["motorisation"],"type":r["type"],"neuf":r["neuf"],"revente":r["revente"],"reprise":r["reprise"],"reference_km":ref,"km_correction":round(corr),"revente_corrigee":round(rev),"reprise_corrigee":round(rev*DICA_REPRISE_FACTOR),"page":r["page"]}))
    candidates.sort(key=lambda z:(z[0],z[1]["horsepower"]==hp if hp is not None else False),reverse=True)
    out=[]
    seen=set()
    for _,x in candidates:
        key=(x["gamme"],x["model"],x["motorisation"])
        if key not in seen: seen.add(key); out.append(x)
    return out[:5]

def model_match_score(text,model):
    raw=(text or "").lower(); model=(model or "").strip().lower()
    if not model:return 0
    if re.fullmatch(r"\d+",model): return 48 if re.search(rf"(?<!\d){re.escape(model)}(?!\d)",raw) else 0
    compact=norm(model); compact_text=norm(raw)
    if compact and compact in compact_text:return 48
    parts=[p for p in re.split(r"[\s/-]+",model) if len(p)>=2]
    return 48 if parts and all(p in raw for p in parts) else 0
def score_result(r,brand,model,year,target_km,hp=None):
    title=str(r.get("title","")); snippet=str(r.get("snippet","")); text=f"{title} {snippet}"; low=text.lower(); score=0
    if norm(brand) in norm(low): score+=25
    score+=model_match_score(low,model)
    if re.search(rf"\b{re.escape(str(year))}\b",low): score+=20
    ks=extract_kms(text)
    if ks:
        d=min(abs(k-target_km) for k in ks); score+=max(0,10-min(10,d/5000))
    if hp is not None:
        hps=extract_hp(text)
        if hp in hps: score+=15
    if norm(model) and norm(model) in norm(title): score+=5
    return score
def is_new(r):
    text=f"{r.get('title','')} {r.get('snippet','')}".lower()
    return any(w in text for w in NEW_WORDS) or bool(re.search(r"\b0\s*km\b",text))
def is_unavailable(r):
    text=f"{r.get('title','')} {r.get('snippet','')}".lower()
    return any(w in text for w in ("vendu","déjà vendu","deja vendu","indisponible","archivé","archive"))
def is_aggregation(r):
    text=f"{r.get('title','')} {r.get('snippet','')}".lower()
    return any(w in text for w in AGGREGATOR_WORDS)
def comparable_row(r,brand,model,year,target_km,hp=None):
    price,rkm=parse_price_km(r); score=score_result(r,brand,model,year,target_km,hp)
    if not price or score<65 or is_new(r) or is_unavailable(r): return None
    if is_aggregation(r): score-=10
    if score<65: return None
    adjusted=price; km_adjustment=0
    if rkm is not None:
        if rkm>target_km: km_adjustment=round((rkm-target_km)*DICA_OVER_KM_RATE)
        elif rkm<target_km: km_adjustment=-round((target_km-rkm)*DICA_UNDER_KM_RATE)
        adjusted=price+km_adjustment
    return {"title":r.get("title"),"url":r.get("link"),"snippet":r.get("snippet"),"price":price,"km":rkm,"adjusted":round(adjusted),"km_adjustment":km_adjustment,"score":round(score),"source":r.get("source","")}
@app.get("/")
def home(): return send_from_directory("static","index.html")
@app.post("/api/cote")
def cote():
    data=request.get_json(force=True); brand=str(data.get("brand","")).strip(); model=str(data.get("model","")).strip()
    cv_raw=str(data.get("cv","")).strip()
    hp_raw=str(data.get("hp","")).strip()
    try: year=int(data.get("year")); km=int(data.get("km")); cv=int(cv_raw) if cv_raw else None; hp=int(hp_raw) if hp_raw else None
    except (TypeError,ValueError): return jsonify({"error":"Année et kilométrage invalides."}),400
    if not brand or not model or year<2010 or km<0 or (cv is not None and (cv<1 or cv>50)) or (hp is not None and (hp<50 or hp>500)): return jsonify({"error":"Merci de renseigner des informations valides."}),400
    if not SERPER_API_KEY: return jsonify({"error":"SERPER_API_KEY manquante sur le serveur."}),500
    accessories=data.get("accessories") or []
    options_total, option_details=options_value(accessories)
    dica=dica_matches(brand,model,year,km,hp,options_total)
    hp_query=f" {hp} ch" if hp else ""
    queries=[f'"{brand} {model}" {year}{hp_query} camping-car occasion',f'"{brand} {model}" {year}{hp_query} "{km} km" occasion',f'"{brand} {model}" {year} camping car occasion prix',f'site:leboncoin.fr "{brand} {model}" {year}',f'site:paruvendu.fr "{brand} {model}" {year}',f'site:hunyvers.com "{brand} {model}" {year}',f'site:camping-car.com "{brand} {model}" {year}']
    results=[]
    for q in queries:
        try:
            resp=requests.post("https://google.serper.dev/search",headers={"X-API-KEY":SERPER_API_KEY,"Content-Type":"application/json"},json={"q":q,"gl":"fr","hl":"fr","num":10},timeout=20); resp.raise_for_status()
            for item in resp.json().get("organic",[]): item["source"]=q; results.append(item)
        except requests.RequestException: continue
    uniq={r["link"]:r for r in results if r.get("link")}
    dedup={}
    for r in uniq.values():
        key=(norm(r.get("title","")),parse_price_km(r)[0],parse_price_km(r)[1])
        if key[0]: dedup[key]=r
    rows=[]; context=[]
    for r in dedup.values():
        row=comparable_row(r,brand,model,year,km,hp)
        if not row: continue
        (context if row["km"] is None else rows).append(row)
    rows.sort(key=lambda x:(x["score"],-abs((x["km"] or km)-km)),reverse=True); context.sort(key=lambda x:x["score"],reverse=True)
    primary=rows[:15]; values=[x["adjusted"] for x in primary if x["adjusted"]]
    if len(values)<3: primary=(primary+context)[:15]; values=[x["adjusted"] for x in primary if x["adjusted"]]
    if len(values)<3: return jsonify({"status":"insufficient","comparables":primary,"context":context[:5],"dica":dica,"message":"Moins de 3 comparables suffisamment fiables ont été trouvés sur le marché actuel."})
    med=statistics.median(values)
    filtered_values=list(values)
    excluded_values=[]
    if len(values)>=5:
        deviations=[abs(v-med) for v in values]
        mad=statistics.median(deviations)
        if mad>0:
            limit=3*mad
            filtered_values=[v for v in values if abs(v-med)<=limit]
        else:
            filtered_values=[v for v in values if med*.90<=v<=med*1.10]
        if len(filtered_values)<3:
            filtered_values=list(values)
        excluded_values=[v for v in values if v not in filtered_values]
    filtered=filtered_values
    market=round(statistics.median(filtered)/100)*100
    market_low=round(min(filtered)/100)*100
    market_high=round(max(filtered)/100)*100
    dispersion=statistics.median([abs(v-statistics.median(filtered)) for v in filtered]) / max(1,statistics.median(filtered))
    confidence="Bonne" if len(filtered)>=7 and dispersion<=0.10 else "Correcte" if len(filtered)>=5 else "Faible"
    for row in primary:
        row["retenu_dans_cote"]=row["adjusted"] in filtered
        row["atypique"]=row["adjusted"] in excluded_values
    search_time=datetime.now().astimezone().isoformat(timespec="minutes")
    return jsonify({"status":"ok","comparables":primary,"context":context[:5],"market":market,"market_low":market_low,"market_high":market_high,"search_time":search_time,"fiscal_cv":cv,"horsepower":hp,"trade":max(0,market-MASTERS_FRAIS),"masters_frais":MASTERS_FRAIS,"confidence":confidence,"count":len(filtered),"excluded_count":len(excluded_values),"dica":dica,"dica_near":dica_near_matches(brand,model,year,km,hp,options_total) if not dica else [],"accessories":option_details,"accessories_value":options_total,"dica_reference_km":max(0,(DICA_EDITION_YEAR-year)*DICA_REF_KM_PER_YEAR),"dica_edition":"Cote Officielle de l’Occasion n°32 — janvier à avril 2026"})
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
, (motorisation or '').lower())
    if m:
        v=int(m.group(1))
        if 50<=v<=500:
            return v
    return None
def options_value(options):
    total=0
    details=[]
    for o in options or []:
        try:
            name=str(o.get("name","")).strip()
            price=float(o.get("price",0))
            year=int(o.get("year",0))
        except (TypeError,ValueError):
            continue
        if not name or price<=0 or year<1900 or year>DICA_EDITION_YEAR:
            continue
        age=DICA_EDITION_YEAR-year
        rate=0.50 if age<=5 else max(0,0.50-0.05*(age-5))
        value=round(price*rate)
        details.append({"name":name,"price":round(price),"year":year,"age":age,"rate":rate,"value":value})
        total+=value
    return total,details

def dica_matches(brand,model,year,km,hp=None,options_value_total=0):
    b,m=norm(brand),norm(model); ref=max(0,(DICA_EDITION_YEAR-year)*DICA_REF_KM_PER_YEAR); out=[]
    for r in DICA:
        if r["year"]!=year or r["brand_norm"]!=b: continue
        rm=r["model_norm"]; rg=norm(r.get("gamme",""))
        composite=norm(f"{r.get('gamme','')} {r.get('model','')}")
        if m==rm:
            model_ok=True
        elif m==composite:
            model_ok=True
        elif rg and rg in m and rm in m:
            model_ok=True
        elif not rg and (rm in m or m in rm):
            model_ok=True
        else:
            model_ok=False
        if not model_ok: continue
        rhp=motor_hp(r.get("motorisation",""))
        if hp is not None and rhp is not None and rhp != hp: continue
        if km>ref: corr=(km-ref)*DICA_OVER_KM_RATE; rev=r["revente"]-corr
        else: corr=(ref-km)*DICA_UNDER_KM_RATE; rev=r["revente"]+corr
        rev=round(rev)
        out.append({"year":r["year"],"horsepower":rhp,"brand":r["brand"],"gamme":r["gamme"],"model":r["model"],"motorisation":r["motorisation"],"type":r["type"],"neuf":r["neuf"],"revente":r["revente"],"reprise":r["reprise"],"reference_km":ref,"km_correction":round(corr),"revente_corrigee":rev,"reprise_corrigee":round(rev*DICA_REPRISE_FACTOR),"options_value":options_value_total,"revente_avec_options":round(rev+options_value_total),"reprise_avec_options":round(rev*DICA_REPRISE_FACTOR+options_value_total),"page":r["page"]})
    return out
def dica_near_matches(brand,model,year,km,hp=None,options_value_total=0):
    b,m=norm(brand),norm(model); ref=max(0,(DICA_EDITION_YEAR-year)*DICA_REF_KM_PER_YEAR); candidates=[]
    for r in DICA:
        if r["year"]!=year or r["brand_norm"]!=b: continue
        rm=r["model_norm"]
        if not (rm==m or rm in m or m in rm): continue
        rhp=motor_hp(r.get("motorisation",""))
        if hp is not None and rhp is not None and rhp != hp: continue
        if km>ref: corr=(km-ref)*DICA_OVER_KM_RATE; rev=r["revente"]-corr
        else: corr=(ref-km)*DICA_UNDER_KM_RATE; rev=r["revente"]+corr
        score=20
        if hp is not None and rhp==hp: score+=15
        if norm(r.get("gamme","")) and norm(r.get("gamme","")) in m: score+=10
        candidates.append((score,{"year":r["year"],"horsepower":rhp,"brand":r["brand"],"gamme":r["gamme"],"model":r["model"],"motorisation":r["motorisation"],"type":r["type"],"neuf":r["neuf"],"revente":r["revente"],"reprise":r["reprise"],"reference_km":ref,"km_correction":round(corr),"revente_corrigee":round(rev),"reprise_corrigee":round(rev*DICA_REPRISE_FACTOR),"page":r["page"]}))
    candidates.sort(key=lambda z:(z[0],z[1]["horsepower"]==hp if hp is not None else False),reverse=True)
    out=[]
    seen=set()
    for _,x in candidates:
        key=(x["gamme"],x["model"],x["motorisation"])
        if key not in seen: seen.add(key); out.append(x)
    return out[:5]

def model_match_score(text,model):
    raw=(text or "").lower(); model=(model or "").strip().lower()
    if not model:return 0
    if re.fullmatch(r"\d+",model): return 48 if re.search(rf"(?<!\d){re.escape(model)}(?!\d)",raw) else 0
    compact=norm(model); compact_text=norm(raw)
    if compact and compact in compact_text:return 48
    parts=[p for p in re.split(r"[\s/-]+",model) if len(p)>=2]
    return 48 if parts and all(p in raw for p in parts) else 0
def score_result(r,brand,model,year,target_km,hp=None):
    title=str(r.get("title","")); snippet=str(r.get("snippet","")); text=f"{title} {snippet}"; low=text.lower(); score=0
    if norm(brand) in norm(low): score+=25
    score+=model_match_score(low,model)
    if re.search(rf"\b{re.escape(str(year))}\b",low): score+=20
    ks=extract_kms(text)
    if ks:
        d=min(abs(k-target_km) for k in ks); score+=max(0,10-min(10,d/5000))
    if hp is not None:
        hps=extract_hp(text)
        if hp in hps: score+=15
    if norm(model) and norm(model) in norm(title): score+=5
    return score
def is_new(r):
    text=f"{r.get('title','')} {r.get('snippet','')}".lower()
    return any(w in text for w in NEW_WORDS) or bool(re.search(r"\b0\s*km\b",text))
def is_unavailable(r):
    text=f"{r.get('title','')} {r.get('snippet','')}".lower()
    return any(w in text for w in ("vendu","déjà vendu","deja vendu","indisponible","archivé","archive"))
def is_aggregation(r):
    text=f"{r.get('title','')} {r.get('snippet','')}".lower()
    return any(w in text for w in AGGREGATOR_WORDS)
def comparable_row(r,brand,model,year,target_km,hp=None):
    price,rkm=parse_price_km(r); score=score_result(r,brand,model,year,target_km,hp)
    if not price or score<65 or is_new(r) or is_unavailable(r): return None
    if is_aggregation(r): score-=10
    if score<65: return None
    adjusted=price; km_adjustment=0
    if rkm is not None:
        if rkm>target_km: km_adjustment=round((rkm-target_km)*DICA_OVER_KM_RATE)
        elif rkm<target_km: km_adjustment=-round((target_km-rkm)*DICA_UNDER_KM_RATE)
        adjusted=price+km_adjustment
    return {"title":r.get("title"),"url":r.get("link"),"snippet":r.get("snippet"),"price":price,"km":rkm,"adjusted":round(adjusted),"km_adjustment":km_adjustment,"score":round(score),"source":r.get("source","")}
@app.get("/")
def home(): return send_from_directory("static","index.html")
@app.post("/api/cote")
def cote():
    data=request.get_json(force=True); brand=str(data.get("brand","")).strip(); model=str(data.get("model","")).strip()
    cv_raw=str(data.get("cv","")).strip()
    hp_raw=str(data.get("hp","")).strip()
    try: year=int(data.get("year")); km=int(data.get("km")); cv=int(cv_raw) if cv_raw else None; hp=int(hp_raw) if hp_raw else None
    except (TypeError,ValueError): return jsonify({"error":"Année et kilométrage invalides."}),400
    if not brand or not model or year<2010 or km<0 or (cv is not None and (cv<1 or cv>50)) or (hp is not None and (hp<50 or hp>500)): return jsonify({"error":"Merci de renseigner des informations valides."}),400
    if not SERPER_API_KEY: return jsonify({"error":"SERPER_API_KEY manquante sur le serveur."}),500
    accessories=data.get("accessories") or []
    options_total, option_details=options_value(accessories)
    dica=dica_matches(brand,model,year,km,hp,options_total)
    hp_query=f" {hp} ch" if hp else ""
    queries=[f'"{brand} {model}" {year}{hp_query} camping-car occasion',f'"{brand} {model}" {year}{hp_query} "{km} km" occasion',f'"{brand} {model}" {year} camping car occasion prix',f'site:leboncoin.fr "{brand} {model}" {year}',f'site:paruvendu.fr "{brand} {model}" {year}',f'site:hunyvers.com "{brand} {model}" {year}',f'site:camping-car.com "{brand} {model}" {year}']
    results=[]
    for q in queries:
        try:
            resp=requests.post("https://google.serper.dev/search",headers={"X-API-KEY":SERPER_API_KEY,"Content-Type":"application/json"},json={"q":q,"gl":"fr","hl":"fr","num":10},timeout=20); resp.raise_for_status()
            for item in resp.json().get("organic",[]): item["source"]=q; results.append(item)
        except requests.RequestException: continue
    uniq={r["link"]:r for r in results if r.get("link")}
    dedup={}
    for r in uniq.values():
        key=(norm(r.get("title","")),parse_price_km(r)[0],parse_price_km(r)[1])
        if key[0]: dedup[key]=r
    rows=[]; context=[]
    for r in dedup.values():
        row=comparable_row(r,brand,model,year,km,hp)
        if not row: continue
        (context if row["km"] is None else rows).append(row)
    rows.sort(key=lambda x:(x["score"],-abs((x["km"] or km)-km)),reverse=True); context.sort(key=lambda x:x["score"],reverse=True)
    primary=rows[:15]; values=[x["adjusted"] for x in primary if x["adjusted"]]
    if len(values)<3: primary=(primary+context)[:15]; values=[x["adjusted"] for x in primary if x["adjusted"]]
    if len(values)<3: return jsonify({"status":"insufficient","comparables":primary,"context":context[:5],"dica":dica,"message":"Moins de 3 comparables suffisamment fiables ont été trouvés sur le marché actuel."})
    med=statistics.median(values)
    filtered_values=list(values)
    excluded_values=[]
    if len(values)>=5:
        deviations=[abs(v-med) for v in values]
        mad=statistics.median(deviations)
        if mad>0:
            limit=3*mad
            filtered_values=[v for v in values if abs(v-med)<=limit]
        else:
            filtered_values=[v for v in values if med*.90<=v<=med*1.10]
        if len(filtered_values)<3:
            filtered_values=list(values)
        excluded_values=[v for v in values if v not in filtered_values]
    filtered=filtered_values
    market=round(statistics.median(filtered)/100)*100
    market_low=round(min(filtered)/100)*100
    market_high=round(max(filtered)/100)*100
    dispersion=statistics.median([abs(v-statistics.median(filtered)) for v in filtered]) / max(1,statistics.median(filtered))
    confidence="Bonne" if len(filtered)>=7 and dispersion<=0.10 else "Correcte" if len(filtered)>=5 else "Faible"
    for row in primary:
        row["retenu_dans_cote"]=row["adjusted"] in filtered
        row["atypique"]=row["adjusted"] in excluded_values
    search_time=datetime.now().astimezone().isoformat(timespec="minutes")
    return jsonify({"status":"ok","comparables":primary,"context":context[:5],"market":market,"market_low":market_low,"market_high":market_high,"search_time":search_time,"fiscal_cv":cv,"horsepower":hp,"trade":max(0,market-MASTERS_FRAIS),"masters_frais":MASTERS_FRAIS,"confidence":confidence,"count":len(filtered),"excluded_count":len(excluded_values),"dica":dica,"dica_near":dica_near_matches(brand,model,year,km,hp,options_total) if not dica else [],"accessories":option_details,"accessories_value":options_total,"dica_reference_km":max(0,(DICA_EDITION_YEAR-year)*DICA_REF_KM_PER_YEAR),"dica_edition":"Cote Officielle de l’Occasion n°32 — janvier à avril 2026"})
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
