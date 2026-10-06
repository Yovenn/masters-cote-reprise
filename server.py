import os, re, statistics, json, requests, unicodedata, subprocess, sys
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE=True
except Exception:
    curl_requests=None
    CURL_CFFI_AVAILABLE=False
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = False
except Exception:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False
from html import unescape
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
try:
    from selectolax.lexbor import LexborHTMLParser
    SELECTOLAX_AVAILABLE = True
except Exception:
    LexborHTMLParser = None
    SELECTOLAX_AVAILABLE = False

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

def finish_alias_match(text, requested_gamme, model=""):
    """Vérifie qu'une annonce porte bien la finition/gamme DICA demandée.
    Le modèle reste traité séparément ; ici on contrôle uniquement la gamme.
    """
    target=norm(requested_gamme)
    if not target:
        return True
    hay=norm(text)
    # Une gamme composée doit retrouver chacun de ses mots significatifs.
    raw_tokens=re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(requested_gamme or "")).encode("ascii","ignore").decode("ascii").lower())
    tokens=[t for t in raw_tokens if len(t)>=3]
    if not tokens:
        return True
    # Tolère les petites fautes OCR/annonce sur les mots longs (ex. ULTIMAT).
    for tok in tokens:
        if tok in hay:
            continue
        if len(tok)>=6 and any(abs(len(tok)-len(x))<=1 and tok[:6]==x[:6] for x in re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(text or "")).encode("ascii","ignore").decode("ascii").lower())):
            continue
        return False
    return True

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
    # Les sites d'annonces (notamment Leboncoin) utilisent souvent des espaces
    # insécables/narrow no-break (U+00A0/U+202F) dans les prix : 59\u202f900 €.
    # Ils doivent être traités comme des séparateurs de milliers, exactement
    # comme un espace classique ou un point.
    sep=r"[ .\u00a0\u202f]"
    patterns=[
        rf"(\d{{2,3}}(?:{sep}\d{{3}})+|\d{{4,6}})\s*€",
        rf"€\s*(\d{{2,3}}(?:{sep}\d{{3}})+|\d{{4,6}})"
    ]
    for p in patterns:
        for m in re.finditer(p,text):
            try:
                v=clean_num(m.group(1))
                if 10000<=v<=150000 and v not in out:
                    out.append(v)
            except (TypeError,ValueError):
                pass
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

def fetch_detail_price_km(r, target_year=None, target_model=None, target_km=None, target_gamme=None):
    """
    Scraping léger et strict d'une page d'annonce.

    Le HTML est parsé une seule fois avec Selectolax. Une valeur prix/km n'est
    acceptée que si elle appartient au même bloc DOM que le modèle recherché.
    Sur une page catalogue, on ne remonte jamais au conteneur parent qui
    contient plusieurs annonces.
    """
    url=str(r.get("link","") or "").strip()
    if not url or not url.startswith(("http://","https://")):
        return None

    try:
        resp=requests.get(
            url,
            headers={"User-Agent":"Mozilla/5.0 (compatible; MastersCoteReprise/1.2)"},
            timeout=6,
            allow_redirects=True
        )
        if resp.status_code != 200 or not resp.text:
            return None
        html=resp.text
        browser_url=resp.url

        # Leboncoin peut répondre 200 en redirigeant une annonce supprimée
        # vers une page générique. Cette page ne doit jamais être considérée
        # comme la fiche d'origine.
        try:
            from urllib.parse import urlparse
            orig=urlparse(url)
            final=urlparse(browser_url)
            if "leboncoin.fr" in orig.netloc.lower():
                if "leboncoin.fr" not in final.netloc.lower():
                    return None
                if final.path.rstrip("/") != orig.path.rstrip("/"):
                    return None
                if "/ad/" not in final.path.lower():
                    return None
        except Exception:
            return None
    except requests.RequestException:
        return None

    if not SELECTOLAX_AVAILABLE:
        return None

    try:
        tree=LexborHTMLParser(html)
    except Exception:
        return None

    def node_text(node):
        try:
            return re.sub(r"\s+"," ",node.text(separator=" ",strip=True)).strip()
        except Exception:
            return ""

    def attr(node,name):
        try:
            return str(node.attributes.get(name,"") or "")
        except Exception:
            return ""

    title_node=tree.css_first("title")
    page_title=node_text(title_node) if title_node else ""
    model_norm=norm(target_model or "")
    gamme_norm=norm(target_gamme or "")
    title_norm=norm(page_title)
    page_years=extract_years(page_title)
    title_model_ok=bool(model_norm and model_norm in title_norm)
    title_year_ok=bool(target_year is None or target_year in page_years)

    url_low=browser_url.lower()
    catalogue_markers=("/recherche","/search","/listing","/annonces","?q=","&q=","resultats","results","/stock")

    body=tree.css_first("body")
    body_text=node_text(body) if body else ""
    page_card_count=len(re.findall(
        r"\bchallenger\s+(?:graphite\s+|start\s+|break\s+|etape\s+|étape\s+|premium\s+)?328\b",
        body_text,re.I
    ))
    probable_catalogue=any(m in url_low for m in catalogue_markers) or page_card_count>3

    def score_identity(text, km=None):
        nt=norm(text)
        if model_norm and model_norm not in nt:
            return -1000
        # Les sites d'annonces raccourcissent parfois une finition DICA :
        # « GRAPHITE EDITION PREMIUM – 328 » devient « Graphite Premium 328 ».
        gamme_ok = (not gamme_norm) or (gamme_norm in nt) or finish_alias_match(text, target_gamme, target_model)
        if not gamme_ok:
            return -1000
        score=100.0 if model_norm else 0.0
        if gamme_norm:
            score+=140
        years=extract_years(text)
        if target_year is not None:
            if target_year in years:
                score+=80
            elif years:
                return -1000
        if target_km is not None and km is not None:
            score+=max(0,100-abs(int(km)-target_km)/250)
        return score

    candidates=[]

    def add_candidate(price, km=None, year=None, text="", source="unknown", base=0):
        try:
            if isinstance(price,dict):
                price=price.get("price")
            price=int(float(str(price).replace(" ","").replace(",", ".")))
        except (TypeError,ValueError):
            return
        if not 10000<=price<=150000:
            return
        km0=None
        if km is not None:
            try:
                km0=clean_num(km)
                if not 0<=km0<=300000:
                    km0=None
            except Exception:
                km0=None
        sc=base+score_identity(text,km0)
        if sc<0:
            return
        candidates.append({
            "score":sc,"price":price,"km":km0,"year":year,
            "name":text[:500],"source":source
        })

    # 1) JSON-LD : on exploite uniquement les produits qui identifient
    # réellement le modèle. Selectolax évite le regex HTML sur tout le document.
    for node in tree.css('script[type="application/ld+json"]'):
        raw=node.text()
        try:
            data=json.loads(unescape(raw))
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
            try:
                structured_text=json.dumps(obj,ensure_ascii=False)
            except Exception:
                structured_text=""
            combined=f"{name} {desc} {structured_text}"
            offers=obj.get("offers")
            if isinstance(offers,dict):
                offers=[offers]
            if isinstance(offers,list):
                for offer in offers:
                    if not isinstance(offer,dict) or offer.get("price") is None:
                        continue
                    kms=extract_kms(combined)
                    yrs=extract_years(combined)
                    # Sur une fiche individuelle, l'Offer JSON-LD porte souvent
                    # le prix alors que le kilométrage est dans un autre bloc.
                    # Le prix reste néanmoins rattaché à CETTE fiche : on peut
                    # donc conserver l'Offer si elle identifie explicitement
                    # le modèle et l'année. Le kilométrage pourra venir du
                    # même document ou du résultat Serper de cette URL.
                    identity_text=f"{name} {desc}"
                    if model_norm and model_norm not in norm(identity_text):
                        continue
                    # La date peut être absente du JSON-LD de l'Offer
                    # alors qu'elle est bien présente dans le titre de la fiche.
                    # On utilise donc aussi le titre de page comme preuve d'année.
                    if target_year is not None and target_year not in yrs and target_year not in page_years:
                        continue
                    add_candidate(
                        offer.get("price"),
                        kms[0] if kms else None,
                        yrs[0] if yrs else (target_year if target_year in page_years else None),
                        combined + " " + page_title,
                        "jsonld",
                        260
                    )
            stack.extend(v for v in obj.values() if isinstance(v,(dict,list)))

    # 2) Blocs DOM : on cible les cartes d'annonce avant les conteneurs généraux.
    # Une carte doit contenir modèle + année + prix + kilométrage.
    selectors=(
        "article",
        "li",
        "[class*='card']",
        "[class*='Card']",
        "[class*='vehicle']",
        "[class*='Vehicle']",
        "[class*='annonce']",
        "[class*='Annonce']",
        "[class*='product']",
        "[class*='Product']",
        "[class*='offer']",
        "[class*='Offer']"
    )
    seen=set()
    for node in tree.css(",".join(selectors)):
        txt=node_text(node)
        if not txt or len(txt)>8000:
            continue
        key=norm(txt[:1800])
        if key in seen:
            continue
        seen.add(key)
        prices=extract_prices(txt)
        kms=extract_kms(txt)
        years=extract_years(txt)
        if not prices or not kms:
            continue

        # Une carte valide doit contenir le modèle, la finition DICA,
        # l’année et le kilométrage. On ne rejette PAS une carte parce que
        # le modèle apparaît plusieurs fois : certains sites répètent le titre
        # dans des éléments cachés/SEO d’une seule et même annonce.
        identity=score_identity(txt,kms[0])
        if identity<0:
            continue
        if target_year is not None and target_year not in years:
            continue

        # Si le bloc contient plusieurs véhicules distincts, on refuse le bloc
        # parent. Indice robuste : plusieurs kilométrages ET plusieurs années.
        if len(set(kms))>1 and len(set(years))>1:
            continue
        # Le kilométrage et le prix restent dans CE bloc DOM.
        nearest_km=min(kms,key=lambda k:abs(k-(target_km if target_km is not None else k)))
        nearest_year=min(years,key=lambda y:abs(y-target_year)) if years and target_year is not None else (years[0] if years else None)
        for p in prices:
            add_candidate(p,nearest_km,nearest_year,txt,"dom-card",180+identity)

    # 3) Pas de meta prix seul : sans kilométrage dans le même bloc,
    # le moteur risquerait d'associer le prix d'une annonce au kilométrage
    # fourni par la recherche. Cette association est interdite.

    # 4) Sur une vraie page détail sans carte exploitable, on accepte le JSON-LD
    # ou les meta uniquement. Pas de fenêtre de texte globale : c'est précisément
    # ce qui créait les mélanges entre 57 990 € et 74 990 €.
    if not candidates:
        return None

    if probable_catalogue:
        # Une page catalogue n'est admise que si une carte DOM ou une donnée
        # structurée identifie explicitement l'offre. On élimine les JSON-LD
        # génériques de catalogue qui n'ont pas le modèle dans leur nom/description.
        strict=[c for c in candidates if c["source"] in ("dom-card","jsonld","meta")]
        if not strict:
            return None
        candidates=strict

    # Si plusieurs cartes existent, le score d'identité + proximité km décide.
    # En cas d'égalité, on privilégie la carte DOM plutôt qu'une donnée globale.
    candidates.sort(
        key=lambda c:(
            c["score"],
            1 if c["source"]=="dom-card" else 0,
            1 if c["source"]=="jsonld" else 0,
            -abs((c["km"] if c["km"] is not None else (target_km or 0))-(target_km or c["km"] or 0))
        ),
        reverse=True
    )
    chosen=candidates[0]

    chosen_text=chosen["name"]
    chosen_years=extract_years(chosen_text+" "+page_title)
    if target_year is not None and chosen_years and target_year not in chosen_years:
        return None
    if model_norm and model_norm not in norm(chosen_text+" "+page_title):
        return None

    return {
        "price":chosen["price"],
        "km":chosen["km"],
        "year":chosen["year"] if chosen["year"] is not None else (target_year if target_year in chosen_years else None),
        "title":page_title,
        "source":"detail",
        "price_source":chosen["source"],
        "price_evidence":chosen["name"][:500],
        "url":browser_url
    }

