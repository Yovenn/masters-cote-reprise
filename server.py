import os, re, statistics, json, requests, unicodedata, subprocess, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False
from html import unescape
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
MASTERS_FRAIS = 10000
# Matching moteur marche : strict par reference DICA
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
    if _year == 2024 and _name != "dica32_poids_lourds_2024_clean.json":
        continue
    if _year == 2016 and _name != "dica32_poids_lourds_2016_clean.json":
        continue
    if _year == 2017 and _name != "dica32_poids_lourds_2017_clean.json":
        continue
    if _year == 2018 and _name != "dica32_poids_lourds_2018_clean.json":
        continue
    if _year == 2019 and _name != "dica32_poids_lourds_2019_clean.json":
        continue
    if _year == 2020 and _name != "dica32_poids_lourds_2020_clean.json":
        continue
    if _year == 2021 and _name != "dica32_poids_lourds_2021_clean.json":
        continue
    if _year == 2022 and _name != "dica32_poids_lourds_2022_clean.json":
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
# Référentiel marque PL : reconstruction prudente de la marque lorsque les anciens fichiers ne la contenaient pas.
PL_CARRIER_BRANDS={"mercedes","iveco","fiat","ford","citroen","citroën","man","renault","volvo","scania"}

def _pl_brand_lookup():
    idx={}
    for _r in DICA_PL:
        _b=str(_r.get("brand") or "").strip()
        _m=norm(_r.get("model",""))
        if not _m or norm(_b) in PL_CARRIER_BRANDS:
            continue
        idx.setdefault(_m,set()).add(_b)
    return idx

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

# Corrige les anciens enregistrements PL classés par porteur lorsqu'un même modèle
# possède une marque unique connue dans le référentiel PL.
PL_MODEL_BRANDS=_pl_brand_lookup()
for _r in DICA_PL:
    _b=norm(_r.get("brand",""))
    if _b in PL_CARRIER_BRANDS or not _r.get("brand"):
        _m=norm(_r.get("model",""))
        _brands=PL_MODEL_BRANDS.get(_m,set())
        if len(_brands)==1:
            _r["brand"]=next(iter(_brands))
            _r["brand_norm"]=norm(_r["brand"])
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
def fetch_detail_browser_html(url):
    """Récupère le HTML rendu par Chromium pour les pages chargées en JavaScript."""
    if not PLAYWRIGHT_AVAILABLE:
        return None, None
    def run_browser():
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page=browser.new_page(user_agent="Mozilla/5.0 (compatible; MastersCoteReprise/1.2)")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            try: page.wait_for_load_state("networkidle", timeout=7000)
            except Exception: pass
            html=page.content(); final_url=page.url; browser.close()
            return html, final_url
    try:
        return run_browser()
    except Exception as first_error:
        # Sur Render, Chromium peut ne pas encore être présent après pip install.
        # Installation à la demande, uniquement si le premier lancement échoue.
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return run_browser()
        except Exception:
            return None, None

