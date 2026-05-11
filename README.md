# 577députés

**Explorateur indépendant de l'activité parlementaire de la 17ᵉ législature de l'Assemblée nationale française.**

🔗 **Site en ligne : [577deputes.fr](https://577deputes.fr)**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)
![SQLite](https://img.shields.io/badge/SQLite-FTS5-003B57)
![Code](https://img.shields.io/badge/code-MIT-green)
![Données](https://img.shields.io/badge/données-Licence%20Ouverte%20Etalab-orange)

---

## En 3 lignes

Un site web local-first qui ingère les **dumps open data officiels** de l'Assemblée nationale et expose, pour la 17ᵉ législature (depuis juillet 2024) :

- **Données brutes** : 577 députés (+ ex-députés indexés), ~17 000 questions parlementaires, ~108 000 amendements, ~6 500 scrutins publics nominatifs (~1 M votes individuels), ~2 700 dossiers législatifs.
- **Analyses dérivées** : détection d'amendements quasi-identiques (MinHash + LSH) avec typologie automatique (obstruction / convergence inter-groupes / amplification interne / réutilisation), matrices de cohésion entre groupes, diagramme ternaire des blocs, détection de réponses ministérielles dupliquées, absentéisme stratégique, amendements jamais défendus, comparateur de députés, constructeur de tops paramétrables.
- **Aucune donnée n'est inventée** : tout provient des sources officielles, et la méthodologie de chaque indicateur est documentée publiquement sur [577deputes.fr/a-propos](https://577deputes.fr/a-propos).

> Le paquet Python interne s'appelle `anqp` (la CLI reste `anqp …`). Le nom public du projet est **577députés**.

## Stack technique

- **Python 3.11+** — FastAPI, Jinja2, SQLite (avec FTS5 `unicode61 remove_diacritics 2` pour la recherche full-text en français)
- **uvicorn** en production, derrière **nginx** (reverse proxy + HTTPS Let's Encrypt + rate-limiting + security headers)
- Aucune base de données externe, aucun service tiers, aucun bundler JS, aucun tracker côté front. Les graphiques sont des SVG/HTML générés côté serveur.
- Source unique de vérité : SQLite. La base est rejouable depuis zéro à tout moment ; les mises à jour sont incrémentales via cache HTTP (ETag / Last-Modified).

```
data.assemblee-nationale.fr  ──httpx (ETag)──▶  ingestion.pipeline  ──INSERT OR REPLACE──▶  SQLite + FTS5  ──▶  FastAPI + Jinja2 (pages SSR + API JSON + exports CSV)
```

## Installation

Le code applicatif vit dans le sous-dossier [`site/`](site/).

```bash
git clone https://github.com/RogericBot/577deputes.git
cd 577deputes/site

python -m venv .venv
.venv/bin/pip install -e .          # ou .venv\Scripts\pip sur Windows

.venv/bin/anqp bootstrap            # télécharge ~330 Mo de dumps, peuple la base (~3-20 min selon la machine)
.venv/bin/anqp serve                # http://127.0.0.1:8000
```

Sous Windows, des scripts d'aide existent :

```powershell
cd 577deputes\site
.\scripts\bootstrap.ps1
.venv\Scripts\anqp.exe serve
```

**Prérequis** : Python ≥ 3.11 avec SQLite ≥ 3.35 (FTS5 inclus de série dans CPython). Aucun Docker, aucun service cloud.

> Le dépôt ne contient **pas** la base de données (~1,8 Go) ni les photos des députés — elles sont téléchargées par `anqp bootstrap`. Il ne contient pas non plus les fichiers d'infrastructure (config serveur, scripts opérationnels, aide-mémoire d'admin), volontairement exclus.

### Commandes CLI principales

```bash
anqp doctor               # sanity check : Python, SQLite, FTS5, accès aux sources
anqp init                 # créer la base + les tables
anqp bootstrap            # tout télécharger + tout ingérer
anqp bootstrap --force    # forcer le re-téléchargement (ignorer le cache ETag)
anqp update               # re-télécharger uniquement ce qui a changé (cache HTTP)
anqp cluster-amendements  # (re)calculer la détection de doublons d'amendements
anqp photos               # (re)télécharger le cache des portraits de députés
anqp stats                # résumé en console
anqp serve --host 0.0.0.0 --port 8000
```

L'instance en ligne se rafraîchit **automatiquement toutes les 24 h** (thread de fond ; désactivable via `ANQP_AUTO_REFRESH=0`). Pour un déploiement classique, on peut aussi planifier `anqp update` en cron.

## Routes principales

| URL | Description |
| --- | --- |
| `/` | Accueil : compteurs, chiffres marquants, dernière activité |
| `/textes`, `/textes/{uid}`, `/textes/{uid}/amendements` | Dossiers législatifs : listing, page-pivot synthétique, amendements hiérarchisés |
| `/amendements`, `/amendements/{uid}` | Recherche FTS sur 108 000+ amendements, fiche détaillée |
| `/scrutins`, `/scrutins/{uid}`, `/scrutins/{uid}/votants` | Scrutins publics nominatifs : listing, décompte par groupe, liste nominative |
| `/questions`, `/questions/{uid}` | Questions parlementaires : listing filtrable/exportable, fiche complète |
| `/deputes`, `/deputes/{uid}` | Trombinoscope filtrable, fiche député avec activité législative |
| `/clusters`, `/stats/clusters` | Doublons d'amendements : liste par texte avec filtre typologique, tableau de bord agrégé |
| `/coalitions` | Matrices de cohésion entre groupes, diagramme ternaire des blocs |
| `/analyses` | Détections : réponses ministérielles dupliquées, absentéisme stratégique, amendements fantômes |
| `/tops`, `/tops/custom` | Classements clés en main + constructeur de tops paramétrables (30+ métriques, 7 entités) |
| `/dissidents` | Mur des dissidents : députés les moins alignés sur leur groupe |
| `/comparer` | Comparateur côte à côte de deux députés |
| `/carte` | Carte de France des circonscriptions, zoomable, avec population et inscrits |
| `/recherche` | Recherche transverse (textes, amendements, questions, scrutins, députés) |
| `/stats`, `/stats/*` | Tableaux de bord (questions, législation, doublons) |
| `/a-propos`, `/mentions-legales` | Méthodologie complète, sources, limites, RGPD |
| `/api/...` | API JSON lecture seule (`/api/docs` pour le Swagger) |
| `/api/health`, `/robots.txt`, `/sitemap.xml`, `/.well-known/security.txt` | Endpoints opérationnels |

Toutes les vues filtrées ont une URL propre, stable et partageable. Beaucoup de graphiques sont exportables en PNG (carré 1080×1080 ou paysage 1200×628) depuis l'interface.

## Structure du dépôt

```
.
├── README.md                 # ← vous êtes ici
├── LICENSE                   # AGPL-3.0 (code)
└── site/                     # l'application
    ├── pyproject.toml
    ├── requirements.txt
    ├── scripts/              # bootstrap / update (PowerShell + bash)
    └── src/anqp/
        ├── cli.py            # CLI Typer
        ├── config.py         # settings + URLs des sources
        ├── db.py             # connect, init_schema, transactions
        ├── schema.sql        # source de vérité du schéma SQLite
        ├── ingestion/        # download (cache HTTP), parsers, pipeline
        └── web/
            ├── app.py        # routes FastAPI
            ├── queries*.py   # requêtes SQL
            ├── cluster_typology.py   # typologie des doublons d'amendements
            ├── templates/    # Jinja2
            └── static/       # CSS + JS minimal + favicon
```

> Certains fichiers internes (journal de décisions, plan, changelog, guide d'installation pour proches, configs serveur) ne sont **pas** publiés dans ce dépôt — ce sont des notes opérationnelles locales.

## Tests

```bash
cd site
.venv/bin/pytest -v          # ou .venv\Scripts\pytest sur Windows
```

Couvre : schéma SQL + FTS5 (sensibilité aux diacritiques), parsing des dumps (avec cas `@xsi:nil` réels), idempotence de l'ingestion (re-ingérer le même ZIP n'ajoute rien), cache HTTP ETag (mocké via `respx`), toutes les routes web + endpoints API + exports CSV/JSON.

## Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `ANQP_DB_PATH` | `data/anqp.db` | Chemin de la base SQLite |
| `ANQP_LEGISLATURE` | `17` | Numéro de législature ingérée |
| `ANQP_AUTO_REFRESH` | `1` | Thread de rafraîchissement 24 h (mettre `0` pour désactiver) |
| `ANQP_SITE_BASE_URL` | `https://577deputes.fr` | URL publique du site (canonical, og:url, sitemap) — à changer si vous hébergez un fork |
| `ANQP_ADMIN_PASSWORD` | *(vide)* | Mot de passe du tableau de bord `/admin/stats` (Basic auth ; si vide, l'accès est refusé) |

## Méthodologie & licences

- **Données sources** : [data.assemblee-nationale.fr](https://data.assemblee-nationale.fr) (Assemblée nationale), INSEE, Ministère de l'Intérieur, [data.gouv.fr](https://www.data.gouv.fr) — toutes sous **Licence Ouverte 2.0 (Etalab)**.
- **Méthodologie détaillée** de chaque indicateur (seuils, algorithmes, limites) : [577deputes.fr/a-propos](https://577deputes.fr/a-propos)
- **Mentions légales & RGPD** : [577deputes.fr/mentions-legales](https://577deputes.fr/mentions-legales)

### Licence du code

Le **code** de ce dépôt est sous licence **MIT** — voir le fichier [`LICENSE`](LICENSE).
Vous pouvez l'utiliser, le modifier et le redistribuer librement, y compris à des fins commerciales, à condition de conserver la mention de copyright.

Les **données** affichées par le site ne relèvent pas de cette licence : elles restent sous la Licence Ouverte 2.0 / Etalab de leurs producteurs respectifs (Assemblée nationale, INSEE, Ministère de l'Intérieur).

## Indépendance

Projet bénévole, sans affiliation politique, partisane ou institutionnelle, sans aucune source de financement publique ou privée. Aucune donnée n'est inventée, devinée ou commentée par l'éditeur. Voir la [page À propos](https://577deputes.fr/a-propos) pour le détail.

## Contact

Pour signaler une erreur de données ou de calcul, ou pour un signalement de sécurité : voir [`/.well-known/security.txt`](https://577deputes.fr/.well-known/security.txt).