def fetch_selected_ad_data(r, target_year=None, target_model=None):
    """Récupère le kilométrage d'une annonce choisie manuellement, même si
    le résultat Serper initial ne l'affichait pas.
    
    Pour une sélection manuelle, on assouplit la finition : le vendeur a déjà
    validé visuellement l'annonce. On exige néanmoins que la fiche identifie
    bien le modèle et l'année recherchés avant d'utiliser son kilométrage.
    """
    url=str(r.get("link","") or "").strip()
    if not url or not url.startswith(("http://","https://")) or not SELECTOLAX_AVAILABLE:
        return None
    try:
        resp=requests.get(
            url,
            headers={"User-Agent":"Mozilla/5.0 (compatible; MastersCoteReprise/1.2)"},
            timeout=7,
            allow_redirects=True
        )
        if resp.status_code != 200 or not resp.text:
            return None
    except requests.RequestException:
        return None
    try:
        tree=LexborHTMLParser(resp.text)
    except Exception:
        return None

    def txt(node):
        try:
            return re.sub(r"\s+"," ",node.text(separator=" ",strip=True)).strip()
        except Exception:
            return ""

    title=txt(tree.css_first("title")) if tree.css_first("title") else ""
    h1=txt(tree.css_first("h1")) if tree.css_first("h1") else ""
    meta_desc=""
    md=tree.css_first('meta[name="description"]')
    if md:
        try: meta_desc=str(md.attributes.get("content","") or "")
        except Exception: pass
    og_title=""
    og=tree.css_first('meta[property="og:title"]')
    if og:
        try: og_title=str(og.attributes.get("content","") or "")
        except Exception: pass

    focused=" ".join(x for x in (title,h1,meta_desc,og_title) if x)
    model_norm=norm(target_model or "")
    if model_norm and model_norm not in norm(focused):
        # Cherche ensuite une carte/bloc de détail qui porte explicitement
        # modèle + année + kilométrage.
        selectors=("article","main","[class*='detail']","[class*='Detail']",
                   "[class*='vehicle']","[class*='Vehicle']","[class*='annonce']",
                   "[class*='Annonce']","[class*='product']","[class*='Product']")
        for node in tree.css(",".join(selectors)):
            t=txt(node)
            if not t or len(t)>12000 or model_norm not in norm(t):
                continue
            ys=extract_years(t); ks=extract_kms(t)
            if ks and (target_year is None or target_year in ys or not ys):
                focused=t
                break
    if model_norm and model_norm not in norm(focused):
        return None
    years=extract_years(focused)
    if target_year is not None and years and target_year not in years:
        return None
    kms=extract_kms(focused)
    if not kms:
        # Dernier recours : petits blocs contenant explicitement "km" et le modèle.
        for node in tree.css("h1,h2,h3,p,li,div,span"):
            t=txt(node)
            if not t or len(t)>1800 or model_norm and model_norm not in norm(t):
                continue
            ys=extract_years(t); ks=extract_kms(t)
            if ks and (target_year is None or target_year in ys or not ys):
                kms=ks
                if years==[] and ys: years=ys
                break
    if not kms:
        return None
    prices=extract_prices(focused)
    return {
        "price":prices[0] if prices else None,
        "km":kms[0],
        "year":target_year if target_year in years else (years[0] if years else None),
        "title":title or h1,
        "source":"selection_detail",
        "url":resp.url
    }

def parse_price_km(r, target_year=None, target_km=None):
    if r.get("price_source") == "lbc_finder_browser":
        p=r.get("price"); k=r.get("km")
        return (int(p) if p is not None else 0), (int(k) if k is not None else None)
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
        return 48 if re.search(rf"(?<!\d){re.escape(model)}(?!\d)",raw) else 0
    if compact in compact_text:
        return 48
    if len(wanted)>1 and all(w in words for w in wanted):
        return 48
    return 0

def finish_alias_match(text, requested_gamme, model=""):
    """Reconnaît les abréviations de finition utilisées par les sites d'annonces.
    Exemple : GRAPHITE EDITION PREMIUM – 328 -> « Graphite Premium 328 ».
    """
    if not requested_gamme:
        return False
    raw = unicodedata.normalize("NFKD", str(requested_gamme)).encode("ascii","ignore").decode("ascii").lower()
    model_raw = unicodedata.normalize("NFKD", str(model or "")).encode("ascii","ignore").decode("ascii").lower()
    tokens = re.findall(r"[a-z0-9]+", raw)
    model_tokens = set(re.findall(r"[a-z0-9]+", model_raw))
    distinctive = [t for t in tokens if t not in model_tokens and t != "edition"]
    if not distinctive:
        return False
    tn = norm(text)
    return all(norm(t) in tn for t in distinctive)