def fetch_detail_price_km(r, target_year=None, target_model=None, target_km=None):
    """
    Scraping strict d'une page d'annonce.

    Principe :
    - ne jamais prendre arbitrairement le premier prix de la page ;
    - priorité aux données structurées de l'offre (JSON-LD / meta) ;
    - vérification modèle + année ;
    - association prix/kilométrage par proximité dans le bloc de l'offre ;
    - conservation du prix brut exact et de sa source pour empêcher qu'un
      prix d'une autre annonce/version soit réutilisé.
    """
    url=str(r.get("link","") or "").strip()
    if not url or not url.startswith(("http://","https://")):
        return None
    try:
        resp=requests.get(
            url,
            headers={"User-Agent":"Mozilla/5.0 (compatible; MastersCoteReprise/1.1)"},
            timeout=6,
            allow_redirects=True
        )
        if resp.status_code != 200 or not resp.text:
            return None
        html=resp.text
    except requests.RequestException:
        resp=None
        html=None

    # Fallback navigateur pour les pages dont le contenu est généré en JavaScript.
    browser_url=getattr(resp, "url", url) if resp is not None else url
    if not html and PLAYWRIGHT_AVAILABLE:
        html, browser_url = fetch_detail_browser_html(url)
    if not html:
        return None

    page_title=""
    mt=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S)
    if mt:
        page_title=re.sub(r"\s+"," ",unescape(re.sub(r"<[^>]+>"," ",mt.group(1)))).strip()

    model_norm=norm(target_model or "")
    page_years=extract_years(page_title)
    title_model_ok=bool(model_norm and model_norm in norm(page_title))
    title_year_ok=bool(target_year is None or target_year in page_years)

    def valid_text(txt):
        txt=re.sub(r"\s+"," ",unescape(re.sub(r"<[^>]+>"," ",str(txt or "")))).strip()
        return txt

    def score_identity(name="", desc="", year=None, km=None):
        txt=f"{name} {desc} {page_title}"
        score=0.0
        if model_norm and model_norm in norm(txt):
            score += 100
        elif model_norm:
            return -1000
        years=extract_years(txt)
        if target_year is not None:
            if target_year in years:
                score += 80
            elif years:
                score -= 100
        if target_km is not None and km is not None:
            d=abs(int(km)-target_km)
            score += max(0,100-d/250)
        return score

    candidates=[]

    def add_candidate(price, km=None, year=None, name="", desc="", source="unknown", base=0):
        try:
            if isinstance(price,dict):
                price=price.get("price")
            price=int(float(str(price).replace(" ","").replace(",", ".")))
        except (TypeError,ValueError):
            return
        if not 10000<=price<=150000:
            return
        km0=None
        try:
            if km is not None:
                km0=clean_num(km)
                if not 0<=km0<=300000:
                    km0=None
        except Exception:
            km0=None
        sc=base+score_identity(name,desc,year,km0)
        if sc < 0:
            return
        candidates.append({
            "score":sc,"price":price,"km":km0,
            "year":year,"name":str(name or ""),
            "source":source
        })

    # 1) JSON-LD : on ne retient une offre que si son produit correspond à
    # l'annonce recherchée. C'est la source la plus fiable quand disponible.
    for raw_json in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',html,re.I|re.S):
        try:
            data=json.loads(unescape(raw_json))
        except Exception:
            continue
        stack=data if isinstance(data,list) else [data]
        while stack:
            obj=stack.pop()
            if isinstance(obj,list):
                stack.extend(obj)
                continue
            if not isinstance(obj,dict):
                continue
            name=str(obj.get("name","") or "")
            desc=str(obj.get("description","") or "")
            offers=obj.get("offers")
            if isinstance(offers,dict):
                offers=[offers]
            if isinstance(offers,list):
                for offer in offers:
                    if not isinstance(offer,dict):
                        continue
                    price=offer.get("price")
                    if price is None:
                        continue
                    combined=f"{name} {desc}"
                    kms=extract_kms(combined)
                    yrs=extract_years(combined)
                    add_candidate(
                        price,
                        kms[0] if kms else None,
                        yrs[0] if yrs else None,
                        name,desc,"jsonld",260
                    )
            stack.extend(v for v in obj.values() if isinstance(v,(dict,list)))

    # 2) Meta prix : uniquement si le titre de la page identifie le modèle
    # et l'année. Cela évite les pages catalogue.
    if title_model_ok and title_year_ok:
        for tag in re.findall(r"<meta[^>]+>",html,re.I|re.S):
            nm=re.search(r'(?:property|name)=["\']([^"\']+)["\']',tag,re.I)
            ct=re.search(r'content=["\']([^"\']+)["\']',tag,re.I)
            if not nm or not ct:
                continue
            key=nm.group(1).lower()
            if key in ("product:price:amount","og:price:amount","price"):
                add_candidate(ct.group(1),target_km,target_year,page_title,"","meta",190)

    # 3) Blocs HTML : on cherche des éléments qui ressemblent à une offre
    # (price/prix + kilométrage) et on score le bloc entier. On ne prend plus
    # jamais "le premier prix après le h1".
    blocks=[]
    patterns=[
        r'<[^>]+(?:class|id)=["\'][^"\']*(?:price|prix|offer|offre|vehicle|vehicule|product|annonce)[^"\']*["\'][^>]*>.*?</[^>]+>',
        r'<(?:article|section|li|div)[^>]*>.*?</(?:article|section|li|div)>'
    ]
    for pat in patterns:
        try:
            blocks.extend(re.findall(pat,html,re.I|re.S))
        except re.error:
            pass

    # Limite les blocs et déduplique les textes.
    seen_blocks=set()
    for raw in blocks[:2500]:
        txt=valid_text(raw)
        key=norm(txt[:2000])
        if not txt or key in seen_blocks:
            continue
        seen_blocks.add(key)
        if len(txt)>12000:
            txt=txt[:12000]
        prices=extract_prices(txt)
        kms=extract_kms(txt)
        years=extract_years(txt)
        if not prices:
            continue
        # Un bloc doit identifier le modèle ou, au minimum, l'année cible.
        identity=score_identity(txt,"",years[0] if years else None,kms[0] if kms else None)
        if identity < 0:
            continue
        for p in prices:
            nearest_km=min(kms,key=lambda k:abs(k-(target_km or k))) if kms else None
            nearest_year=min(years,key=lambda y:abs(y-(target_year or y))) if years else None
            add_candidate(p,nearest_km,nearest_year,txt[:300],txt,"html-block",120+max(0,identity))

    # 4) Dernier filet : texte autour du modèle. Les prix sont alors associés
    # au kilométrage le plus proche, jamais à un prix provenant d'une autre
    # occurrence éloignée dans la page.
    if title_model_ok and title_year_ok:
        text=valid_text(html)
        low=text.lower()
        model_match=re.search(re.escape(str(target_model)),low,re.I) if target_model else None
        if not model_match and target_model:
            compact=re.sub(r"\s+",r"\\s*",re.escape(str(target_model)))
            model_match=re.search(compact,low,re.I)
        if model_match:
            window=low[max(0,model_match.start()-1800):min(len(low),model_match.end()+3000)]
            prices=extract_prices(window)
            kms=extract_kms(window)
            years=extract_years(window)
            for p in prices:
                nearest_km=min(kms,key=lambda k:abs(k-(target_km or k))) if kms else None
                nearest_year=min(years,key=lambda y:abs(y-(target_year or y))) if years else None
                add_candidate(p,nearest_km,nearest_year,page_title,window,"model-window",80)

    # Second passage avec Chromium si requests n'a pas trouvé de prix fiable.
    if not candidates and PLAYWRIGHT_AVAILABLE:
        browser_html, browser_url = fetch_detail_browser_html(url)
        if browser_html and browser_html != html:
            visible=unescape(re.sub(r"<[^>]+>", " ", browser_html))
            visible=re.sub(r"\s+", " ", visible)
            low=visible.lower()
            if target_model:
                mm=re.search(re.escape(str(target_model)), low, re.I)
                if not mm:
                    compact=re.sub(r"\s+", r"\\s*", re.escape(str(target_model)))
                    mm=re.search(compact, low, re.I)
                if mm:
                    window=low[max(0,mm.start()-2000):min(len(low),mm.end()+4000)]
                    prices=extract_prices(window); kms=extract_kms(window); years=extract_years(window)
                    for p in prices:
                        nearest_km=min(kms,key=lambda k:abs(k-(target_km or k))) if kms else None
                        nearest_year=min(years,key=lambda y:abs(y-(target_year or y))) if years else None
                        add_candidate(p, nearest_km, nearest_year, page_title, window, "playwright-text", 120)

    if not candidates:
        return None

    # Déduplication des mêmes valeurs/source puis sélection par identité,
    # proximité kilométrique et fiabilité de la source.
    best={}
    for c in candidates:
        key=(c["price"],c["km"],c["source"],c["name"][:120])
        if key not in best or c["score"]>best[key]["score"]:
            best[key]=c
    candidates=list(best.values())
    candidates.sort(
        key=lambda c:(
            c["score"],
            1 if c["source"]=="jsonld" else 0,
            -abs((c["km"] if c["km"] is not None else target_km or 0)-(target_km or c["km"] or 0))
        ),
        reverse=True
    )
    chosen=candidates[0]

    # Une page qui ne confirme pas l'année dans le titre/élément sélectionné
    # ne doit pas fournir un prix au moteur.
    chosen_years=extract_years(f"{chosen['name']} {page_title}")
    if target_year is not None and chosen_years and target_year not in chosen_years:
        return None

    return {
        "price":chosen["price"],
        "km":chosen["km"],
        "year":chosen["year"] if chosen["year"] is not None else (target_year if title_year_ok else None),
        "title":page_title,
        "source":"detail",
        "price_source":chosen["source"],
        "price_evidence":chosen["name"][:500],
        "url":browser_url
    }

