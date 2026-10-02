import os, re, statistics, json, requests, unicodedata
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

BASE_DIR = os.path.dirname(__file__)

app = Flask(__name__, static_folder="static")

@app.errorhandler(Exception)
def api_safe_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error":"Erreur interne du moteur : "+str(e)}), 500
    raise e

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
DICA_EDITION_YEAR = 2026
DICA_REF_KM_PER_YEAR = 12000
DICA_OVER_KM_RATE = 0.12
DICA_UNDER_KM_RATE = 0.09
DICA_REPRISE_FACTOR = 0.85
MASTERS_FRAIS = 8000
DICA_RECALAGE_FACTOR = 0.50
DICA_REF_KM_BY_TYPE = {"F":15000,"V":20000,"P":12000,"C":12000,"I":12000}



def poids_lourd_km_rules(text):
    """Règles DICA PL n°32."""
    t=norm(text)
    if "tandem" in t or "6roues" in t or "6 roues" in (text or "").lower():
        return 20000,0.50,0.25
    if "man" in t or "iveco" in t:
        return 25000,0.50,0.20
    return None,None,None

def poids_lourd_matches(brand, model, year, km, hp=None, ptac=None):
    out=[]
    for r in DICA_PL:
        if r["year"]!=year: continue
        if r.get("brand_norm") and r["brand_norm"]!=norm(brand): continue
        if not dica_model_match(model,r.get("model",""),""): continue
        if ptac is not None and abs(float(r.get("ptac") or 0)-(float(ptac)/1000.0))>0.15: continue
        rhp=motor_hp(r.get("carrier",""))
        if hp is not None and rhp is not None and rhp!=hp: continue
        ref,over,under=poids_lourd_km_rules(r.get("carrier",""))
        if ref is None:
            corr=0
            value=round(r["revente"])
        else:
            corr=(km-ref)*over if km>ref else (ref-km)*under
            value=round(r["revente"]-corr) if km>ref else round(r["revente"]+corr)
        out.append({"year":r["year"],"brand":r["brand"],"model":r["model"],"type":r.get("type"),"motorisation":r.get("carrier"),"ptac":r.get("ptac"),"neuf":r.get("neuf"),"revente":r.get("revente"),"revente_corrigee":value,"reference_km":ref,"km_correction":round(corr),"page":r.get("page")})
    return out

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii","ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]","",s)

def dica_ref_km(year, type_):
    return max(0, (DICA_EDITION_YEAR-year) * DICA_REF_KM_BY_TYPE.get(str(type_ or "").upper(), DICA_REF_KM_PER_YEAR))

with open(os.path.join(BASE_DIR, "dica32_camping_cars.json"), encoding="utf-8") as f:
    _dica = json.load(f)
_dica_vf_records = []
for _year in range(2016, 2026):
    _path = os.path.join(BASE_DIR, "data", f"dica32_vans_fourgons_{_year}.json")
    try:
        with open(_path, encoding="utf-8") as f:
            _year_data = json.load(f)
        if isinstance(_year_data, list):
            _dica_vf_records.extend(_year_data)
        elif isinstance(_year_data, dict):
            _dica_vf_records.extend(_year_data.get("records", []))
    except FileNotFoundError:
        continue
_dica_vf = {"records": _dica_vf_records}
DICA_PL = []
import glob
for _path in sorted(glob.glob(os.path.join(BASE_DIR, "data", "dica32_poids_lourds_*.json"))):
    _name = os.path.basename(_path)
    _m = re.search(r"dica32_poids_lourds_(20\d{2})", _name)
    if not _m:
        continue
    _year = int(_m.group(1))
    # 2023 a été retraité directement depuis les colonnes 2023 du PDF DICA.
    # On ignore les anciens morceaux OCR qui mélangeaient des lignes 2024.
    if _year == 2023 and _name != "dica32_poids_lourds_2023_clean.json":
        continue
    try:
        with open(_path, encoding="utf-8") as f:
            _rows = json.load(f)
        for row in _rows:
            if isinstance(row, list) and len(row) >= 8:
                brand, model, type_, carrier, ptac, neuf, revente, page = row[:8]
            elif isinstance(row, list) and len(row) >= 7:
                model, type_, carrier, ptac, neuf, revente, page = row[:7]
                carrier_text = str(carrier or "")
                brand = next((b for b in ("Mercedes","Iveco","Fiat","Ford","Citroën","Citroen","MAN","Renault","Volvo","Scania") if b.lower() in carrier_text.lower()), "")
            else:
                continue
            DICA_PL.append({"year":_year,"brand":brand,"brand_norm":norm(brand),"model":model,"model_norm":norm(model),"type":type_,"carrier":carrier,"ptac":ptac,"neuf":neuf,"revente":revente,"page":page})
    except FileNotFoundError:
        continue
