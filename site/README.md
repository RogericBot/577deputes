# 577députés — Activité parlementaire (XVIIᵉ législature)

> Le package Python interne s'appelle toujours `anqp` (la CLI reste `anqp ...`).

Application web **100 % locale** qui expose l'activité parlementaire
de la 17ᵉ législature de l'Assemblée nationale française (en cours
depuis juillet 2024) :

- **Questions parlementaires** (écrites, orales, au Gouvernement) — phase 1
- **Activité législative** : textes (PJL/PPL), amendements, scrutins
  publics nominatifs — **phase 2**
- Fiches des 577 députés actifs croisées avec leur activité.

> **Source** : dumps officiels de [data.assemblee-nationale.fr](https://data.assemblee-nationale.fr/) (open data).
> Aucune donnée n'est inventée : si une source est indisponible, l'app le dit explicitement.

---

## Installation (≤ 5 commandes)

### Windows (PowerShell)

```powershell
git clone <repo> anqp && cd anqp
.\scripts\bootstrap.ps1            # crée .venv, installe les deps, télécharge ~50 MB, peuple la base
.venv\Scripts\anqp.exe serve       # http://127.0.0.1:8000
```

### macOS / Linux

```bash
git clone <repo> anqp && cd anqp
./scripts/bootstrap.sh
.venv/bin/anqp serve
```

### Manuel (4 commandes)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -e .   # ou .venv\Scripts\pip sur Windows
.venv/bin/anqp bootstrap
.venv/bin/anqp serve
```

**Prérequis :** Python ≥ 3.11 avec SQLite ≥ 3.35 (FTS5 inclus de série).
La distribution standard CPython convient. Aucune base de données externe,
aucun Docker, aucun service cloud.

Le bootstrap télécharge ~330 MB de ZIP nocturnes (questions + activité
législative), parse :

- 17 000+ questions parlementaires + réponses ministérielles ;
- 2 700+ dossiers législatifs avec leurs documents et navette complète ;
- 107 000+ amendements (commission + séance) avec dispositif et exposé sommaire ;
- 6 500+ scrutins publics nominatifs et 1 million de votes individuels ;

et les indexe pour le full-text search. Sur une machine moderne :
~3 minutes au total (l'amendements pèse 251 MB et représente l'essentiel).

---

## Démos — 10 cas d'usage

Une fois l'app lancée sur `http://127.0.0.1:8000` :

### Activité législative (phase 2)

1. **Textes les plus amendés de la législature**
   `/textes?sort=amendements_desc` — un coup d'œil sur les batailles d'hémicycle.

2. **Page-pivot d'un texte massif** (~19 500 amendements)
   `/textes/DLR5L17N52428` — synthèse en haut, navigation par article ensuite.
   Aucune liste flat n'est imposée à l'écran, le détail est dépliable.

3. **Top 25 députés "amendementeurs" + taux d'adoption par groupe**
   `/stats/amendements` — la mesure complémentaire du nombre de questions.

4. **Discipline interne par groupe** (sur scrutin public uniquement)
   `/stats/scrutins` — % de votes alignés sur la majorité du groupe.

5. **Détail d'un scrutin** : ventilation par groupe + liste nominative dépliable
   `/scrutins/VTANR5L17V6490` puis bouton "Voir les pour ↗".

### Questions parlementaires (phase 1, toujours actif)

6. **Toutes les questions de Manon Meunier au ministre de l'Agriculture**
   `/questions?ministere=Agriculture%2C+agro-alimentaire+et+souverainet%C3%A9+alimentaire&q=meunier`

7. **Top 30 des députés les plus actifs en questions**
   `/stats/deputes`

8. **Volume de questions sur le thème "santé" depuis 2025**
   `/questions?rubrique=sant%C3%A9&date_min=2025-01-01`

9. **Activité du groupe Rassemblement National**
   `/stats/groupes` puis lien vers le RN.

10. **Évolution mensuelle par type de question**
    `/stats/temporel` — courbes QE / QOSD / QG mois par mois.

Chaque vue filtrée est :

- Partageable (URL propre, indexable, stable).
- Exportable pour les questions (CSV / JSON via les boutons en haut de listing).
- Accessible aussi en pure JSON : `/api/questions`, `/api/textes`,
  `/api/amendements`, `/api/scrutins`.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    data.assemblee-nationale.fr                       │
│        AMO10 (députés), QE, QOSD, QAG  → ZIP nocturnes JSON          │
└─────────────────────────────┬────────────────────────────────────────┘
                              │ httpx (ETag + Last-Modified)
              ┌───────────────▼──────────────┐
              │  ingestion.pipeline           │  src/anqp/ingestion/
              │   ├ download.py (cache HTTP)  │
              │   ├ deputies.py (acteurs/orgs)│
              │   └ questions.py (QE/QOSD/QG) │
              └───────────────┬──────────────┘
                              │ INSERT OR REPLACE (idempotent)
              ┌───────────────▼──────────────┐
              │      SQLite + FTS5            │  data/anqp.db
              │  organes / deputies / mandates│
              │  questions / questions_fts    │
              │  ingestion_runs / source_cache│
              └───────────────┬──────────────┘
                              │
              ┌───────────────▼──────────────┐
              │      FastAPI + Jinja2         │  src/anqp/web/
              │  pages SSR · API JSON · CSV   │
              │  charts SVG inline (zéro JS)  │
              └───────────────────────────────┘
```

Source unique de vérité = SQLite. La base est rejouable depuis zéro à
tout moment (`anqp bootstrap`) ; les mises à jour suivantes sont
incrémentales (`anqp update`).

### Choix techniques (justifications)

| Élément | Choix | Justification |
| --- | --- | --- |
| **Source de données** | Bulk JSON (data.assemblee-nationale.fr) | Source officielle, complète, mise à jour quotidiennement, sans rate limit, schéma stable. *Alternative scraping HTML rejetée* : fragile, 100x plus de requêtes, sujet aux blocages CDN. *Alternative API queryable* : n'existe pas. |
| **DB locale** | SQLite + FTS5 | Aucune installation. FTS5 supporte la recherche full-text avec `unicode61 remove_diacritics 2` (parfait pour le français). 50 MB de données, 200 MB de DB en WAL : tient en RAM côté page cache. *Postgres rejeté* : Docker = friction utilisateur, gain perf nul à cette taille. *DuckDB rejeté* : OLAP-first, indexation FTS moins mature. |
| **Backend** | FastAPI | Async-natif, OpenAPI gratuit, dépendances légères, idéal pour mêler pages SSR et API JSON dans la même app. |
| **Frontend** | Jinja2 + HTMX-friendly + SVG inline | Pas de Node, pas de bundler. Tout est rendu côté serveur. Les graphiques sont des SVG générés dans les templates : zéro JS tiers, totalement local et déterministe. |
| **HTTP cache** | ETag + Last-Modified, persistés en SQLite (`source_cache`) | Le CDN de l'Assemblée renvoie 304 si rien n'a bougé. Évite de retélécharger 50 MB chaque nuit. |
| **Idempotence** | `INSERT OR REPLACE` sur `uid` natural key | Les dumps sont des full snapshots, pas des deltas. Re-ingérer = remplacer ligne par ligne. Simple, sûr, rejouable. |
| **Logs** | JSON Lines (stdlib) | 1 ligne = 1 enregistrement, grep/jq friendly, rotation 5×5 MB sur disque (`logs/anqp.log`). Pas de structlog : la stdlib suffit. |
| **CLI** | Typer + Rich | UX terminal moderne, autocomplétion gratuite, `anqp --help` propre. |
| **Tests** | pytest + respx + TestClient | Couvrent parsing (avec `xsi:nil` cases réelles), idempotence, FTS, toutes les routes, exports. |

Pour l'historique détaillé des décisions (et celles qui ont été rejetées),
voir [`decisions.md`](decisions.md).

---

## Commandes (CLI `anqp`)

```bash
anqp doctor          # Sanity check : Python, SQLite, FTS5, accès aux sources
anqp init            # Créer la base + les tables
anqp bootstrap       # Tout télécharger + tout ingérer
anqp bootstrap -s AMO10 -s QE          # Limiter à 1 ou plusieurs sources
anqp bootstrap --force                 # Forcer le re-téléchargement (ignorer ETag)
anqp bootstrap --skip-download         # Re-parser des ZIPs déjà présents dans data/raw/
anqp update          # Idem mais via cache HTTP : skip si rien n'a bougé
anqp stats           # Résumé en console
anqp serve --host 0.0.0.0 --port 8000  # Démarrer le serveur
anqp export-questions out.csv --type QE --auteur-uid PA722220
```

---

## Routes web

### Pages
| URL | Description |
| --- | --- |
| `/` | Accueil + recherche globale + sections législation et questions |
| `/textes` | Listing dossiers législatifs (statut, initiateur, dépôt) |
| `/textes/{uid}` | **Page-pivot synthétique** : dépôt, navette, amendements (synthèse), scrutins, top auteurs |
| `/textes/{uid}/amendements` | Listing hiérarchisé + filtre article + sort + groupe + examen |
| `/amendements` | Recherche transverse FTS sur 108 000+ amendements |
| `/amendements/{uid}` | Détail d'un amendement (dispositif, exposé sommaire, lien texte/auteur) |
| `/scrutins` | Listing chronologique des scrutins publics nominatifs |
| `/scrutins/{uid}` | Décompte officiel + ventilation par groupe + discipline |
| `/scrutins/{uid}/votants` | Liste nominative dépliable, filtrable par position et groupe |
| `/questions` | Listing paginé, filtrable, triable, exportable |
| `/questions/{uid}` | Détail complet d'une question (auteur, ministère, texte intégral, réponse, source officielle) |
| `/deputes` | Trombinoscope filtrable des 577 députés actifs |
| `/deputes/{uid}` | Fiche député : circonscription, mandats, **activité législative** (amendements, votes, discipline) |
| `/stats` | Index des tableaux de bord (2 sections : Questions + Législation) |
| `/stats/groupes` | Volume + taux de réponse par groupe parlementaire (questions) |
| `/stats/ministeres` | Top ministères interpellés |
| `/stats/themes` | Top rubriques (questions) |
| `/stats/deputes` | Top députés (questions) |
| `/stats/temporel` | Courbes mensuelles par type de question |
| `/stats/textes` | Volume des dossiers par statut, initiateur, dépôts mensuels |
| `/stats/amendements` | Top auteurs, taux d'adoption par groupe |
| `/stats/scrutins` | Discipline interne par groupe, top participants en scrutin nominatif |
| `/clusters` | Amendements quasi-identiques (MinHash + LSH), classés par texte, repliables |
| `/dissidents` | Mur des dissidents : députés les moins alignés sur leur groupe, tri configurable |
| `/tops` | Huit angles de classement (députés actifs, amdts cosignés, scrutins serrés…) — chaque carte est exportable en image |
| `/carte` | Carte de France des circonscriptions, zoomable, avec population et inscrits |
| `/comparer` | Comparateur côte à côte de deux députés |
| `/recherche` | Recherche transverse (textes, amendements, questions, scrutins, députés) |
| `/a-propos` | Méthodologie, sources de données, limites et seuils utilisés |

### API JSON (lecture seule)
| URL | Description |
| --- | --- |
| `GET /api/health` | Statut + compteurs |
| `GET /api/questions?...` | Recherche filtrable, paginée |
| `GET /api/questions/{uid}` | Détail d'une question |
| `GET /api/deputes?...` | Listing députés |
| `GET /api/deputes/{uid}` | Détail député + activité agrégée + mandats |
| `GET /api/textes?...` | Listing dossiers législatifs |
| `GET /api/textes/{uid}` | Détail dossier + actes + documents + résumé amendements |
| `GET /api/amendements?...` | Recherche transverse amendements |
| `GET /api/amendements/{uid}` | Détail amendement |
| `GET /api/scrutins?...` | Listing scrutins |
| `GET /api/scrutins/{uid}` | Détail scrutin + ventilation par groupe |
| `GET /export/questions.csv?...` | Export CSV de la requête courante (≤ 10 000 lignes) |
| `GET /export/questions.json?...` | Idem JSON |
| `GET /docs` | OpenAPI Swagger UI (généré par FastAPI) |

Toutes les pages renvoient un header `Server-Timing` pour mesurer les
latences en local (objectif < 500 ms tenu sur listings et stats).

---

## Tests

```bash
.venv\Scripts\pytest        # Windows
.venv/bin/pytest -v         # POSIX
```

Couvre :
- Schéma SQL + FTS5 (sensibilité aux diacritiques)
- Parsing AMO (députés, organes, mandats) avec cas `@xsi:nil` réels
- Parsing questions (QE/QOSD/QG) + statut + délai
- Idempotence : re-ingérer le même ZIP n'ajoute rien
- Cache HTTP ETag (mocké via `respx`)
- Toutes les pages + endpoints API + exports CSV/JSON
- Header `Server-Timing`

25 tests, ~3 secondes.

---

## Mise à jour quotidienne

Les dumps officiels sont régénérés chaque nuit (~04 h UTC). Pour
synchroniser l'app, planifier `anqp update` ; un cache hit (304) coûte
< 1 seconde par source.

**Windows — Planificateur de tâches :**
```powershell
schtasks /Create /SC DAILY /TN "anqp update" /ST 06:00 `
  /TR "powershell -ExecutionPolicy Bypass -File ${PWD}\scripts\update.ps1"
```

**Linux/macOS — cron :**
```cron
0 6 * * * cd /chemin/vers/anqp && ./scripts/update.sh >> logs/cron.log 2>&1
```

---

## Limitations connues phase 2

1. **Lien amendement → scrutin non garanti.** Les amendements stockent
   `seance_discussion_ref` mais ne pointent pas le scrutin individuel
   éventuel. La donnée source ne le permet pas de manière fiable. Affiché
   dans l'UI : « Discuté en séance du JJ/MM ; un scrutin public peut ne
   pas avoir été tenu sur cet amendement. »

2. **Cosignataires d'amendement comptés mais pas listés.** Pour des
   raisons de volume (~108 k amendements × 1-100 cosignataires), la
   table actuelle stocke uniquement `cosignataires_count`. Phase 3 :
   table `amendement_cosignataires` séparée.

3. **Lien dossier ↔ scrutin partiel.** ~80 % des scrutins ont
   `dossier_uid = NULL` car la source ne les rattache pas
   systématiquement. La page `/textes/{uid}` n'affiche que les
   scrutins explicitement liés ; les autres restent visibles via
   `/scrutins`.

4. **Discipline = scrutins publics nominatifs uniquement.** Toutes les
   métriques de "vote" se basent sur les ~6 500 scrutins publics. Les
   votes à main levée (largement majoritaires en hémicycle) ne laissent
   aucune trace exploitable. **Affiché en clair dans l'UI partout.**

5. **Amendements : pas d'historique.** Si un amendement change de sort
   entre deux ingestions, on conserve le dernier état seulement.

6. **Encodage hétérogène source.** Les fichiers d'amendements alternent
   UTF-8 et latin-1. Le parser tente UTF-8 puis bascule en latin-1 ;
   un fichier ni l'un ni l'autre serait un edge case non testé.

## Suivi des limitations phase 1 (révision 0.3.0)

Les 9 limitations originellement listées ont été traitées. État au
2026-05-09 :

| # | Limitation initiale | Statut | Correctif appliqué |
| --- | --- | --- | --- |
| 1 | Députés actifs uniquement (`/deputes/{uid}` 404 sur ex-députés) | ✅ Résolu | Source `AMO50` ajoutée et ingérée AVANT `AMO10`. 625 députés indexés (577 actifs + 48 ex), tous accessibles. |
| 2 | Pas de différentiel d'ingestion | ✅ Résolu | `ingestion_runs.notes` reporte désormais `status_changes=N answers_published=M` (lisible via `anqp doctor` et la table `ingestion_runs`). |
| 3 | Pas de versioning historique | ✅ Résolu (light) | Table `questions_history` ajoutée. Capture une ligne par transition de statut ou de date de réponse. Pas de doublement de la base, juste les transitions intéressantes. |
| 4 | Charts non interactifs | ✅ Résolu | Tooltips natifs (`<title>` SVG) au survol des barres et points. Zéro dépendance JS conservée. |
| 5 | FTS naïve | ✅ Résolu | Le sanitiseur supporte maintenant : phrases entre guillemets (`"texte exact"`), opérateurs explicites `AND`/`OR`/`NOT`/`NEAR`, recherche par champ (`titre:retraite`, `auteur_nom_complet:dupont`), négation `-mot`. Le mode AND-préfixe reste le défaut. |
| 6 | QAG : pas de séance complète | ⚠️ Partiel | Hors scope (besoin d'une nouvelle source `seances`). La fiche QAG reste isolée ; à ouvrir en phase 4 si la donnée séances est ingérée. |
| 7 | Concurrence d'écriture (Postgres) | ⚠️ Documenté | SQLite en WAL est satisfaisant pour le local (≤ ~10 lecteurs). La couche `db.py` reste isolée derrière `connect()` ; la migration Postgres consiste à substituer cette fonction et à porter `schema.sql` (les types et FTS5 nécessitent un travail dédié). Non implémenté. |
| 8 | Photos en ligne | ✅ Résolu | Commande `anqp photos` + auto-déclenchement après `bootstrap`. 625 portraits cachés dans `src/anqp/web/static/photos/` (~6 MB). Route `/photo/{uid}` sert le cache local et tombe sur un placeholder SVG si le fichier manque. |
| 9 | Pas de multi-legislature | ✅ Résolu | Tous les chemins / requêtes / parseurs lisent désormais `settings.legislature` (par défaut 17). Override par variable d'environnement `ANQP_LEGISLATURE=16`. URLs des sources reconstruites dynamiquement. À noter : un seul jeu de tables est utilisé ; pour comparer 16/17 dans la même app il faudrait soit relancer un bootstrap dans une autre `ANQP_DB_PATH`, soit étendre les requêtes pour grouper. |

### Nouvelles limitations introduites en 0.3.0 (à itérer)

1. **Photos sur disque, pas dans la DB.** `static/photos/` n'est pas
   versionné par git (gitignore). Sur un autre poste, il faut relancer
   `anqp photos` une fois.
2. **Multi-legislature partielle.** Le code accepte `ANQP_LEGISLATURE`
   mais la base n'est pas multi-tenant : un changement de legislature
   nécessite une base séparée (via `ANQP_DB_PATH`). Fusionner deux
   legislatures dans la même DB fonctionne à l'ingestion mais pas
   encore via un sélecteur UI.
3. **Historique SCD-2 light.** `questions_history` ne capture que les
   transitions de `statut` et `date_reponse`. Si un texte de réponse
   est modifié SANS changer ces deux champs (correctif rédactionnel),
   l'ancien texte est perdu. Étendre la capture demande de stocker un
   hash du texte intégral, pas fait dans cette itération.

---

## Structure du repo

```
.
├── README.md                    # ← vous êtes ici
├── decisions.md                 # ADR-style, 1 décision = 1 entrée
├── CHANGELOG.md
├── pyproject.toml
├── requirements.txt
├── scripts/
│   ├── bootstrap.ps1 / .sh
│   └── update.ps1 / .sh
├── src/anqp/
│   ├── cli.py                   # Typer CLI
│   ├── config.py                # Settings + URLs source
│   ├── db.py                    # connect, init_schema, transactions
│   ├── schema.sql               # Source de vérité du schéma
│   ├── logging_setup.py         # JSON lines + rotation fichiers
│   ├── ingestion/
│   │   ├── download.py          # ETag + Last-Modified cache
│   │   ├── parse_helpers.py
│   │   ├── deputies.py          # AMO → organes/deputies/mandates
│   │   ├── questions.py         # QE/QOSD/QG → questions + FTS
│   │   └── pipeline.py          # Orchestrateur + ingestion_runs log
│   └── web/
│       ├── app.py               # Routes FastAPI
│       ├── queries.py           # Toutes les requêtes SQL
│       ├── templates/           # Jinja2
│       └── static/              # CSS + favicon
└── tests/
    ├── conftest.py
    ├── fixtures/                # Mini-zips réels (~70 KB)
    ├── test_db.py
    ├── test_ingestion.py
    └── test_web.py
```

---

## Licence

Le code de cette application est sous MIT. Les données affichées
proviennent de l'open data de l'Assemblée nationale : voir leurs
[CGU & licence](https://data.assemblee-nationale.fr/).