def parse_price_km(r, target_year=None, target_km=None):
    # Si la page détail a été consultée, son prix prime toujours sur le
    # prix extrait du snippet Google/Serper.
    detail=r.get("_detail") or {}
    if detail.get("price"):
        return int(detail["price"]), (int(detail["km"]) if detail.get("km") is not None else None)
    title=str(r.get("title","") or "")
    snippet=str(r.get("snippet","") or "")
    text=f"{title} {snippet}"
    candidates=[]
    patterns=[r"(\d{2,3}(?:[ .]\d{3})+|\d{4,6})\s*€",r"€\s*(\d{2,3}(?:[ .]\d{3})+|\d{4,6})"]
    for pat in patterns:
        for m in re.finditer(pat,text):
            try:
                value=clean_num(m.group(1))
            except ValueError:
                continue
            if not 10000<=value<=150000:
                continue
            pos=m.start()
            local=text[max(0,pos-260):min(len(text),m.end()+260)].lower()
            score=0.0
            # Un prix présent dans le titre est prioritaire : les snippets
            # peuvent mélanger plusieurs annonces d'une même page.
            if pos < len(title):
                score += 80
            if target_km is not None:
                km_matches=list(re.finditer(r"\b(\d{1,3}(?:[ .]\d{3})|\d{3,6})\s*km\b",text.lower()))
                if km_matches:
                    nearest=min(km_matches,key=lambda k:abs(pos-k.start()))
                    distance=abs(pos-nearest.start())
                    km_value=clean_num(nearest.group(1))
                    if km_value==target_km:
                        score += 140
                        score += max(0,60-distance/10)
                    else:
                        score -= min(80,abs(km_value-target_km)/500)
            if target_year is not None:
                ymatches=[int(m.group()) for m in re.finditer(r"\b20(?:1\d|2[0-9])\b",local)]
                if target_year in ymatches:
                    score += 55
                elif ymatches and target_year not in ymatches:
                    score -= 70
            if re.search(r"\b(?:neuf|neuve|2025|2026)\b",local) and target_year not in (2025,2026):
                score -= 60
            if value not in [x[0] for x in candidates]:
                candidates.append((value,score))
    if candidates:
        candidates.sort(key=lambda x:(x[1],-x[0]),reverse=True)
        price=candidates[0][0]
    else:
        ps=extract_prices(text)
        price=ps[0] if ps else None
    ks=extract_kms(text)
    return price,(ks[0] if ks else None)
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
    """Matching generique de la reference modele."""
    raw=(text or "").lower()
    model=(model or "").strip().lower()
    if not model:
        return 0
    compact=norm(model)
    compact_text=norm(raw)
    if not compact:
        return 0
    words=re.findall(r"[a-z0-9]+",compact_text)
    wanted=re.findall(r"[a-z0-9]+",norm(model))
    if model.isdigit():
        return 48 if re.search(rf"(?<!\\d){re.escape(model)}(?!\\d)",raw) else 0
    if compact in compact_text:
        return 48
    if len(wanted)>1 and all(w in words for w in wanted):
        return 48
    return 0

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

