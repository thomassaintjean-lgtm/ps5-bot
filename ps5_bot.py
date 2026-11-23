"""
Bot d'alerte PS5 / PS5 Pro -> Discord  (v2)

1) SONY : lit le catalogue https://www.playstation.com/fr-fr/consoles/ps5/shop-all/,
   garde les PS5 avec lecteur et les PS5 Pro (packs compris) vendues au prix
   officiel ou moins, puis ouvre chaque fiche PlayStation Direct pour voir si
   le bouton « Ajouter au panier » est actif.
2) ENSEIGNES : vérifie une liste de fiches produit (Boulanger, Fnac…) et
   n'alerte que si le produit est en stock ET au prix officiel ou moins
   (les vendeurs marketplace à prix gonflé sont donc ignorés).

Variables d'environnement :
  DISCORD_WEBHOOK_URL  (obligatoire)
  DISCORD_USER_ID      (optionnel) pour être mentionné
  TEST_NOTIF=1         (optionnel) envoie un message de test
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ------------------------------------------------------------------ RÉGLAGES
PRIX_MAX = {             # prix officiels Sony France
    "standard": 649.99,  # PS5 avec lecteur 1 To
    "pro": 899.99,       # PS5 Pro 2 To
}
INCLURE_NUMERIQUE = False  # True pour surveiller aussi la PS5 Édition Numérique (599,99 €)
PRIX_MAX["numerique"] = 599.99

CATALOGUE_SONY = "https://www.playstation.com/fr-fr/consoles/ps5/shop-all/"

# Fiches d'autres enseignes : (nom affiché, modèle, URL)
# Ajoute ici les liens que tu veux surveiller (Fnac, Leclerc, Micromania, Carrefour…)
ENSEIGNES = [
    ("Boulanger – PS5 Pro 2 To", "pro", "https://www.boulanger.com/ref/1230556"),
    ("Boulanger – PS5 Standard 1 To", "standard", "https://www.boulanger.com/ref/1231149"),
]
# ---------------------------------------------------------------------------

STATE_FILE = Path("state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ------------------------------------------------------------------ OUTILS
def envoyer_discord(titre, message, url=None, couleur=0x00B050):
    if not WEBHOOK:
        print("⚠️  DISCORD_WEBHOOK_URL manquant, pas d'envoi.")
        return
    mention = f"<@{USER_ID}> " if USER_ID else "@everyone "
    payload = {
        "content": mention + titre,
        "allowed_mentions": {"parse": ["everyone"], "users": [USER_ID] if USER_ID else []},
        "embeds": [{"title": titre, "description": message, "url": url, "color": couleur,
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    r = requests.post(WEBHOOK, json=payload, timeout=15)
    print(f"   Discord -> {r.status_code}")


def lire_prix(texte):
    """'649,99 €' / '€649,99' / '899€95' -> 649.99"""
    if not texte:
        return None
    texte = re.sub(r"(?<![\d.,])(\d)[ \u202f\xa0.](?=\d{3}[,.]\d{2})", r"\1", str(texte))  # « 1 299,95 » -> « 1299,95 »
    for motif in (r"€\s*(\d{1,5})[,.](\d{2})",      # €649,99
                  r"(\d{1,5})[,.](\d{2})\s*€",      # 649,99 €
                  r"(\d{2,5})\s*€\s*(\d{2})\b",     # 899€95
                  r"^\s*(\d{1,5})[,.](\d{2})\s*$"):  # 649.99 (valeur brute)
        m = re.search(motif, texte)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
    m = re.fullmatch(r"\s*(\d{2,5})\s*", texte)
    return float(m.group(1)) if m else None


def modele(nom):
    n = nom.lower()
    if any(x in n for x in ("reconditionn", "manette", "casque", "lecteur de disque seul")):
        return None
    if "numérique" in n or "numerique" in n or "digital" in n:
        return "numerique" if INCLURE_NUMERIQUE else None
    if "pro" in n:
        return "pro"
    if "playstation®5" in n or "playstation 5" in n or "ps5" in n:
        return "standard"
    return None


def page_bloquee(page):
    t = page.content().lower()
    titre = (page.title() or "").lower()
    signes = ("access denied", "accès refusé", "captcha-delivery", "just a moment",
              "pardon our interruption", "attention required", "vous avez été bloqué")
    return any(s in t[:20000] for s in signes) or any(s in titre for s in signes)


# ------------------------------------------------------------------ SONY
def lister_catalogue_sony(page):
    page.goto(CATALOGUE_SONY, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    for _ in range(5):  # clique « Afficher plus » tant qu'il existe
        bouton = page.get_by_role("button", name=re.compile(r"afficher plus", re.I))
        if bouton.count() and bouton.first.is_visible():
            bouton.first.click()
            page.wait_for_timeout(2500)
        else:
            break

    # Pour chaque lien produit, remonte jusqu'à la « carte » qui contient le prix
    cartes = page.eval_on_selector_all(
        "a[href*='pdp-redirect']",
        """els => els.map(a => {
            let n = a, t = '';
            for (let i = 0; i < 6 && n; i++) { t = n.innerText || ''; if (t.includes('€')) break; n = n.parentElement; }
            return {href: a.href, texte: t};
        })""")

    produits = {}
    for c in cartes:
        lignes = [l.strip() for l in c["texte"].split("\n") if l.strip()]
        nom = next((l for l in lignes if "PlayStation" in l or "PS5" in l), None)
        prix = lire_prix(c["texte"])
        if nom and c["href"] not in produits:
            produits[c["href"]] = (nom, prix)
    return produits


def dispo_playstation_direct(page, url):
    """'dispo', 'indispo', 'file_attente' ou 'bloque'."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
    except PWTimeout:
        return "bloque"
    if "queue" in page.url.lower():
        return "file_attente"
    if page_bloquee(page):
        return "bloque"
    if "direct.playstation.com" not in page.url:
        return "indispo"  # Sony redirige vers une page vitrine : pas vendu en direct

    boutons = page.get_by_role("button", name=re.compile(r"ajouter au panier", re.I))
    for i in range(boutons.count()):
        b = boutons.nth(i)
        try:
            if b.is_visible() and b.is_enabled():
                rupture = page.get_by_text(re.compile(r"rupture de stock", re.I))
                if any(rupture.nth(j).is_visible() for j in range(rupture.count())):
                    return "indispo"
                return "dispo"
        except Exception:
            continue
    return "indispo"