DICA = []
for row in (_dica["records"] + _dica_vf.get("records", [])):
    if isinstance(row, list):
        year, brand, gamme, model, motorisation, type_, neuf, revente, reprise, page = row
        DICA.append({
            "year": year, "brand": brand, "brand_norm": norm(brand),
            "gamme": gamme, "model": model, "model_norm": norm(model),
            "motorisation": motorisation, "type": type_, "neuf": neuf,
            "revente": revente, "reprise": reprise, "page": page
        })
    else:
        row["brand_norm"] = norm(row.get("brand",""))
        row["model_norm"] = norm(row.get("model",""))
        DICA.append(row)
NEW_WORDS=("neuf","neuve","0 km","0km","jamais immatriculé","jamais immatricule","véhicule neuf","vehicule neuf","stock neuf","déstockage","destockage","non immatriculé","non immatricule","modèle neuf","modele neuf")
AGGREGATOR_WORDS=("page 2","page 3","page 4","page 5","tous les véhicules","toutes les annonces","résultats de recherche","resultats de recherche","annonces similaires")
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
def extract_transmission(text):
    t=(text or "").lower()
    if re.search(r"\b(?:bo[iî]te\s*)?(?:auto(?:matique)?|bva|matic|9g[- ]tronic|8g[- ]tronic|e[- ]shift|comfort[- ]matic|robotis[ée]e)\b",t):
        return "Automatique"
    if re.search(r"\b(?:bo[iî]te\s*)?(?:manuelle|bvm)\b",t):
        return "Manuelle"
    return None

def extract_hp(text):
    out=[]
    for m in re.finditer(r'\b(\d{2,3})\s*(?:ch|cv|chevaux)\b', (text or '').lower()):
        v=int(m.group(1))
        if 50<=v<=500: out.append(v)
    return out

def extract_years(text):
    out=[]
    for m in re.finditer(r'\b(20(?:1\d|2[0-9]))\b', str(text or '')):
        y=int(m.group(1))
        if 2010<=y<=DICA_EDITION_YEAR:
            out.append(y)
    return sorted(set(out))
def motor_hp(motorisation):
    vals=extract_hp(motorisation)
    if vals:
        return vals[0]
    parts=str(motorisation or "").strip().split()
    if parts and parts[-1].isdigit():
        v=int(parts[-1])
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

def dica_model_key(s):
    # Neutralise uniquement les qualificatifs de transmission souvent ajoutés
    # au modèle dans les annonces : MC4 262 Matic -> MC4 262.
    raw=unicodedata.normalize("NFKD", str(s or "")).encode("ascii","ignore").decode("ascii").lower()
    raw=re.sub(r"\b(?:matic|bva|bvm|automatique|automatic|auto)\b"," ",raw)
    return re.sub(r"\s+"," ",raw).strip()

def dica_model_match(target, record_model, record_gamme=""):
    t_raw=dica_model_key(target)
    rm_raw=dica_model_key(record_model)
    rg_raw=dica_model_key(record_gamme)
    t=norm(t_raw)
    rm=norm(rm_raw)
    if not t or not rm:
        return False
    composite=norm(f"{rg_raw} {rm_raw}")
    if t==rm or t==composite:
        return True
    # Compare les vrais tokens avant la normalisation compacte.
    # Exemple : "640 Titanium" doit matcher une gamme contenant 640/Titanium.
    tt=set(re.findall(r"[a-z0-9]+",t_raw))
    rt=set(re.findall(r"[a-z0-9]+",rm_raw))
    ct=set(re.findall(r"[a-z0-9]+",f"{rg_raw} {rm_raw}"))
    # Évite les faux positifs par sous-chaîne de modèle :
    # « 600 SPB » ne doit pas sélectionner « 600 SPB Family ».
    # Les variantes ne sont acceptées que si les tokens du modèle sont identiques,
    # ou si la saisie correspond exactement à gamme + modèle.
    if tt and rt and tt == rt:
        return True
    if tt and ct and tt == ct:
        return True
    return False