def dica_gamme_score(text,brand,model,year,requested_gamme,category="camping"):
    """
    Filtrage strict de finition/gamme.

    Quand une gamme DICA précise est sélectionnée, une annonce qui ne donne
    pas sa finition n'est plus considérée comme comparable : nous préférons
    afficher « marché insuffisant » plutôt que mélanger des versions.
    """
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
    if matched:
        longest=max(matched,key=len)
        if longest==requested:
            return 20
        return -35

    # Accepte une abréviation non ambiguë de la même finition.
    if finish_alias_match(text, requested_gamme, model):
        return 20

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
    # Le modèle doit être présent dans le TITRE de l'annonce (ou dans le
    # titre détaillé déjà récupéré). Un snippet peut mélanger plusieurs véhicules.
    # C'est particulièrement important pour les modèles numériques (328, 270...).
    title_model_score=model_match_score(title,model)
    detail_title=str((r.get("_detail") or {}).get("title","") or "")
    detail_model_score=model_match_score(detail_title,model) if detail_title else 0
    if title_model_score<=0 and detail_model_score<=0:
        return 0
    model_score=max(title_model_score,detail_model_score)
    score+=model_score
    # Quand une finition DICA précise est sélectionnée, elle devient un
    # garde-fou pour les comparables automatiques : une Ultimate/VIP/Start
    # ne doit pas entrer dans la moyenne d'une Graphite Premium.
    # Les variantes restent disponibles dans la sélection manuelle.
    if dica_gamme:
        finish_score=dica_gamme_score(text,brand,model,year,dica_gamme,category)
        if finish_score < 0:
            return 0
        score+=finish_score
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
        for m in re.finditer(r"\b(\d{1,3}(?:[ .]\d{3})|\d{3,6})\s*km\b",text.lower()):
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

def evidence_for_result(r,brand,model,year,target_km,dica_gamme=None):
    """Retourne les preuves textuelles provenant de LA même annonce source."""
    detail=r.get("_detail") or {}
    source_text=f"{r.get('title','')} {r.get('snippet','')}".strip()
    detail_text=str(detail.get("price_evidence") or "")
    title_text=str(r.get("title","") or "")
    # Le détail peut apporter une preuve plus précise pour le prix/km, mais
    # on conserve aussi la source Serper pour vérifier qu'il s'agit bien de la même annonce.
    text_price=detail_text or source_text
    def fragment_price(text, value):
        if value is None: return None
        pat=r"(?:\d{2,3}(?:[ .]\d{3})+|\d{4,6})\s*€|€\s*(?:\d{2,3}(?:[ .]\d{3})+|\d{4,6})"
        for m in re.finditer(pat,text or ""):
            try:
                if clean_num(re.sub(r"[^0-9]","",m.group()))==int(value): return m.group(0)
            except Exception: pass
        return None
    def fragment_km(text, value):
        if value is None: return None
        for m in re.finditer(r"\b(\d{1,3}(?:[ .]\d{3})|\d{3,6})\s*km\b",text or "",re.I):
            try:
                if clean_num(m.group(1))==int(value): return m.group(0)
            except Exception: pass
        return None
    def fragment_year(text, value):
        if value is None: return None
        m=re.search(rf"\b{int(value)}\b",text or "")
        return m.group(0) if m else None
    def around(text, needle, radius=90):
        if not needle: return None
        pos=(text or "").lower().find(needle.lower())
        if pos<0: return None
        a=max(0,pos-radius); b=min(len(text),pos+len(needle)+radius)
        return re.sub(r"\s+"," ",(text or "")[a:b]).strip()
    price,rkm=parse_price_km(r,year,target_km)
    price_ev=fragment_price(text_price,price) or fragment_price(source_text,price)
    km_ev=fragment_km(text_price,rkm) or fragment_km(source_text,rkm)
    year_ev=fragment_year(title_text,year) or fragment_year(str(r.get("snippet","") or ""),year) or fragment_year(detail_text,year)
    model_ev=around(source_text,model) or around(str(detail.get("title","") or ""),model)
    finish_ev=None
    if dica_gamme:
        finish_ev=around(source_text,dica_gamme)
        if not finish_ev and finish_alias_match(source_text,dica_gamme,model):
            finish_ev=around(source_text,re.sub(r"\bedition\b","",str(dica_gamme),flags=re.I).strip())
        if not finish_ev and detail.get("title") and finish_alias_match(str(detail.get("title")),dica_gamme,model):
            finish_ev=around(str(detail.get("title")),re.sub(r"\bedition\b","",str(dica_gamme),flags=re.I).strip())
    fields={
        "prix": bool(price_ev), "kilometrage": bool(km_ev), "annee": bool(year_ev),
        "modele": bool(model_ev), "finition": (not dica_gamme) or bool(finish_ev)
    }
    return {
        "prix":{"ok":bool(price_ev),"preuve":price_ev,"contexte":around(text_price,price_ev,110)},
        "kilometrage":{"ok":bool(km_ev),"preuve":km_ev,"contexte":around(text_price,km_ev,110)},
        "annee":{"ok":bool(year_ev),"preuve":year_ev,"contexte":around(source_text,year_ev,90)},
        "modele":{"ok":bool(model_ev),"preuve":model_ev,"contexte":model_ev},
        "finition":{"ok":bool(fields["finition"]),"preuve":finish_ev,"contexte":finish_ev},
        "score":sum(1 for v in fields.values() if v),
        "source":str(r.get("link","") or r.get("url","") or detail.get("url","") or "")
    }

def comparable_row(r,brand,model,year,target_km,hp=None,transmission=None,dica_gamme=None,category="camping"):
    price,rkm=parse_price_km(r,year,target_km)
    text=f"{r.get('title','')} {r.get('snippet','')}"
    # Même garde-fou dans les comparables automatiques : le snippet ne peut
    # jamais faire passer un autre modèle pour le modèle recherché.
    title_model_ok=model_match_score(str(r.get("title","") or ""),model)>0
    detail_title=str((r.get("_detail") or {}).get("title","") or "")
    detail_model_ok=bool(detail_title and model_match_score(detail_title,model)>0)
    if not title_model_ok and not detail_model_ok:
        return None
    title_years=extract_years(str(r.get("title","")))
    snippet_years=extract_years(str(r.get("snippet","")))
    # Année stricte : si le titre porte une année différente, l'annonce est
    # hors cible. Sinon, l'année doit être associée au prix retenu ; la simple
    # présence de 2022 quelque part dans un snippet multi-annonces ne suffit
    # plus.
    market_years=(year, year+1)
    if title_years and not any(y in market_years for y in title_years):
        return None
    associated_year=price_associated_year(r,price,year,target_km) if price else None
    if associated_year is not None and associated_year not in market_years:
        return None
    if associated_year is None and not any(y in market_years for y in snippet_years):
        return None
    score=score_result(r,brand,model,year,target_km,hp,transmission,dica_gamme,category)
    if not price or score<65 or is_new(r) or is_unavailable(r): return None
    if is_aggregation(r): score-=10
    if score<65: return None

    # Aucun filtre de finition ici : une annonce peut être comparable même
    # si son titre ne précise pas la finition. La validation de la version
    # exacte reste du ressort du vendeur lors de la sélection manuelle.
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

    # Indice de fiabilité de la donnée utilisée pour la cote.
    # Il mesure la complétude de LA MÊME annonce, pas la popularité du site.
    detail=r.get("_detail") or {}
    source_text=f"{r.get('title','')} {r.get('snippet','')}"
    source_years=extract_years(source_text)
    source_kms=extract_kms(source_text)
    source_prices=extract_prices(source_text)
    evidence=evidence_for_result(r,brand,model,year,target_km,dica_gamme)
    fields={k:bool(v.get("ok")) for k,v in evidence.items() if isinstance(v,dict) and "ok" in v}
    complete=sum(1 for v in fields.values() if v)
    if detail:
        provenance="fiche_detail_verifiee"
    elif complete>=5:
        provenance="annonce_complete_source"
    elif complete>=4:
        provenance="annonce_partielle"
    else:
        provenance="resultat_recherche"

    if provenance=="fiche_detail_verifiee" and complete>=5:
        reliability="A"
    elif provenance=="annonce_complete_source":
        reliability="A"
    elif complete>=4:
        reliability="B"
    else:
        reliability="C"

    try:
        source_domain=re.sub(r"^www\\.","",requests.utils.urlparse(str(r.get("link") or r.get("url") or "")).netloc.lower())
    except Exception:
        source_domain=""

    return {
        "title":r.get("title"),"url":r.get("link") or r.get("url"),"snippet":r.get("snippet"),
        "price":price,"km":rkm,"adjusted":round(adjusted),"km_adjustment":km_adjustment,
        "score":round(score),"source":r.get("source",""),"source_domain":source_domain,
        "transmission":extract_transmission(text),"reliability":reliability,
        "provenance":provenance,"fields_verified":[k for k,v in fields.items() if v],
        "complete_fields":complete,"evidence":evidence
    }