# ------------------------------------------------------------------ ENSEIGNES
def offre_enseigne(page, url):
    """Renvoie (statut, prix, vendeur). Lit les données produit (JSON-LD) de la page."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
    except PWTimeout:
        return "bloque", None, None
    if page_bloquee(page):
        return "bloque", None, None

    blocs = page.eval_on_selector_all("script[type='application/ld+json']", "els => els.map(e => e.textContent)")
    offres = []

    def collecter(obj):
        if isinstance(obj, list):
            for o in obj:
                collecter(o)
        elif isinstance(obj, dict):
            if "offers" in obj:
                o = obj["offers"]
                offres.extend(o if isinstance(o, list) else [o])
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    collecter(v)

    for b in blocs:
        try:
            collecter(json.loads(b))
        except Exception:
            pass

    for o in offres:
        if not isinstance(o, dict):
            continue
        prix = o.get("price") or o.get("lowPrice")
        try:
            prix = float(str(prix).replace(",", "."))
        except Exception:
            prix = None
        stock = str(o.get("availability", "")).lower()
        vendeur = (o.get("seller") or {}).get("name") if isinstance(o.get("seller"), dict) else None
        if prix:
            return ("dispo" if "instock" in stock else "indispo"), prix, vendeur

    # --- Plan B : pas de données structurées, on lit la page comme un humain
    prix = None
    for sel, attr in (("meta[itemprop='price']", "content"),
                      ("meta[property='product:price:amount']", "content"),
                      ("meta[property='og:price:amount']", "content"),
                      ("[itemprop='price']", "content")):
        try:
            el = page.query_selector(sel)
            if el:
                prix = lire_prix(el.get_attribute(attr) or el.inner_text())
                if prix:
                    break
        except Exception:
            pass
    if not prix:
        try:
            txt = page.evaluate("""() => {
                const els = [...document.querySelectorAll('[class*="price" i], [data-testid*="price" i]')]
                  .filter(e => e.offsetParent !== null && /\\d/.test(e.innerText || ''));
                return els.length ? els[0].innerText : '';
            }""")
            prix = lire_prix(txt)
        except Exception:
            pass

    bouton_actif = False
    boutons = page.get_by_role("button", name=re.compile(r"ajouter au panier", re.I))
    for i in range(boutons.count()):
        try:
            if boutons.nth(i).is_visible() and boutons.nth(i).is_enabled():
                bouton_actif = True
                break
        except Exception:
            pass

    print(f"   [diag] titre page : {page.title()[:80]!r} | prix lu : {prix} | bouton panier actif : {bouton_actif}")
    if prix is None and not bouton_actif:
        return "inconnu", None, None
    return ("dispo" if bouton_actif else "indispo"), prix, None


# ------------------------------------------------------------------ MAIN
def main():
    if os.environ.get("TEST_NOTIF") == "1":
        envoyer_discord("✅ Test du bot PS5", "Si tu vois ce message, les alertes fonctionnent.", couleur=0x3366FF)
        return

    try:
        etat = json.loads(STATE_FILE.read_text())
    except Exception:
        etat = {}

    with sync_playwright() as p:
        nav = p.chromium.launch(headless=True)
        ctx = nav.new_context(user_agent=UA, locale="fr-FR", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()

        # ---- 1. Sony
        print("== Catalogue Sony ==")
        try:
            catalogue = lister_catalogue_sony(page)
        except Exception as e:
            print("   impossible de lire le catalogue :", e)
            catalogue = {}

        for url, (nom, prix) in catalogue.items():
            m = modele(nom)
            if not m:
                continue
            if prix is None or prix > PRIX_MAX[m] + 0.01:
                print(f"   ignoré (prix {prix}) : {nom}")
                continue
            statut = dispo_playstation_direct(page, url)
            cle = "sony:" + nom
            print(f"   {nom} – {prix} € : {statut}")
            if statut == "dispo" and etat.get(cle) != "dispo":
                envoyer_discord(f"🎮 {nom} DISPO chez Sony – {prix:.2f} €".replace(".", ","),
                                "Le bouton « Ajouter au panier » est actif sur PlayStation Direct. Fonce !", url)
            elif statut == "file_attente" and etat.get(cle) != "file_attente":
                envoyer_discord(f"⏳ File d'attente Sony ouverte – {nom}",
                                "Sony a activé sa file d'attente : un réassort est probablement en cours.",
                                url, couleur=0xFFA500)
            if statut != "bloque":
                etat[cle] = statut

        # ---- 2. Autres enseignes
        print("== Enseignes ==")
        for nom, m, url in ENSEIGNES:
            statut, prix, vendeur = offre_enseigne(page, url)
            plafond = PRIX_MAX[m]
            bon_prix = prix is not None and prix <= plafond + 0.01
            effectif = "dispo" if (statut == "dispo" and bon_prix) else statut
            cle = "enseigne:" + url
            print(f"   {nom} : {statut}, {prix} € (plafond {plafond}), vendeur {vendeur}")
            if effectif == "dispo" and etat.get(cle) != "dispo":
                txt = f"En stock à **{prix:.2f} €**".replace(".", ",")
                if vendeur:
                    txt += f" – vendu par {vendeur}"
                envoyer_discord(f"🎮 {nom} DISPO au bon prix !", txt + ". Fonce !", url)
            if statut not in ("bloque", "inconnu"):
                etat[cle] = effectif

        nav.close()

    STATE_FILE.write_text(json.dumps(etat, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