def dica_matches(brand,model,year,km,hp=None,options_value_total=0,category="camping"):
    b,m=norm(brand),norm(model); ref=dica_ref_km(year, None); out=[]
    for r in DICA:
        if r["year"]!=year or r["brand_norm"]!=b: continue
        if category=="van" and r.get("type")!="V": continue
        if category=="fourgon" and r.get("type")!="F": continue
        if category=="camping" and r.get("type") in ("F","V"): continue
        rm=r["model_norm"]; rg=norm(r.get("gamme",""))
        if not dica_model_match(model, r.get("model",""), r.get("gamme","")): continue
        ref=dica_ref_km(r["year"], r.get("type"))
        rhp=motor_hp(r.get("motorisation",""))
        if hp is not None and rhp is not None and rhp != hp: continue
        if km>ref: corr=(km-ref)*DICA_OVER_KM_RATE; rev=r["revente"]-corr
        else: corr=(ref-km)*DICA_UNDER_KM_RATE; rev=r["revente"]+corr
        rev=round(rev)
        out.append({"year":r["year"],"horsepower":rhp,"brand":r["brand"],"gamme":r["gamme"],"model":r["model"],"motorisation":r["motorisation"],"type":r["type"],"neuf":r["neuf"],"revente":r["revente"],"reprise":r["reprise"],"reference_km":ref,"km_correction":round(corr),"revente_corrigee":rev,"reprise_corrigee":round(r["reprise"] + ((rev-r["revente"]) * DICA_REPRISE_FACTOR)),"options_value":options_value_total,"revente_avec_options":round(rev+options_value_total),"reprise_avec_options":round(rev*DICA_REPRISE_FACTOR+options_value_total),"page":r["page"]})
    # Si plusieurs lignes DICA restent possibles, on ne choisit jamais
    # arbitrairement une gamme/motorisation. Une ligne n'est prioritaire que
    # lorsque la saisie permet de l'identifier sans ambiguïté.
    if len(out) > 1:
        target_key=dica_model_key(model)
        exact=[x for x in out if dica_model_key(x["model"]) == target_key]
        if len(exact)==1:
            out=exact
        else:
            same_hp=[x for x in out if hp is not None and x["horsepower"] == hp]
            if len(same_hp)==1:
                out=same_hp
    return out
def dica_near_matches(brand,model,year,km,hp=None,options_value_total=0,category="camping"):
    b,m=norm(brand),norm(model)
    candidates=[]
    for r in DICA:
        if r["brand_norm"]!=b: continue
        if category=="van" and r.get("type")!="V": continue
        if category=="fourgon" and r.get("type")!="F": continue
        if category=="camping" and r.get("type") in ("F","V"): continue
        year_gap=abs(r["year"]-year)
        if year_gap>2: continue
        rm=r["model_norm"]; rg=norm(r.get("gamme",""))
        if not dica_model_match(model, r.get("model",""), r.get("gamme","")): continue
        rhp=motor_hp(r.get("motorisation",""))
        if hp is not None and rhp != hp: continue
        ref=dica_ref_km(r["year"], r.get("type"))
        if km>ref: corr=(km-ref)*DICA_OVER_KM_RATE; rev=r["revente"]-corr
        else: corr=(ref-km)*DICA_UNDER_KM_RATE; rev=r["revente"]+corr
        score=100-(year_gap*20)
        if hp is not None and rhp==hp: score+=40
        if rg and rg in m: score+=20
        if r["year"]==year: score+=20
        candidates.append((score,year_gap,{"year":r["year"],"horsepower":rhp,"brand":r["brand"],"gamme":r["gamme"],"model":r["model"],"motorisation":r["motorisation"],"type":r["type"],"neuf":r["neuf"],"revente":r["revente"],"reprise":r["reprise"],"reference_km":ref,"km_correction":round(corr),"revente_corrigee":round(rev),"reprise_corrigee":round(r["reprise"] + ((rev-r["revente"]) * DICA_REPRISE_FACTOR)),"page":r["page"]}))
    candidates.sort(key=lambda z:(z[0],-z[1]),reverse=True)
    out=[]; seen=set()
    for _,_,x in candidates:
        key=(x["year"],x["gamme"],x["model"],x["motorisation"])
        if key not in seen:
            seen.add(key); out.append(x)
    return out[:5]

def model_match_score(text,model):
    raw=(text or "").lower(); model=(model or "").strip().lower()
    if not model:return 0
    if re.fullmatch(r"\d+",model): return 48 if re.search(rf"(?<!\d){re.escape(model)}(?!\d)",raw) else 0
    compact=norm(model); compact_text=norm(raw)
    if compact and compact in compact_text:
        # Une finition voisine ne doit pas être assimilée au modèle demandé.
        # Les séries spéciales peuvent toutefois être formulées différemment
        # dans les annonces : « 696F 60 Edition » / « 696F 60 anniversaire ».
        variant_words={"family","plus","supreme","sport","sports","elite","maxi","premium","edition","limited","exclusive","duo","xl","xs","l","s","m","g","gx","lj","sgx","slb","spb","4x4","60","anniversary","anniversaire"}
        m=re.search(rf"(?<![a-z0-9]){re.escape(compact)}(?![a-z0-9])",compact_text)
        if m:
            tail=compact_text[m.end():].strip().split()
            if tail and tail[0] in variant_words:
                return 0
            return 48
    # Séries spéciales : les annonces séparent souvent les codes alphanumériques
    # (« 696F » -> « 696 F ») et écrivent « 60e », « 60 ans » ou « 60 anniversaire »
    # au lieu de « 60 Edition ». On exige néanmoins le modèle de base ET l'identifiant
    # de série pour éviter de mélanger 696F standard et 696F 60e anniversaire.
    requested_tokens=re.findall(r"[a-z0-9]+",norm(model))
    if "60" in requested_tokens:
        has_base=False
        for tok in requested_tokens:
            if tok in {"60","edition","anniversary","anniversaire"}:
                continue
            if len(tok)>=2 and re.search(rf"(?<![a-z0-9]){re.escape(tok[:-1])}\\s*{re.escape(tok[-1])}(?![a-z0-9])",raw):
                has_base=True
            elif tok and re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])",compact_text):
                has_base=True
        has_60=bool(re.search(r"(?<!\\d)60(?:e|eme|ème|ans)?(?!\\d)",raw))
        has_special=bool(re.search(r"\\b(?:edition|anniversaire|anniversary|ans)\\b",raw))
        if has_base and has_60 and has_special:
            return 48
    parts=[p for p in re.split(r"[\s/-]+",model) if len(p)>=2]
    return 48 if parts and all(p in raw for p in parts) else 0