def collect_lbc_apify(brand, model, years, category="camping", requested_gamme=""):
    """Collecte LBC centralisée via Apify + proxy résidentiel FR."""
    token=os.environ.get("APIFY_API_TOKEN","").strip()
    if not token:
        return [], []
    actor=os.environ.get("APIFY_LBC_ACTOR","scrapifier/leboncoin-universal-scraper-vehicles").strip()
    category_id={"camping":"4","van":"5","fourgon":"5","poids_lourd":"300"}.get(category,"4")
    endpoint=f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
    collected=[]; errors=[]
    def attrs(ad):
        x=ad.get("attributes") or []
        if isinstance(x,dict): x=[{"key":k,"value":v} for k,v in x.items()]
        return x if isinstance(x,list) else []
    def attr_find(ad,kind):
        for a in attrs(ad):
            if not isinstance(a,dict): continue
            k=norm(a.get("key","")); lab=norm(a.get("key_label",""))
            if kind=="km" and not any(z in k or z in lab for z in ("mileage","kilometr")): continue
            if kind=="year" and not any(z in k or z in lab for z in ("regdate","year","annee")): continue
            if kind=="brand" and not any(z in k or z in lab for z in ("brand","marque")): continue
            if kind=="model" and not any(z in k or z in lab for z in ("model","modele")): continue
            for f in ("value","value_label","values","values_label"):
                v=a.get(f); vals=v if isinstance(v,list) else [v]
                for item in vals:
                    if item not in (None,""): return item
        return None
    if not years:
        return [], []
    y_min, y_max = int(min(years)), int(max(years))
    payload={"category":category_id,
             "locations":[],
             "text":f"{brand} {model}",
             "year_min":y_min,"year_max":y_max,
             "sort":"newest","max_results":100,
             "proxyConfiguration":{"useApifyProxy":True,"apifyProxyGroups":["RESIDENTIAL"],"apifyProxyCountry":"FR"}}
    try:
        resp=requests.post(endpoint,json=payload,
            headers={"Content-Type":"application/json","Authorization":f"Bearer {token}"},timeout=90)
        if not resp.ok:
            errors.append(f"Apify LBC {y_min}-{y_max} HTTP {resp.status_code}: {resp.text[:300]}")
        else:
            data=resp.json()
            if isinstance(data,list):
                collected.extend(data)
                if not data:
                    errors.append(f"Apify LBC {y_min}-{y_max}: HTTP {resp.status_code}, dataset vide (Actor {actor})")
            else:
                keys=", ".join(str(k) for k in data.keys()) if isinstance(data,dict) else type(data).__name__
                errors.append(f"Apify LBC {y_min}-{y_max}: HTTP {resp.status_code}, réponse inattendue ({keys}) (Actor {actor})")
    except Exception as exc:
        errors.append(f"Apify LBC {y_min}-{y_max} {type(exc).__name__}: {exc}")
    unique=[]; seen=set(); compact_model=re.sub(r"\s+","",norm(model))
    reject_counts={"record_type":0,"url":0,"status":0,"brand":0,"model":0,"price":0,"year":0}
    for ad in collected:
        if not isinstance(ad,dict): continue
        if str(ad.get("recordType","")).upper() not in ("","AD"):
            reject_counts["record_type"]+=1; continue
        url=str(ad.get("url") or ad.get("listingUrl") or "").strip().rstrip("/")
        if not url or "leboncoin.fr" not in url.lower() or "/ad/" not in url.lower() or url in seen:
            reject_counts["url"]+=1; continue
        if str(ad.get("status","active")).lower() not in ("active",""):
            reject_counts["status"]+=1; continue
        title=str(ad.get("subject") or ad.get("title") or "").strip()
        body=str(ad.get("body") or ad.get("description") or "").strip()
        identity=f"{title} {body}"; nt=norm(identity)
        typed_brand=attr_find(ad,"brand")
        typed_model=attr_find(ad,"model")
        if typed_brand is not None and norm(brand) and norm(brand) not in norm(typed_brand):
            reject_counts["brand"]+=1; continue
        if typed_model is not None:
            typed_model_norm=norm(typed_model)
            title_model_ok=(norm(model) in norm(title) or compact_model in re.sub(r"\\s+","",norm(title)))
            body_model_ok=(norm(model) in norm(body) or compact_model in re.sub(r"\\s+","",norm(body)))
            # L'Actor peut renseigner l'attribut structuré "model" avec une
            # valeur courte (ex. "328") qui ne reprend pas toute la finition.
            # Le titre/corps reste donc la preuve de la référence complète.
            if norm(model) not in typed_model_norm and compact_model not in re.sub(r"\\s+","",typed_model_norm):
                if not title_model_ok and not body_model_ok:
                    reject_counts["model"]+=1; continue
        elif norm(model) not in norm(title) and compact_model not in re.sub(r"\\s+","",norm(title)):
            reject_counts["model"]+=1; continue

        # Quand une référence DICA précise est sélectionnée, la finition devient
        # obligatoire pour la collecte LBC. On accepte les variantes d'ordre
        # ("Graphite Ultimate" / "Ultimate Graphite"), mais jamais une autre
        # finition ("Premium", "Start", "Break", "Etape"...).
        if requested_gamme:
            finish_ok=finish_alias_match(identity, requested_gamme, model)
            if not finish_ok:
                reject_counts["model"]+=1; continue
        # Selon la version de l'Actor, le prix peut être dans price,
        # priceCents ou _price_eur. On ne mélange jamais avec un autre résultat.
        price=ad.get("price")
        if price is None:
            price=ad.get("_price_eur")
        if price is None and ad.get("priceCents") is not None:
            try: price=float(ad.get("priceCents"))/100.0
            except (TypeError,ValueError): price=None
        if isinstance(price,list) and price: price=price[0]
        if isinstance(price,dict): price=price.get("value",price.get("amount"))
        try: price=round(float(str(price).replace(" ","").replace("\u00a0","").replace("\u202f","").replace(",",".")))
        except (TypeError,ValueError): price=None
        if price is None or not 10000<=price<=150000:
            reject_counts["price"]+=1; continue
        km=None
        # L'Actor peut fournir le kilométrage à plat ou dans attributes.
        flat_km=ad.get("mileageKm",ad.get("vehicle_mileage",ad.get("mileage")))
        kv=flat_km if flat_km is not None else attr_find(ad,"km")
        if kv is not None:
            try: km=clean_num(kv); km=km if 0<=km<=300000 else None
            except Exception: km=None
        if km is None:
            ks=extract_kms(identity); km=ks[0] if ks else None
        yrs=extract_years(identity)
        yv=ad.get("year",ad.get("vehicle_registration_year"))
        if yv is None:
            yv=attr_find(ad,"year")
        if yv is not None:
            try:
                yy=int(str(yv)[:4])
                if 1900<=yy<=2100: yrs.insert(0,yy)
            except Exception: pass
        valid=[yy for yy in yrs if yy in years]
        if not valid:
            reject_counts["year"]+=1; continue
        seen.add(url)
        unique.append({"source_domain":"leboncoin.fr","title":title,"url":url,"link":url,"snippet":body[:1400],
          "price":price,"price_source":"lbc_apify_current","km":km,"year":valid[0],"all_years":yrs[:6],
          "query":f"LBC_APIFY_{valid[0]}","source":"LBC_APIFY","direct_listing":True,
          "detail_scraped":True,"raw_price":price,"raw_km":km,
          "index_date":ad.get("index_date") or ad.get("_scrapedAt") or ad.get("scraped_at")})
    if collected and not unique and not errors:
        errors.append(
            f"Apify LBC: {len(collected)} résultat(s) brut(s) reçus mais 0 annonce(s) retenue(s). "
            f"Rejets: URL={reject_counts['url']}, modèle={reject_counts['model']}, marque={reject_counts['brand']}, "
            f"prix={reject_counts['price']}, année={reject_counts['year']}, statut={reject_counts['status']}, type={reject_counts['record_type']}."
        )
    elif not collected and not errors:
        errors.append("Apify LBC: 0 résultat brut reçu")
    return unique, errors