def price_associated_year(r, price, target_year, target_km=None):
    """Détermine l'année réellement associée au prix dans un snippet.
    Un résultat Google peut contenir plusieurs véhicules/dates ; on prend
    l'année la plus proche du prix retenu, pas simplement n'importe quelle
    année présente dans le snippet.
    """
    title=str(r.get("title","") or "")
    snippet=str(r.get("snippet","") or "")
    text=f"{title} {snippet}"
    price_positions=[]
    patterns=[r"(\d{2,3}(?:[ .]\d{3})+|\d{4,6})\s*€",r"€\s*(\d{2,3}(?:[ .]\d{3})+|\d{4,6})"]
    for pat in patterns:
        for m in re.finditer(pat,text):
            try:
                v=clean_num(m.group(1))
            except ValueError:
                continue
            if v==price:
                price_positions.append(m.start())
    if not price_positions:
        return None
    years=[]
    for m in re.finditer(r"\b20(?:1\d|2[0-9])\b",text):
        years.append((m.start(),int(m.group())))
    if not years:
        return None
    # Si plusieurs occurrences du même prix existent, choisir celle la plus
    # proche du kilométrage cible lorsqu'il est disponible.
    pos=price_positions[0]
    if len(price_positions)>1 and target_km is not None:
        km_positions=[]
        for m in re.finditer(r"\\b(\\d{1,3}(?:[ .]\\d{3})|\\d{3,6})\\s*km\\b",text.lower()):
            try:
                kv=clean_num(m.group(1))
            except ValueError:
                continue
            km_positions.append((m.start(),kv))
        if km_positions:
            pos=min(price_positions,key=lambda p:min(abs(p-kp) for kp,kv in km_positions))
    nearest=min(years,key=lambda y:abs(y[0]-pos))
    # Une année très éloignée du prix n'est pas une association fiable.
    return nearest[1] if abs(nearest[0]-pos)<=220 else None