def dica_gamme_score(text,brand,model,year,requested_gamme,category="camping"):
    requested=norm(requested_gamme)
    if not requested:
        return 0
    candidates=[]
    for r in DICA:
        if r.get("brand_norm")!=norm(brand) or r.get("year")!=year:
            continue
        if category=="van" and r.get("type")!="V": continue
        if category=="fourgon" and r.get("type")!="F": continue
        if category=="camping" and r.get("type") in ("F","V"): continue
        if not dica_model_match(model,r.get("model",""),r.get("gamme","")):
            continue
        g=norm(r.get("gamme",""))
        if g and g not in candidates:
            candidates.append(g)
    text_norm=norm(text)
    matched=[g for g in candidates if g in text_norm]
    if not matched:
        # L'annonce ne précise pas sa gamme : elle reste exploitable.
        return 0
    # Parmi les gammes présentes dans le texte, la plus longue est généralement
    # la plus précise : TWIN SPORTS doit primer sur TWIN, par exemple.
    longest=max(matched,key=len)
    if longest==requested:
        return 20
    # Si une autre gamme DICA de la même famille/modèle est explicitement citée,
    # on l'écarte plutôt que de la faire entrer dans la cote de la gamme choisie.
    return -35

def score_result(r,brand,model,year,target_km,hp=None,transmission=None,dica_gamme=None,category="camping"):
    title=str(r.get("title","")); snippet=str(r.get("snippet","")); text=f"{title} {snippet}"; low=text.lower(); score=0
    if norm(brand) in norm(low): score+=25
    # Renforce la qualification par catégorie sans exiger un libellé unique :
    # les annonces de fourgons peuvent employer "fourgon", "fourgon aménagé"
    # ou "fourgonnette aménagée"; les vans sont souvent annoncés simplement "van".
    if category=="fourgon":
        if re.search(r"\bfourgon(?:s)?\b",low): score+=18
        elif re.search(r"\b(fourgonette|fourgonette)\b",low): score+=12
        elif re.search(r"\bvan\b",low): score-=20
    elif category=="van":
        if re.search(r"\bvan(?:s)?\b",low): score+=18
        elif re.search(r"\bfourgon(?:s)?\b",low): score-=20
    elif category=="camping":
        if re.search(r"\bfourgon(?:s)?\b|\bvan(?:s)?\b",low): score-=15
    model_score=model_match_score(low,model)
    if model_score<=0:
        return 0
    score+=model_score
    if dica_gamme:
        gamme_score=dica_gamme_score(text,brand,model,year,dica_gamme,category)
        if gamme_score<0:
            return 0
        score+=gamme_score
    if re.search(rf"\b{re.escape(str(year))}\b",low): score+=20
    ks=extract_kms(text)
    if ks:
        d=min(abs(k-target_km) for k in ks); score+=max(0,10-min(10,d/5000))
    if hp is not None:
        hps=extract_hp(text)
        if hp in hps: score+=15
    if transmission:
        rt=extract_transmission(text)
        if rt==transmission: score+=25
        elif rt and rt!=transmission: score-=15
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
def experimental_brand_value(results, brand, year, category, requested_gamme=None, requested_model=None):
    """Coefficient marque strict : même année/catégorie, annonces reliées à une référence DICA, médiane et filtrage des ratios atypiques."""
    candidates=[]
    seen_ads=set()
    for ad in results:
        if is_new(ad) or is_unavailable(ad) or is_aggregation(ad):
            continue
        price, km=parse_price_km(ad)
        if not price:
            continue
        text=f"{ad.get('title','')} {ad.get('snippet','')}"
        ad_years=extract_years(text)
        if not ad_years or year not in ad_years:
            continue

        # Une annonce ne peut contribuer qu'une seule fois.
        ad_key=norm(ad.get("title","")) or norm(text)
        if ad_key in seen_ads:
            continue

        best=None
        for r in DICA:
            if r.get("year")!=year or r.get("brand_norm")!=norm(brand):
                continue
            if category=="van" and r.get("type")!="V":
                continue
            if category=="fourgon" and r.get("type")!="F":
                continue
            if category=="camping" and r.get("type") in ("F","V"):
                continue

            model_for_match=requested_model or r.get("model","")
            ms=model_match_score(text, model_for_match)
            if ms<=0:
                continue
            # Le coefficient de marque doit respecter la gamme DICA choisie.
            # Une annonce qui cite explicitement une autre gamme est exclue ;
            # une annonce qui ne précise pas la gamme reste exploitable.
            gs=dica_gamme_score(text, brand, model_for_match, year, requested_gamme, category) if requested_gamme else 0
            if gs < 0:
                continue
            rhp=motor_hp(r.get("motorisation",""))
            hps=extract_hp(text)
            if hps and rhp and rhp not in hps:
                continue
            combined=ms+gs
            if best is None or combined > best[0]:
                best=(combined,r)

        if not best:
            continue

        dr=best[1]
        ref=dica_ref_km(dr["year"],dr.get("type"))
        if km is None:
            corr_revente=dr["revente"]
        elif km>ref:
            corr_revente=dr["revente"]-(km-ref)*DICA_OVER_KM_RATE
        else:
            corr_revente=dr["revente"]+(ref-km)*DICA_UNDER_KM_RATE

        if corr_revente<=0:
            continue
        ratio=price/corr_revente

        # Bornes de sécurité avant calcul statistique.
        if not 0.45<=ratio<=1.35:
            continue

        seen_ads.add(ad_key)
        candidates.append({
            "ratio":ratio,
            "price":price,
            "dica_revente":round(corr_revente),
            "title":ad.get("title",""),
            "dica_model":dr.get("model",""),
            "dica_gamme":dr.get("gamme","")
        })

    # Pas de coefficient si l'échantillon est trop faible.
    if len(candidates)<5:
        return None

    ratios=[x["ratio"] for x in candidates]
    median_ratio=statistics.median(ratios)

    # Filtrage robuste des annonces atypiques par MAD.
    deviations=[abs(v-median_ratio) for v in ratios]
    mad=statistics.median(deviations)
    if mad>0:
        limit=3*mad
        filtered=[x for x in candidates if abs(x["ratio"]-median_ratio)<=limit]
    else:
        filtered=[x for x in candidates if median_ratio*0.90<=x["ratio"]<=median_ratio*1.10]

    # Le coefficient n'est utilisable que si au moins 5 observations restent.
    if len(filtered)<5:
        return None

    coef=statistics.median([x["ratio"] for x in filtered])
    return {
        "coefficient":round(coef,3),
        "comparables":len(filtered),
        "candidates_total":len(candidates),
        "examples":filtered[:5]
    }