def collect_lbc_search_results(brand, model, years, category="camping", max_pages=3, requested_gamme=""):
    """Collecte LBC ultra-légère via le Finder.
    Retourne (annonces, erreurs) afin que le diagnostic distingue
    une vraie absence d'annonce d'un blocage HTTP/anti-bot."""
    # Si Apify est configuré, il est la source LBC exclusive.
    # Ne jamais basculer silencieusement vers Finder : un retour Apify vide
    # doit rester visible comme "0 annonce Apify", pas comme un faux HTTP 403 Finder.
    if os.environ.get("APIFY_API_TOKEN","").strip():
        apify_ads, apify_errors = collect_lbc_apify(brand, model, years, category, requested_gamme=requested_gamme)
        return apify_ads, apify_errors

    if not CURL_CFFI_AVAILABLE:
        return [], ["curl_cffi indisponible"]

    def attr_value(ad,key):
        for a in ad.get("attributes",[]) or []:
            if str(a.get("key","")).lower()==key:
                return a.get("value")
        return None

    def search_one(query_text,page_no):
        errors=[]
        try:
            for impersonate in ("chrome_android","chrome"):
                try:
                    session=curl_requests.Session(impersonate=impersonate)
                    session.headers.update({
                        "User-Agent":"LBC;Android;15;Pixel 8;phone;masters-cote-reprise;wifi;100.85.2",
                        "Accept":"application/json,application/hal+json",
                        "Content-Type":"application/json",
                        "Origin":"https://www.leboncoin.fr",
                        "Referer":"https://www.leboncoin.fr/",
                        "X-LBC-CC":"7"
                    })
                    proxy=os.environ.get("LBC_PROXY_URL","").strip()
                    if proxy:
                        session.proxies.update({"http":proxy,"https":proxy})
                    session.get("https://www.leboncoin.fr/",timeout=8)
                    payload={
                "filters":{
                    "category":{"id":"4"},
                    "keywords":{"text":query_text},
                    "enums":{"ad_type":["offer"]}
                },
                "limit":35,
                "limit_alu":0,
                "offset":35*(page_no-1),
                "disable_total":True,
                "extend":True,
                "listing_source":"direct-search" if page_no==1 else "pagination"
            }
                    resp=session.post(
                        "https://api.leboncoin.fr/finder/search",
                        json=payload,
                        timeout=12
                    )
                    if resp.ok:
                        data=resp.json() or {}
                        return data.get("ads",[]) or [], errors
                    errors.append(f"{query_text} p{page_no}: HTTP {resp.status_code}")
                    if resp.status_code not in (403,429,451):
                        break
                except Exception as exc:
                    errors.append(f"{query_text} p{page_no}: {type(exc).__name__}: {exc}")
            return [], errors
        except Exception as exc:
            return [], [f"{query_text} p{page_no}: {type(exc).__name__}: {exc}"]

    collected=[]
    lbc_errors=[]
    jobs=[(f"{brand} {model} {y}",p) for y in years for p in range(1,max_pages+1)]
    with ThreadPoolExecutor(max_workers=min(4,len(jobs))) as pool:
        futures=[pool.submit(search_one,q,p) for q,p in jobs]
        for fut in as_completed(futures):
            try:
                ads,errs=fut.result()
                collected.extend(ads)
                lbc_errors.extend(errs)
            except Exception as exc:
                lbc_errors.append(f"worker: {type(exc).__name__}: {exc}")

    unique=[]
    seen=set()
    compact_model=re.sub(r"\s+","",norm(model))
    for ad in collected:
        url=str(ad.get("url","") or "").strip().rstrip("/")
        if not url or url in seen:
            continue

        title=str(ad.get("subject","") or "")
        body=str(ad.get("body","") or "")
        identity=f"{title} {body}"
        nt=norm(identity)
        if norm(model) not in nt and compact_model not in re.sub(r"\s+","",nt):
            continue

        # Le finder LBC renvoie le prix sous forme de price_cents.
        # Ne pas attendre une clé "price" : elle peut être absente de la réponse brute.
        price=None
        p=ad.get("price_cents")
        if p is None:
            p=ad.get("price")
        if isinstance(p,list) and p:
            p=p[0]
        try:
            if ad.get("price_cents") is not None:
                price=round(float(ad.get("price_cents"))/100)
            else:
                price=clean_num(p)
        except Exception:
            price=None
        if price is None or not 10000<=price<=150000:
            continue

        km=None
        for key in ("mileage","mileage_km","kilometrage"):
            v=attr_value(ad,key)
            if v is not None:
                try:
                    km=clean_num(v)
                    break
                except Exception:
                    pass
        # Certains résultats utilisent value_label plutôt que value.
        if km is None:
            for a in ad.get("attributes",[]) or []:
                if str(a.get("key","")).lower()=="mileage":
                    for field in ("value_label","value","values_label","values"):
                        vv=a.get(field)
                        vals=vv if isinstance(vv,list) else [vv]
                        for item in vals:
                            try:
                                km=clean_num(item)
                                if 0<=km<=300000:
                                    break
                            except Exception:
                                pass
                        if km is not None:
                            break
                if km is not None:
                    break

        yrs=extract_years(identity)
        for key in ("regdate","registration_year","year"):
            v=attr_value(ad,key)
            if v is not None:
                try:
                    y=int(str(v)[:4])
                    if 1900<=y<=2100:
                        yrs.insert(0,y)
                        break
                except Exception:
                    pass
        valid=[y for y in yrs if y in years]
        if not valid:
            continue

        seen.add(url)
        unique.append({
            "source_domain":"leboncoin.fr",
            "title":title,
            "url":url,
            "snippet":body[:1000],
            "price":price,
            "price_source":"lbc_finder_api_price_cents",
            "km":km,
            "year":valid[0],
            "all_years":yrs[:6],
            "query":"LBC_FINDER_API",
            "direct_listing":True,
            "detail_scraped":True,
            "raw_price":price,
            "raw_km":km
        })
    return unique, lbc_errors


