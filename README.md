# Masters — Cote Reprise

Application de cote de reprise pour camping-cars.

## Fonctionnement
- Saisir marque, modèle exact, année et kilométrage.
- Le serveur interroge le moteur de recherche via Serper.
- Il recherche des annonces récentes et calcule une cote marché.
- Règle Masters : reprise = cote marché - 8 000 €.

## Déploiement Render
Build command:
pip install -r requirements.txt

Start command:
gunicorn server:app

Variable d'environnement obligatoire:
SERPER_API_KEY = votre clé Serper

Ne partagez jamais votre clé API dans GitHub ou dans une conversation.


## Collecte Leboncoin centralisée

Le moteur utilise désormais un collecteur serveur optionnel pour Leboncoin. Lorsqu'il est configuré, il devient la source LBC prioritaire et récupère les prix au moment de la demande, avec proxy résidentiel français. Les prix issus de Serper ne sont jamais utilisés comme prix LBC de secours.

Variables Render recommandées :
- `APIFY_API_TOKEN` : jeton API Apify.
- `APIFY_LBC_ACTOR` : acteur Apify LBC, par défaut `piotrv1001/leboncoin-listings-scraper`.
- `LBC_PROXY_URL` : ancien fallback direct Finder, uniquement pour diagnostic/maintenance.

Le parcours utilisateur ne nécessite aucune extension Chrome. En l'absence du collecteur central, le moteur signale l'indisponibilité LBC au lieu de transformer un ancien prix indexé en prix actuel.