def comparable_row(r,brand,model,year,target_km,hp=None,transmission=None,dica_gamme=None,category="camping"):
    price,rkm=parse_price_km(r)
    text=f"{r.get('title','')} {r.get('snippet','')}"
    title_years=extract_years(str(r.get("title","")))
    snippet_years=extract_years(str(r.get("snippet","")))
    # Une annonce n'entre dans la cote que si l'année du véhicule est
    # explicitement identifiable. Si le titre donne une année, elle doit
    # être exactement celle du véhicule évalué. Sinon, l'année recherchée
    # doit au minimum apparaître dans le descriptif.
    if title_years and year not in title_years:
        return None
    if not title_years and year not in snippet_years:
        return None
    score=score_result(r,brand,model,year,target_km,hp,transmission,dica_gamme,category)
    if not price or score<65 or is_new(r) or is_unavailable(r): return None
    if is_aggregation(r): score-=10
    if score<65: return None
    adjusted=price; km_adjustment=0
    if rkm is not None:
        if category=="poids_lourd":
            ref_km, over_rate, under_rate = poids_lourd_km_rules(f"{r.get('title','')} {r.get('snippet','')}")
            if ref_km is not None:
                delta_km=rkm-target_km
                km_adjustment=round(delta_km*(over_rate if delta_km>0 else under_rate))
                km_adjustment=km_adjustment
        else:
            if rkm>target_km: km_adjustment=round((rkm-target_km)*DICA_OVER_KM_RATE)
            elif rkm<target_km: km_adjustment=-round((target_km-rkm)*DICA_UNDER_KM_RATE)
        adjusted=price+km_adjustment
    return {"title":r.get("title"),"url":r.get("link"),"snippet":r.get("snippet"),"price":price,"km":rkm,"adjusted":round(adjusted),"km_adjustment":km_adjustment,"score":round(score),"source":r.get("source",""),"transmission":extract_transmission(text)}