def comparable_row(r,brand,model,year,target_km,hp=None,transmission=None,dica_gamme=None,category="camping"):
    price,rkm=parse_price_km(r,year,target_km)
    text=f"{r.get('title','')} {r.get('snippet','')}"
    title_years=extract_years(str(r.get("title","")))
    snippet_years=extract_years(str(r.get("snippet","")))
    # Année stricte : si le titre porte une année différente, l'annonce est
    # hors cible. Sinon, l'année doit être associée au prix retenu ; la simple
    # présence de 2022 quelque part dans un snippet multi-annonces ne suffit
    # plus.
    if title_years and year not in title_years:
        return None
    associated_year=price_associated_year(r,price,year,target_km) if price else None
    if associated_year is not None and associated_year!=year:
        return None
    if associated_year is None and year not in snippet_years:
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
    # Les sites d'annonces écrivent souvent les modèles différemment
    # (ex. « MC4 262 » / « MC 4 262 » / « MC LOUIS MC4 262 »).
    # On multiplie les formulations de recherche, mais le filtrage final
    # reste strict sur le modèle, l'année et la catégorie.
    model_compact=re.sub(r"\s+","",str(model or ""))
    model_spaced=re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"," ",str(model or ""))
    model_variants=[]
    for mv in (str(model).strip(),model_compact,model_spaced.strip()):
        if mv and mv.lower() not in [x.lower() for x in model_variants]:
            model_variants.append(mv)
    # Recherche volontairement courte : on privilégie quelques requêtes très ciblées
    # plutôt qu'une longue série d'appels Serper séquentiels.
    queries=[
        f'"{brand} {model}" {year} {market_term} occasion',
        f'"{brand} {model}" {year} "{km} km" {market_term} occasion',
        f'"{brand} {model}" {year} {market_term} prix occasion',
        f'site:leboncoin.fr "{brand} {model}" {year} {market_term}'
    ]
    # Si une gamme DICA précise existe, on ajoute une recherche ciblée.
    if dica_gamme:
        queries.append(f'"{brand} {model}" "{dica_gamme}" {year} {market_term} occasion')
    queries=list(dict.fromkeys(queries))
    # Déduplication des requêtes pour ne pas gaspiller les appels Serper.
    queries=list(dict.fromkeys(queries))
    results=[]
    def serper_search(q):
        try:
            resp=requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY":SERPER_API_KEY,"Content-Type":"application/json"},
                json={"q":q,"gl":"fr","hl":"fr","num":10},
                timeout=10
            )
            resp.raise_for_status()
            return q, resp.json().get("organic",[])
        except requests.RequestException:
            return q, []
    # Les recherches indépendantes sont lancées en parallèle pour éviter que
    # plusieurs appels réseau successifs fassent dépasser le délai de l'interface.
    with ThreadPoolExecutor(max_workers=min(5,len(queries))) as pool:
        futures=[pool.submit(serper_search,q) for q in queries]
        for fut in as_completed(futures):
            q,items=fut.result()
            for item in items:
                item["source"]=q
                results.append(item)
    uniq={r["link"]:r for r in results if r.get("link")}
    dedup={}
    for r in uniq.values():
        key=(norm(r.get("title","")),parse_price_km(r,year,km)[0],parse_price_km(r,year,km)[1])
        if key[0]: dedup[key]=r

    # IMPORTANT : un résultat Google/Serper peut être une page catalogue
    # dont le snippet mélange plusieurs véhicules. On ouvre maintenant beaucoup
    # plus largement les candidats et, surtout, les résultats qui contiennent
    # l'année + le kilométrage cible. Le prix retenu doit venir de la page de
    # l'annonce lorsque celle-ci est accessible, jamais d'un autre véhicule
    # présent sur la même page.
    detail_candidates=[]
    for r in dedup.values():
        prelim=score_result(r,brand,model,year,km,hp,transmission,dica_gamme,category)
        text_pre=f"{r.get('title','')} {r.get('snippet','')}"
        text_pre_low=text_pre.lower()
        years_pre=extract_years(text_pre)
        kms_pre=extract_kms(text_pre)
        exact_km=(km in kms_pre)
        exact_year=(year in years_pre)
        # Les pages d'agrégation sont également explorées : certaines
        # contiennent une annonce exacte dans leur liste et le moteur de
        # recherche peut ne pas fournir directement sa page détail.
        if prelim>=45 and (not years_pre or exact_year) and (exact_km or exact_year or not is_aggregation(r)):
            bonus=(90 if exact_km else 0)+(35 if exact_year else 0)
            detail_candidates.append((prelim+bonus,r))

    detail_candidates.sort(key=lambda x:x[0],reverse=True)
    # 80 pages maximum : suffisamment large pour ne plus rater une annonce
    # pertinente cachée derrière plusieurs résultats de recherche, tout en
    # gardant un temps de réponse raisonnable.
    # Scraping des pages détail en parallèle : les requêtes HTTP restent rapides
    # et on évite de bloquer 10 fois 6 secondes l'une après l'autre.
    def scrape_candidate(r):
        detail=fetch_detail_price_km(r,year,model,km)
        if not detail or not detail.get("price"):
            return None
        detail_year=detail.get("year")
        detail_title=str(detail.get("title","") or "")
        if detail_year is not None and detail_year!=year:
            return None
        if model and norm(model) not in norm(detail_title):
            compact_model=norm(re.sub(r"(?<=[A-Za-z])(?=\\d)|(?<=\\d)(?=[A-Za-z])"," ",model))
            if compact_model not in norm(detail_title):
                return None
        return r,detail

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(scrape_candidate,r) for _,r in detail_candidates[:4]]
        for fut in as_completed(futures):
            try:
                result=fut.result()
            except Exception:
                result=None
            if result:
                r,detail=result
                r["_detail"]=detail

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
        experimental_professional_value = dica[0]["reprise_corrigee"] if len(dica)==1 else None
        return jsonify({
            "status":"insufficient","category":category,"ptac":ptac,
            "category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),
            "dica_gamme":dica_gamme,"comparables":primary,"context":context[:5],"dica":dica,
            "dica_ambiguous":len(dica)>1,
            "dica_near":dica_near_matches(brand,model,year,km,hp,options_total,category) if (not dica and category!="poids_lourd") else [],
            "experimental_recalage_factor":DICA_RECALAGE_FACTOR,
            "experimental_market_gap":None,
            "experimental_professional_value":experimental_professional_value,
            "quality":{"comparables":len(values),"km_comparables":len(values),"sans_km":len(context),"atypiques":0,"transmission_fallback":transmission_fallback},
            "message":"Marché insuffisant : le moteur n'a pas encore trouvé 3 comparables qualifiés. Aucune recherche secondaire n'est lancée."
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
        experimental_professional_value=round(dica[0]["reprise_corrigee"])
    return jsonify({"status":"ok","category":category,"ptac":ptac,"category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),"comparables":primary,"context":context[:5],"market":market,"market_low":market_low,"market_high":market_high,"search_time":search_time,"fiscal_cv":cv,"horsepower":hp,"transmission":transmission,"transmission_fallback":transmission_fallback,"transmission_counts":transmission_counts,"transmission_gap":transmission_gap,"trade":max(0,market-MASTERS_FRAIS),"masters_frais":MASTERS_FRAIS,"confidence":confidence,"count":len(filtered),"excluded_count":len(excluded_values),"dica":dica,"dica_ambiguous":len(dica)>1,"dica_near":dica_near,"quality":{"comparables":len(filtered),"atypiques":len(excluded_values),"transmission_fallback":transmission_fallback,"km_comparables":sum(1 for x in primary if x.get("km") is not None),"sans_km":len(context)},"experimental_recalage_factor":DICA_RECALAGE_FACTOR,"experimental_market_gap":experimental_market_gap,"experimental_professional_value":experimental_professional_value,"accessories":option_details,"accessories_value":options_total,"dica_reference_km":(dica[0]["reference_km"] if dica else (dica_near[0]["reference_km"] if dica_near else (None if category=="poids_lourd" else dica_ref_km(year, "V" if category=="van" else "F" if category=="fourgon" else None)))),"dica_edition":"Cote Officielle de l’Occasion n°32 — janvier à avril 2026"})
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
