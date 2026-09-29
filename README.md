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