@app.get("/")
def home(): return send_from_directory("static","index.html")
@app.get("/api/dica/catalog")
def dica_catalog():
    category=str(request.args.get("category","camping")).strip().lower()
    year_raw=str(request.args.get("year","")).strip()
    brand_filter=str(request.args.get("brand","")).strip()
    if category not in ("camping","poids_lourd","van","fourgon"):
        return jsonify({"error":"Catégorie invalide."}),400
    year=int(year_raw) if year_raw.isdigit() else None
    rows=[]
    seen=set()
    source = DICA_PL if category=="poids_lourd" else DICA
    for r in source:
        if year is not None and r.get("year")!=year:
            continue
        if category=="van" and r.get("type")!="V": continue
        if category=="fourgon" and r.get("type")!="F": continue
        if category=="camping" and r.get("type") in ("F","V"): continue
        if brand_filter and norm(r.get("brand",""))!=norm(brand_filter):
            continue
        key=(r.get("year"),r.get("brand",""),r.get("gamme",""),r.get("model",""),r.get("motorisation",""),r.get("type",""))
        if key in seen: continue
        seen.add(key)
        rows.append({
            "year":r.get("year"),"brand":r.get("brand",""),"gamme":r.get("gamme",""),
            "model":r.get("model",""),"motorisation":r.get("carrier",r.get("motorisation","")),
            "type":r.get("type",""),"neuf":r.get("neuf"),"revente":r.get("revente"),
            "reprise":r.get("reprise"),"page":r.get("page")
        })
    rows.sort(key=lambda x:(x["brand"],x["gamme"],x["model"],x["motorisation"]))
    return jsonify({"category":category,"year":year,"count":len(rows),"records":rows})

@app.get("/dica_menu.json")
def dica_menu_file():
    return send_from_directory("static", "dica_menu.json", mimetype="application/json", max_age=0)

@app.get("/api/dica/years")
def dica_years():
    category=str(request.args.get("category","camping")).strip().lower()
    if category not in ("camping","poids_lourd","van","fourgon"):
        return jsonify({"error":"Catégorie invalide."}),400
    source = DICA_PL if category=="poids_lourd" else DICA
    years=sorted({
        int(r.get("year")) for r in source
        if r.get("year") is not None
        and ((category=="van" and r.get("type")=="V") or
             (category=="fourgon" and r.get("type")=="F") or
             (category in ("camping","poids_lourd") and r.get("type") not in ("F","V")))
    }, reverse=True)
    return jsonify({"category":category,"years":years})

@app.get("/api/dica/brands")
def dica_brands():
    category=str(request.args.get("category","camping")).strip().lower()
    year_raw=str(request.args.get("year","")).strip()
    if category not in ("camping","poids_lourd","van","fourgon") or not year_raw.isdigit():
        return jsonify({"error":"Paramètres invalides."}),400
    year=int(year_raw)
    source = DICA_PL if category=="poids_lourd" else DICA
    brands=sorted({
        r.get("brand","") for r in source
        if r.get("year")==year and r.get("brand")
        and ((category=="van" and r.get("type")=="V") or
             (category=="fourgon" and r.get("type")=="F") or
             (category in ("camping","poids_lourd") and r.get("type") not in ("F","V")))
    }, key=lambda x:x.lower())
    return jsonify({"category":category,"year":year,"brands":brands})

@app.get("/api/dica/models")
def dica_models():
    category=str(request.args.get("category","camping")).strip().lower()
    year_raw=str(request.args.get("year","")).strip()
    brand=str(request.args.get("brand","")).strip()
    if category not in ("camping","poids_lourd","van","fourgon") or not year_raw.isdigit() or not brand:
        return jsonify({"error":"Paramètres invalides."}),400
    year=int(year_raw)
    rows=[]
    seen=set()
    source = DICA_PL if category=="poids_lourd" else DICA
    for r in source:
        if r.get("year")!=year or norm(r.get("brand",""))!=norm(brand):
            continue
        if category=="van" and r.get("type")!="V": continue
        if category=="fourgon" and r.get("type")!="F": continue
        if category=="camping" and r.get("type") in ("F","V"): continue
        key=(r.get("gamme",""),r.get("model",""),r.get("motorisation",""))
        if key in seen: continue
        seen.add(key)
        rows.append({
            "gamme":r.get("gamme",""),"model":r.get("model",""),
            "motorisation":r.get("carrier",r.get("motorisation","")),
            "neuf":r.get("neuf"),"revente":r.get("revente"),
            "reprise":r.get("reprise"),"page":r.get("page"),
            "type":r.get("type","")
        })
    rows.sort(key=lambda x:(x["gamme"],x["model"],x["motorisation"]))
    return jsonify({"category":category,"year":year,"brand":brand,"records":rows})