@app.post("/api/collecte")
def collecte_diagnostic():
    """Diagnostic pur de collecte : recherche les annonces et retourne les données brutes.
    Aucun scoring, aucune cote, aucun filtre de finition et aucune correction kilométrique.
    """
    data=request.get_json(force=True) or {}
    category=str(data.get("category","camping")).strip().lower()
    brand=str(data.get("brand","")).strip()
    model=str(data.get("model","")).strip()
    try:
        year=int(data.get("year"))
    except (TypeError,ValueError):
        return jsonify({"error":"Année invalide."}),400
    if category not in ("camping","poids_lourd","van","fourgon") or not brand or not model:
        return jsonify({"error":"Marque, modèle, année et catégorie sont nécessaires."}),400
    APIFY_TOKEN=os.environ.get("APIFY_API_TOKEN","").strip()
    if not APIFY_TOKEN:
        return jsonify({"error":"APIFY_API_TOKEN manquante sur le serveur."}),500

    market_years=(year,year+1)
    variants=[]
    compact=re.sub(r"\s+","",model)
    spaced=re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"," ",model).strip()
    for mv in (model,compact,spaced):
        if mv and mv.lower() not in [x.lower() for x in variants]:
            variants.append(mv)

    # Leboncoin est collecté séparément par son Actor véhicule dédié.
    # Pour les autres sites, on utilise l'Actor Google Search officiel d'Apify.
    # Cela évite de dépendre d'un solde Serper séparé.
    queries=[]
    other_sites=(
        ("paruvendu.fr/a/caravaning-occasion/","paruvendu"),
        ("camping-car.com/occasion/","camping-car.com"),
        ("campingcarannonces.com","campingcarannonces"),
        ("annonces-caravaning.com","annonces-caravaning"),
    )
    for y in market_years:
        for mv in variants:
            for domain,_label in other_sites:
                queries.append(f'site:{domain} "{brand} {mv}" {y}')
    queries=list(dict.fromkeys(queries))

    results=[]
    errors=[]
    try:
        apify_url=f"https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?token={APIFY_TOKEN}"
        payload={
            "queries":"\n".join(queries),
            "maxPagesPerQuery":1,
            "resultsPerPage":10,
            "countryCode":"fr",
            "languageCode":"fr",
            "mobileResults":False,
            "includeUnfilteredResults":False,
            "saveHtml":False,
            "saveHtmlToKeyValueStore":False,
            "geminiSearch":{"enableGemini":False},
            "perplexitySearch":{"enablePerplexity":False,"returnImages":False,"returnRelatedQuestions":False},
            "chatGptSearch":{"enableChatGpt":False},
            "copilotSearch":{"enableCopilot":False},
            "maximumLeadsEnrichmentRecords":0
        }
        resp=requests.post(apify_url,json=payload,timeout=120)
        if not resp.ok:
            try:
                detail=resp.json()
            except Exception:
                detail=resp.text
            errors.append({"query":"APIFY_GOOGLE_SEARCH","error":f"HTTP {resp.status_code}: {detail}"})
        else:
            data_items=resp.json() or []
            for bucket in data_items:
                if not isinstance(bucket,dict):
                    continue
                q=str((bucket.get("searchQuery") or {}).get("term") or bucket.get("query") or "")
                bucket_error=bucket.get("error") or bucket.get("#error")
                if bucket_error:
                    errors.append({"query":q,"error":str(bucket_error)})
                for item in (bucket.get("organicResults") or []):
                    if not isinstance(item,dict):
                        continue
                    results.append({
                        "link":item.get("url") or item.get("link") or "",
                        "title":item.get("title") or "",
                        "snippet":item.get("snippet") or item.get("description") or "",
                        "_query":q
                    })
    except Exception as exc:
        errors.append({"query":"APIFY_GOOGLE_SEARCH","error":str(exc)})


    def is_direct_listing_url(url):
        u=str(url or "").strip().lower()
        if not u.startswith(("http://","https://")):
            return False
        if "leboncoin.fr" in u:
            return "/ad/" in u
        if "paruvendu.fr" in u:
            return "/a/caravaning-occasion/" in u
        for marker in ("/recherche", "/search", "/listing", "/resultats", "/results", "/stock", "?q=", "&q="):
            if marker in u:
                return False
        return True

    # Si l'extension Chrome fournit LBC, ses données passent avant la tentative serveur.
    lbc_browser_ads=data.get("lbc_browser_ads") or []
    lbc_browser_used=bool(lbc_browser_ads)
    lbc_direct,lbc_errors=collect_lbc_search_results(brand,model,market_years,category=category,max_pages=3,requested_gamme=str(data.get("dica_gamme","")).strip())
    if lbc_browser_used:
        for ad in lbc_browser_ads:
            if isinstance(ad,dict) and ad.get("url"):
                lbc_direct.append({
                    "source_domain":"leboncoin.fr","title":ad.get("title",""),"url":ad.get("url"),
                    "snippet":ad.get("snippet",""),"price":ad.get("price"),
                    "price_source":"lbc_finder_browser","km":ad.get("km"),"year":ad.get("year"),
                    "query":"LBC_BROWSER_FINDER","direct_listing":True,"detail_scraped":True,
                    "raw_price":ad.get("price"),"raw_km":ad.get("km")
                })
        lbc_errors=[]
    rows=[]
    seen=set()
    for lr in lbc_direct:
        u=str(lr.get("url","") or "").strip()
        if u and u not in seen:
            seen.add(u)
            rows.append(lr)

    for r in results:
        url=str(r.get("link","") or "").strip()
        # Quand le collecteur LBC central est configuré, Serper ne doit jamais
        # réinjecter une ancienne fiche/prix LBC dans le diagnostic.
        if os.environ.get("APIFY_API_TOKEN","").strip() and "leboncoin.fr" in url.lower():
            continue
        if not url or url in seen or not is_direct_listing_url(url):
            continue
        seen.add(url)
        title_raw=str(r.get("title","") or "")
        txt=f"{title_raw} {r.get('snippet','')}"
        title_norm=norm(title_raw)
        model_norm=norm(model)

        # Le résultat Serper peut être une fiche individuelle mais totalement
        # hors sujet. Le modèle doit donc être présent dans LE TITRE de la fiche,
        # jamais seulement dans le snippet.
        model_tokens=re.findall(r"[a-z0-9]+", model_norm)
        title_tokens=re.findall(r"[a-z0-9]+", title_norm)
        if model_norm and model_norm not in title_norm:
            # Tolère uniquement les variantes d'espacement du modèle.
            compact_model=re.sub(r"\s+","",model_norm)
            compact_title=re.sub(r"\s+","",title_norm)
            if compact_model not in compact_title:
                continue

        # Les pages de catalogue déguisées en fiches sont explicitement écartées.
        bad_title_markers=("toutes les annonces","annonces camping-car","petite annonce",
                           "cote challenger","cote camping","page ")
        if any(m in title_norm for m in bad_title_markers):
            continue

        raw_prices=extract_prices(txt)
        raw_kms=extract_kms(txt)
        raw_years=extract_years(txt)

        detail=fetch_detail_price_km(
            {"link":url},
            target_year=year,
            target_model=model,
            target_km=None,
            target_gamme=None
        ) or {}

        domain=""
        try:
            from urllib.parse import urlparse
            domain=re.sub(r"^www\.","",urlparse(url).netloc.lower())
        except Exception:
            pass

        # Leboncoin : ne jamais utiliser le prix du snippet Serper comme
        # vérité finale. Les index peuvent être obsolètes (annonce supprimée
        # ou prix ancien). Pour LBC, le prix doit venir de la fiche actuelle.
        if "leboncoin.fr" in domain:
            price=detail.get("price") if detail.get("price") is not None else None
            price_source="fiche_detail" if price is not None else None
        else:
            if detail.get("price") is not None:
                price=detail.get("price")
                price_source="fiche_detail"
            elif raw_prices:
                price=raw_prices[0]
                price_source="resultat_recherche"
            else:
                price=None
                price_source=None
        km=detail.get("km") if detail.get("km") is not None else (raw_kms[0] if raw_kms else None)
        detail_title=str(detail.get("title","") or "")
        detail_year=detail.get("year")
        years=extract_years(f"{detail_title} {txt}")
        detected_year=detail_year if detail_year in market_years else next((y for y in years if y in market_years),None)

        rows.append({
            "source_domain":domain,
            "title":detail_title or r.get("title",""),
            "url":url,
            "snippet":r.get("snippet",""),
            "price":price,
            "price_source":price_source,
            "km":km,
            "year":detected_year,
            "all_years":years[:6],
            "query":r.get("_query",""),
            "direct_listing":True,
            "detail_scraped":bool(detail),
            "raw_price":raw_prices[0] if raw_prices else None,
            "raw_km":raw_kms[0] if raw_kms else None,
            # Pour LBC, ce prix peut provenir d'une indexation ancienne :
            # il est affiché uniquement comme information, jamais utilisé
            # comme prix de marché confirmé.
            "prix_indexe_lbc": (raw_prices[0] if ("leboncoin.fr" in domain and raw_prices) else None)
        })

    # IMPORTANT : pour Leboncoin, aucune récupération secondaire via
    # Serper n'est autorisée. Les résultats indexés peuvent être obsolètes
    # (annonce supprimée ou ancien prix). Le prix LBC doit provenir uniquement
    # de la fiche actuelle traitée par fetch_detail_price_km().

    def site_key(domain):
        d=domain.lower()
        if "leboncoin" in d:return "Leboncoin"
        if "paruvendu" in d:return "ParuVendu"
        if "camping-car.com" in d:return "Camping-Car.com"
        if "campingcarannonces" in d:return "CampingCarAnnonces"
        if "annonces-caravaning" in d:return "Annonces-Caravaning"
        return domain or "Autre"

    stats={}
    for x in rows:
        s=site_key(x["source_domain"])
        z=stats.setdefault(s,{"annonces":0,"prix":0,"km":0,"annee":0})
        z["annonces"]+=1
        z["prix"]+=int(x["price"] is not None)
        z["km"]+=int(x["km"] is not None)
        z["annee"]+=int(x["year"] is not None)

    rows.sort(key=lambda x:(x["year"] not in market_years, x["source_domain"], -(x["price"] or 0)))
    return jsonify({
        "status":"ok",
        "mode":"diagnostic_collecte",
        "vehicle":{"category":category,"brand":brand,"model":model,"years":list(market_years)},
        "queries":len(queries),
        "raw_results":len(results),
        "unique_annonces":len(rows),
        "direct_fiches":len(rows),
        "errors":errors,
        "errors_summary":serper_error_summary(errors),
        "lbc_errors":lbc_errors[:30],
        "lbc_proxy_configured":bool(os.environ.get("LBC_PROXY_URL","").strip()),
        "lbc_provider":("apify" if os.environ.get("APIFY_API_TOKEN","").strip() else
                        "finder_proxy" if os.environ.get("LBC_PROXY_URL","").strip() else
                        "finder_direct"),
        "lbc_apify_configured":bool(os.environ.get("APIFY_API_TOKEN","").strip()),
        "lbc_browser_used":bool(lbc_browser_used),
        "lbc_browser_annonces":len(lbc_browser_ads),
        "stats":stats,
        "annonces":rows[:100]
    })

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
    # Fenêtre de recherche marché : année du véhicule + l'année suivante.
    # Cette règle est commune à toutes les catégories (camping-car, fourgon,
    # van et poids lourd). Les annonces hors fenêtre ne sont jamais proposées
    # à la sélection manuelle.
    market_years=(year, year+1)
    # IMPORTANT : ne pas mettre « 2022 2023 » dans une même requête :
    # un moteur de recherche peut alors exiger les deux années et éliminer
    # précisément les annonces qui ne mentionnent que leur année réelle.
    # Les requêtes sont donc générées séparément pour 2022 puis 2023.
    market_year_query=str(year)
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
    # Recherche marché volontairement large :
    # marque + modèle + année, sans imposer la finition DICA.
    # Les deux années sont recherchées séparément.
    base_queries=[
        f'"{brand} {model}" {market_year_query} {market_term} occasion',
        f'"{brand} {model}" {market_year_query} prix occasion',
        f'site:leboncoin.fr "{brand} {model}" {market_year_query}',
        f'site:annonces-caravaning.com "{brand} {model}" {market_year_query} {market_term}',
        f'site:camping-car.com/occasion/annonces "{brand} {model}" {market_year_query}',
        f'site:campingcarannonces.com "{brand} {model}" {market_year_query}',
        f'site:paruvendu.fr "{brand} {model}" {market_year_query} camping-car occasion',
        f'"{brand}" "{model}" {market_year_query} {market_term}'
    ]
    queries=base_queries + [q.replace(str(year),str(year+1)) for q in base_queries]
    queries=list(dict.fromkeys(queries))
    # Déduplication des requêtes pour ne pas gaspiller les appels Serper.
    queries=list(dict.fromkeys(queries))
    # Collecte LBC actuelle centralisée avant de construire les comparables.
    lbc_direct,lbc_errors=collect_lbc_search_results(brand,model,market_years,category=category,max_pages=3)
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
    # LBC central : les prix actuels Apify/Finder priment sur Serper.
    if os.environ.get("APIFY_API_TOKEN","").strip():
        results=[r for r in results if "leboncoin.fr" not in str(r.get("link","")).lower()]
    for lr in lbc_direct:
        if isinstance(lr,dict) and lr.get("url"):
            results.append({"link":lr.get("url"),"title":lr.get("title",""),"snippet":lr.get("snippet",""),
              "price":lr.get("price"),"km":lr.get("km"),"year":lr.get("year"),
              "price_source":lr.get("price_source","lbc_apify_current"),"source":lr.get("source","LBC_APIFY"),
              "lbc_current":True,"direct_listing":True,"index_date":lr.get("index_date")})

    browser_ads=data.get("lbc_browser_ads") or []
    for ad in browser_ads:
        if not isinstance(ad,dict): continue
        url=str(ad.get("url","") or "").strip()
        if not url or "leboncoin.fr" not in url: continue
        results.append({
            "link":url,"title":str(ad.get("title","") or ""),
            "snippet":str(ad.get("snippet","") or ""),
            "price":ad.get("price"),"km":ad.get("km"),"year":ad.get("year"),
            "price_source":"lbc_finder_browser","source":"LBC_BROWSER_FINDER"
        })
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
    # Jusqu'à 24 fiches incomplètes peuvent être vérifiées. Les résultats
    # déjà complets (prix + km + année + identité) ne sont pas ouverts :
    # ils constituent la donnée source la plus sûre et évitent les mélanges
    # de fiches catalogue.
    # Scraping des pages détail en parallèle : les requêtes HTTP restent rapides
    # et on évite de bloquer 10 fois 6 secondes l'une après l'autre.
    def scrape_candidate(r):
        source_text=f"{r.get('title','')} {r.get('snippet','')}"
        source_prices=extract_prices(source_text)
        source_kms=extract_kms(source_text)
        source_years=extract_years(source_text)
        source_complete=(
            bool(source_prices)
            and bool(source_kms)
            and year in source_years
            and score_result(r,brand,model,year,km,hp,transmission,dica_gamme,category)>=65
        )

        # Une ligne de résultat complète et cohérente est déjà une annonce exploitable.
        # On ne l'écrase PAS avec une page détail qui pourrait être une fiche
        # différente, un véhicule voisin ou une ancienne version du stock.
        if source_complete:
            return None

        detail=fetch_detail_price_km(r,year,model,km,dica_gamme)
        if not detail or not detail.get("price"):
            return None
        detail_year=detail.get("year")
        detail_title=str(detail.get("title","") or "")
        if detail_year is not None and detail_year!=year:
            return None
        if model and norm(model) not in norm(detail_title):
            compact_model=norm(re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"," ",model))
            if compact_model not in norm(detail_title):
                return None

        # Garde-fou kilométrage : le détail doit confirmer le véhicule annoncé
        # par la source, sinon le résultat source reste prioritaire.
        if source_kms:
            detail_km=detail.get("km")
            if detail_km is None:
                return None
            nearest=min(source_kms,key=lambda x:abs(x-int(detail_km)))
            if abs(nearest-int(detail_km))>250:
                return None

        # Garde-fou prix : si la source donne déjà un prix, un détail très
        # différent ne doit jamais remplacer silencieusement ce prix.
        if source_prices and detail.get("price"):
            source_price=min(source_prices,key=lambda p:abs(p-int(detail.get("price"))))
            detail_price=int(detail["price"])
            if abs(detail_price-source_price)>max(3000,round(source_price*0.08)):
                return None

        return r,detail

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(scrape_candidate,r) for _,r in detail_candidates[:24]]
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

    # Déduplication des annonces syndiquées : plusieurs sites peuvent reprendre
    # exactement le même véhicule avec le même prix et le même kilométrage.
    # Une seule occurrence doit compter dans la cote.
    seen_market=set(); unique_rows=[]
    for row in sorted(rows,key=lambda x:(x["score"],-abs((x["km"] or km)-km)),reverse=True):
        fp=(norm(brand),norm(model),year,int(row.get("price") or 0),int(row.get("km") or 0))
        if fp in seen_market:
            continue
        seen_market.add(fp)
        unique_rows.append(row)
    rows=unique_rows
    context.sort(key=lambda x:x["score"],reverse=True)

    # Annonces proposées à la sélection manuelle.
    manual_candidates=[]
    for r in dedup.values():
        price,rkm=parse_price_km(r,year,km)
        txt=f"{r.get('title','')} {r.get('snippet','')}"
        if not price or is_new(r) or is_unavailable(r): continue
        # Pour une sélection manuelle, l'identité modèle doit être portée par
        # le titre de l'annonce (ou par son titre détaillé), jamais seulement
        # par un snippet de catalogue qui peut citer plusieurs véhicules.
        title_model_ok=model_match_score(str(r.get("title","") or ""),model)>0
        detail_title=str((r.get("_detail") or {}).get("title","") or "")
        detail_model_ok=bool(detail_title and model_match_score(detail_title,model)>0)
        if not title_model_ok and not detail_model_ok: continue
        # Les pages catalogue génériques ne sont pas des annonces sélectionnables
        # sauf si leur propre titre identifie clairement le modèle.
        if is_aggregation(r) and not title_model_ok: continue
        yrs=extract_years(txt)
        if yrs and not any(y in market_years for y in yrs): continue
        # Sélection manuelle : on laisse aussi passer une annonce dont le
        # kilométrage n'est pas présent dans le résultat de recherche.
        # Le vendeur pourra l'ouvrir et décider lui-même si elle correspond.
        s=score_result(r,brand,model,year,km,hp,transmission,dica_gamme,category)
        manual_candidates.append({"url":r.get("link"),"title":r.get("title",""),"snippet":r.get("snippet",""),
          "price":price,"km":rkm,"year":(next((y for y in yrs if y in market_years), None) if yrs else None),"score":round(s),
          "km_pending":rkm is None,
          "source_domain":re.sub(r"^www\.","",requests.utils.urlparse(str(r.get("link",""))).netloc.lower())})
    _seen_mc=set(); _mc=[]
    for x in sorted(manual_candidates,key=lambda x:(x["score"],-abs((x.get("km") or km)-km)),reverse=True):
        if x["url"] and x["url"] not in _seen_mc: _seen_mc.add(x["url"]); _mc.append(x)
    # Si la recherche automatique a déjà produit des comparables, on les propose
    # aussi à la sélection manuelle : aucune annonce trouvée ne doit disparaître
    # simplement parce que son score de preuve est inférieur.
    for row in rows:
        url=str(row.get("url","") or "").strip()
        if not url or any(x.get("url")==url for x in manual_candidates):
            continue
        manual_candidates.append({
            "url":url,
            "title":row.get("title",""),
            "snippet":row.get("title",""),
            "price":row.get("price"),
            "km":row.get("km"),
            "year":year,
            "score":row.get("score",0),
            "source_domain":row.get("source_domain","")
        })
    _seen_mc=set(); _mc=[]
    for x in sorted(manual_candidates,key=lambda x:(x.get("score",0),-abs((x.get("km") or km)-km)),reverse=True):
        if x.get("url") and x["url"] not in _seen_mc:
            _seen_mc.add(x["url"]); _mc.append(x)
    # Les annonces sans kilométrage doivent rester visibles : le vendeur
    # doit pouvoir ouvrir la fiche et valider lui-même la présence du km.
    # On garde les meilleures annonces avec km + jusqu'à 20 annonces sans km.
    _with_km=[x for x in _mc if x.get("km") is not None]
    _without_km=[x for x in _mc if x.get("km") is None]
    manual_candidates=(_with_km[:30]+_without_km[:20])[:50]

    # Le navigateur renvoie aussi les données des annonces cochées. Cela
    # permet de conserver exactement les annonces présentées à l'utilisateur,
    # même si le classement Serper change entre les deux appels.
    selected_ads=data.get("selected_ads") or []
    for ad in selected_ads:
        if not isinstance(ad,dict): continue
        url=str(ad.get("url","") or "").strip()
        if not url: continue
        if url not in dedup:
            dedup[url]={
                "link":url,
                "title":str(ad.get("title","") or ""),
                "snippet":str(ad.get("snippet","") or ""),
                "source":"selection_manuelle"
            }

    selected_urls={str(u).strip() for u in (data.get("selected_urls") or []) if str(u).strip()}
    selected_urls.update(str(ad.get("url","")).strip() for ad in selected_ads if isinstance(ad,dict) and str(ad.get("url","")).strip())
    if selected_urls:
        selected_rows=[]
        for r in dedup.values():
            if str(r.get("link","")).strip() not in selected_urls: continue
            price,rkm=parse_price_km(r,year,km)
            selected_text=f"{r.get('title','')} {r.get('snippet','')}"
            selected_years=extract_years(selected_text)
            if selected_years and not any(y in market_years for y in selected_years):
                continue
            # Si le vendeur a volontairement sélectionné une annonce affichée
            # sans kilométrage, on tente alors une lecture de sa fiche détail.
            # Cela permet d'utiliser le kilométrage réellement affiché sur
            # l'annonce sans l'imposer au vendeur lors du premier tri.
            if price and rkm is None and not r.get("_detail"):
                try:
                    # Pour une annonce choisie manuellement, le vendeur a
                    # validé la fiche. On tente donc une lecture directe du
                    # titre/bloc de détail, sans exiger que la finition DICA
                    # soit répétée mot pour mot dans le HTML.
                    selected_detail=fetch_selected_ad_data(r,year,model)
                    if selected_detail and selected_detail.get("km") is not None:
                        r["_detail"]={
                            "price": selected_detail.get("price") or price,
                            "km": selected_detail.get("km"),
                            "year": selected_detail.get("year"),
                            "title": selected_detail.get("title") or r.get("title",""),
                            "source": selected_detail.get("source","selection_detail"),
                            "url": selected_detail.get("url") or r.get("link")
                        }
                        price,rkm=parse_price_km(r,year,km)
                    else:
                        # Deuxième tentative avec le parseur détaillé existant,
                        # toujours sans imposer le kilométrage cible.
                        detail=fetch_detail_price_km(r,year,model,None,None)
                        if detail and detail.get("km") is not None:
                            r["_detail"]={
                                "price": detail.get("price") or price,
                                "km": detail.get("km"),
                                "year": detail.get("year"),
                                "title": detail.get("title") or r.get("title",""),
                                "source": detail.get("source","detail"),
                                "url": detail.get("url") or r.get("link")
                            }
                            price,rkm=parse_price_km(r,year,km)
                except Exception:
                    pass
            # Une annonce cochée sans km reste sélectionnée mais ne peut entrer
            # dans la moyenne que si son kilométrage a finalement été récupéré.
            if not price or rkm is None or is_new(r) or is_unavailable(r): continue
            if category=="poids_lourd":
                ref_km,over_rate,under_rate=poids_lourd_km_rules(f"{r.get('title','')} {r.get('snippet','')}")
                delta=rkm-km; km_adjustment=round(delta*(over_rate if delta>0 else under_rate)) if ref_km is not None else 0
            else:
                km_adjustment=round((rkm-km)*DICA_OVER_KM_RATE) if rkm>km else (-round((km-rkm)*DICA_UNDER_KM_RATE) if rkm<km else 0)
            evidence=evidence_for_result(r,brand,model,year,km,dica_gamme)
            fields={k:bool(v.get("ok")) for k,v in evidence.items() if isinstance(v,dict) and "ok" in v}
            selected_rows.append({"title":r.get("title"),"url":r.get("link"),"snippet":r.get("snippet"),"price":price,"km":rkm,
              "adjusted":round(price+km_adjustment),"km_adjustment":km_adjustment,
              "score":round(score_result(r,brand,model,year,km,hp,transmission,dica_gamme,category)),
              "source":r.get("source",""),"source_domain":re.sub(r"^www\.","",requests.utils.urlparse(str(r.get("link",""))).netloc.lower()),
              "transmission":extract_transmission(f"{r.get('title','')} {r.get('snippet','')}"),
              "reliability":"C","provenance":"selection_manuelle","manual_selection":True,
              "fields_verified":[k for k,v in fields.items() if v],"complete_fields":sum(1 for v in fields.values() if v),"evidence":evidence})
        seen_selected=set(); selected_unique=[]
        for row in selected_rows:
            if row["url"] not in seen_selected: seen_selected.add(row["url"]); selected_unique.append(row)
        selected_rows=selected_unique; values=[x["adjusted"] for x in selected_rows]
        if values:
            manual_market=round(statistics.median(values)/100)*100
            return jsonify({"status":"ok","manual_selection":True,"provisional":len(values)<3,"category":category,"ptac":ptac,
              "category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),
              "dica_gamme":dica_gamme,"comparables":selected_rows,"context":[],"manual_candidates":manual_candidates,
              "selected_urls":[x["url"] for x in selected_rows],"market":manual_market,"market_low":round(min(values)/100)*100,"market_high":round(max(values)/100)*100,
              "search_time":datetime.now().astimezone().isoformat(timespec="minutes"),"fiscal_cv":cv,"horsepower":hp,"transmission":transmission,
              "transmission_fallback":False,"transmission_counts":{},"transmission_gap":None,"trade":max(0,manual_market-MASTERS_FRAIS),
              "masters_frais":MASTERS_FRAIS,"confidence":"Sélection manuelle","count":len(values),"excluded_count":0,"dica":dica,"dica_ambiguous":len(dica)>1,
              "dica_near":dica_near_matches(brand,model,year,km,hp,options_total,category) if (not dica and category!="poids_lourd") else [],
              "quality":{"comparables":len(values),"km_comparables":len(values),"sans_km":0,"atypiques":0,"fiabilite_A":0,"fiabilite_B":0,
                         "sources":sorted({x.get("source_domain") for x in selected_rows if x.get("source_domain")})},
              "experimental_recalage_factor":DICA_RECALAGE_FACTOR,"experimental_market_gap":(round(manual_market-dica[0]["revente_corrigee"]) if len(dica)==1 else None),
              "experimental_professional_value":(round(dica[0]["reprise_corrigee"]) if len(dica)==1 else None),
              "message":"Cote calculée à partir des annonces sélectionnées manuellement."})

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
            "dica_gamme":dica_gamme,"comparables":primary,"context":context[:5],"manual_candidates":manual_candidates,"selected_urls":[],"dica":dica,
            "dica_ambiguous":len(dica)>1,
            "dica_near":dica_near_matches(brand,model,year,km,hp,options_total,category) if (not dica and category!="poids_lourd") else [],
            "experimental_recalage_factor":DICA_RECALAGE_FACTOR,
            "experimental_market_gap":None,
            "experimental_professional_value":experimental_professional_value,
            "quality":{"comparables":len(values),"km_comparables":len(values),"sans_km":len(context),"atypiques":0,"transmission_fallback":transmission_fallback,"fiabilite_A":sum(1 for x in primary if x.get("reliability")=="A"),"fiabilite_B":sum(1 for x in primary if x.get("reliability")=="B"),"sources":sorted({x.get("source_domain") for x in primary if x.get("source_domain")})},
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
    return jsonify({"status":"ok","category":category,"ptac":ptac,"category_label":("Van aménagé" if category=="van" else "Fourgon aménagé" if category=="fourgon" else "Camping-car poids lourd" if category=="poids_lourd" else "Camping-car"),"comparables":primary,"context":context[:5],"market":market,"market_low":market_low,"market_high":market_high,"search_time":search_time,"fiscal_cv":cv,"horsepower":hp,"transmission":transmission,"transmission_fallback":transmission_fallback,"transmission_counts":transmission_counts,"transmission_gap":transmission_gap,"trade":max(0,market-MASTERS_FRAIS),"masters_frais":MASTERS_FRAIS,"confidence":confidence,"count":len(filtered),"excluded_count":len(excluded_values),"dica":dica,"dica_ambiguous":len(dica)>1,"dica_near":dica_near,"quality":{"comparables":len(filtered),"atypiques":len(excluded_values),"transmission_fallback":transmission_fallback,"km_comparables":sum(1 for x in primary if x.get("km") is not None),"sans_km":len(context),"fiabilite_A":sum(1 for x in primary if x.get("reliability")=="A"),"fiabilite_B":sum(1 for x in primary if x.get("reliability")=="B"),"sources":sorted({x.get("source_domain") for x in primary if x.get("source_domain")})},"experimental_recalage_factor":DICA_RECALAGE_FACTOR,"experimental_market_gap":experimental_market_gap,"experimental_professional_value":experimental_professional_value,"accessories":option_details,"accessories_value":options_total,"dica_reference_km":(dica[0]["reference_km"] if dica else (dica_near[0]["reference_km"] if dica_near else (None if category=="poids_lourd" else dica_ref_km(year, "V" if category=="van" else "F" if category=="fourgon" else None)))),"dica_edition":"Cote Officielle de l’Occasion n°32 — janvier à avril 2026"})
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","8080")))