@app.post("/api/cote")
def cote():
    data=request.get_json(force=True); category=str(data.get("category","camping")).strip().lower(); brand=str(data.get("brand","")).strip(); model=str(data.get("model","")).strip(); dica_gamme=str(data.get("dica_gamme","")).strip()
    cv_raw=str(data.get("cv","")).strip()
    hp_raw=str(data.get("hp","")).strip(); transmission=str(data.get("transmission","")).strip() or None
    try: year=int(data.get("year")); km=int(data.get("km")); ptac=int(data.get("ptac")) if str(data.get("ptac","")).strip() else None; cv=int(cv_raw) if cv_raw else None; hp=int(hp_raw) if hp_raw else None
    except (TypeError,ValueError): return jsonify({"error":"Année et kilométrage invalides."}),400
    if category not in ("camping","poids_lourd","van","fourgon"): return jsonify({"error":"Catégorie invalide."}),400
    if not brand or not model or year<2010 or km<0 or (category=="poids_lourd" and (ptac is None or ptac<3501)) or (cv is not None and (cv<1 or cv>50)) or (hp is not None and (hp<50 or hp>500)) or (transmission not in (None,"Automatique","Manuelle")): return jsonify({"error":"Merci de renseigner des informations valides."}),400
    if not SERPER_API_KEY: return jsonify({"error":"SERPER_API_KEY manquante sur le serveur."}),500
    accessories=data.get("accessories") or []
    options_total, option_details=options_value(accessories)
    dica=poids_lourd_matches(brand,model,year,km,hp,ptac) if category=="poids_lourd" else dica_matches(brand,model,year,km,hp,options_total,category)
    hp_query=f" {hp} ch" if hp else ""
    transmission_query=f" {transmission.lower()}" if transmission else ""
    market_term="camping-car poids lourd" if category=="poids_lourd" else ("camping-car" if category=="camping" else ("fourgon aménagé" if category=="fourgon" else "van aménagé"))
    gamme_query=f' "{dica_gamme}"' if dica_gamme else ""
    queries=[f'"{brand} {model}"{gamme_query} {year}{hp_query}{transmission_query} {market_term} occasion',f'"{brand} {model}" {year}{hp_query} "{km} km" {market_term} occasion',f'"{brand} {model}" {year} {market_term} occasion prix',f'site:leboncoin.fr "{brand} {model}" {year} {market_term}',f'site:paruvendu.fr "{brand} {model}" {year} {market_term}',f'site:hunyvers.com "{brand} {model}" {year} {market_term}',f'site:camping-car.com "{brand} {model}" {year} {market_term}']
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
        row=comparable_row(r,brand,model,year,km,hp,transmission,dica_gamme,category)
        if not row: continue
        (context if row["km"] is None else rows).append(row)
    rows.sort(key=lambda x:(x["score"],-abs((x["km"] or km)-km)),reverse=True); context.sort(key=lambda x:x["score"],reverse=True)
    matching_rows=[x for x in rows if x.get("transmission")==transmission] if transmission else rows
    transmission_fallback=bool(transmission and len(matching_rows)<3)
    primary=(matching_rows if not transmission_fallback else rows)[:15]; values=[x["adjusted"] for x in primary if x["adjusted"]]
    # Les annonces sans kilométrage restent du contexte uniquement :
    # elles ne doivent jamais entrer dans la médiane ni permettre de fabriquer
    # une cote lorsqu'il n'y a pas assez de comparables qualifiés.
    experimental_brand=None
    if len(values)<3:
        brand_queries=[
            f'"{brand}" {year} {market_term} occasion',
            f'"{brand}" {year} {market_term} prix occasion',
            f'site:leboncoin.fr "{brand}" {year} {market_term}',
            f'site:camping-car.com "{brand}" {year}'
        ]
        brand_results=[]
        for q in brand_queries:
            try:
                resp=requests.post("https://google.serper.dev/search",headers={"X-API-KEY":SERPER_API_KEY,"Content-Type":"application/json"},json={"q":q,"gl":"fr","hl":"fr","num":10},timeout=20)
                resp.raise_for_status()
                for item in resp.json().get("organic",[]):
                    item["source"]=q
                    brand_results.append(item)
            except requests.RequestException:
                continue
        brand_unique={x.get("link"):x for x in brand_results if x.get("link")}
        experimental_brand=experimental_brand_value(list(brand_unique.values()),brand,year,category,requested_gamme=dica_gamme,requested_model=model)
        if category!="poids_lourd" and experimental_brand and len(dica)==1:
            resale=round(dica[0]["revente_corrigee"]*experimental_brand["coefficient"])
            masters=max(0,resale-MASTERS_FRAIS)
            return jsonify({
                "status":"experimental",
                "category":category,"ptac":ptac,
                "category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),
                "dica_gamme":dica_gamme,"comparables":primary,"context":context[:5],
                "dica":dica,"dica_ambiguous":False,"dica_near":[],
                "market":None,"market_low":None,"market_high":None,
                "confidence":"Estimative","experimental_brand":experimental_brand,
                "experimental_resale":resale,"experimental_professional_value":masters,
                "trade":masters,"masters_frais":MASTERS_FRAIS,
                "quality":{"comparables":len(values),"km_comparables":len(values),"sans_km":len(context),"atypiques":0,"transmission_fallback":transmission_fallback,"brand_comparables":experimental_brand["comparables"]},
                "message":"Marché insuffisant pour établir une cote modèle. Valeur estimative calculée à partir d'un coefficient marché observé pour la marque, appliqué au prix supposé de revente DICA corrigé. Les 8 000 € Masters sont déduits du prix supposé de revente."
            })
        return jsonify({
            "status":"insufficient","category":category,"ptac":ptac,
            "category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),
            "dica_gamme":dica_gamme,"comparables":primary,"context":context[:5],"dica":dica,
            "dica_ambiguous":len(dica)>1,
            "dica_near":dica_near_matches(brand,model,year,km,hp,options_total,category) if (not dica and category!="poids_lourd") else [],
            "quality":{"comparables":len(values),"km_comparables":len(values),"sans_km":len(context),"atypiques":0,"transmission_fallback":transmission_fallback},
            "message":"Cote marché non calculée : moins de 3 comparables qualifiés avec kilométrage ont été trouvés. Les annonces sans kilométrage restent affichées à titre de contexte uniquement."
        })
    med=statistics.median(values)
    filtered_values=list(values)
    excluded_values=[]
    if len(values)>=5:
        deviations=[abs(v-med) for v in values]
        mad=statistics.median(deviations)
        if mad>0:
            limit=3*mad
            keep_mask=[abs(v-med)<=limit for v in values]
        else:
            keep_mask=[med*.90<=v<=med*1.10 for v in values]
        if sum(keep_mask)>=3:
            filtered_values=[v for v,keep in zip(values,keep_mask) if keep]
            excluded_values=[v for v,keep in zip(values,keep_mask) if not keep]
        else:
            filtered_values=list(values)
    filtered=filtered_values
    market=round(statistics.median(filtered)/100)*100
    market_low=round(min(filtered)/100)*100
    market_high=round(max(filtered)/100)*100
    dispersion=statistics.median([abs(v-statistics.median(filtered)) for v in filtered]) / max(1,statistics.median(filtered))
    confidence="Bonne" if len(filtered)>=7 and dispersion<=0.10 else "Correcte" if len(filtered)>=5 else "Faible"
    filtered_ids={id(row) for row in primary if row["adjusted"] in filtered}
    excluded_ids={id(row) for row in primary if row["adjusted"] in excluded_values}
    for row in primary:
        row["retenu_dans_cote"]=id(row) in filtered_ids
        row["atypique"]=id(row) in excluded_ids
    dica_near=dica_near_matches(brand,model,year,km,hp,options_total,category) if (not dica and category!="poids_lourd") else []
    search_time=datetime.now().astimezone().isoformat(timespec="minutes")
    transmission_gap=None
    transmission_counts={"Automatique":0,"Manuelle":0,"Inconnue":0}
    for row in rows:
        rt=row.get("transmission")
        transmission_counts[rt if rt in ("Automatique","Manuelle") else "Inconnue"]+=1
    auto_vals=[x["adjusted"] for x in rows if x.get("transmission")=="Automatique"]
    manual_vals=[x["adjusted"] for x in rows if x.get("transmission")=="Manuelle"]
    if len(auto_vals)>=3 and len(manual_vals)>=3:
        auto_med=round(statistics.median(auto_vals)/100)*100
        manual_med=round(statistics.median(manual_vals)/100)*100
        transmission_gap={"automatique_median":auto_med,"manuelle_median":manual_med,"difference":auto_med-manual_med,"difference_pct":round((auto_med-manual_med)/manual_med*100,1)}
    experimental_professional_value=None
    experimental_market_gap=None
    if len(dica)==1:
        experimental_market_gap=round(market-dica[0]["revente_corrigee"])
        experimental_professional_value=round(dica[0]["reprise_corrigee"] + (experimental_market_gap*DICA_RECALAGE_FACTOR))
    return jsonify({"status":"ok","category":category,"ptac":ptac,"category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),"comparables":primary,"context":context[:5],"market":market,"market_low":market_low,"market_high":market_high,"search_time":search_time,"fiscal_cv":cv,"horsepower":hp,"transmission":transmission,"transmission_fallback":transmission_fallback,"transmission_counts":transmission_counts,"transmission_gap":transmission_gap,"trade":max(0,market-MASTERS_FRAIS),"masters_frais":MASTERS_FRAIS,"confidence":confidence,"count":len(filtered),"excluded_count":len(excluded_values),"dica":dica,"dica_ambiguous":len(dica)>1,"dica_near":dica_near,"quality":{"comparables":len(filtered),"atypiques":len(excluded_values),"transmission_fallback":transmission_fallback,"km_comparables":sum(1 for x in primary if x.get("km") is not None),"sans_km":len(context)},"experimental_recalage_factor":DICA_RECALAGE_FACTOR,"experimental_market_gap":experimental_market_gap,"experimental_professional_value":experimental_professional_value,"accessories":option_details,"accessories_value":options_total,"dica_reference_km":(dica[0]["reference_km"] if dica else (dica_near[0]["reference_km"] if dica_near else (None if category=="poids_lourd" else dica_ref_km(year, "V" if category=="van" else "F" if category=="fourgon" else None)))),"dica_edition":"Cote Officielle de l’Occasion n°32 — janvier à avril 2026"})
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
