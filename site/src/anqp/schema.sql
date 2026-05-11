-- =====================================================================
-- anqp schema — SQLite (FTS5 required).
-- All bulk dumps from data.assemblee-nationale.fr are full snapshots so
-- ingestion uses INSERT OR REPLACE on the natural keys (uid).
-- =====================================================================

-- Foreign keys are declared below for documentation only; they are NOT
-- enforced at runtime (see db.connect). Questions can legitimately
-- reference ex-deputies absent from the current AMO10 snapshot.
PRAGMA foreign_keys = OFF;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------------
-- ORGANES — groupes parlementaires, commissions, partis, etc.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organes (
    uid                TEXT PRIMARY KEY,        -- e.g. PO845401
    code_type          TEXT,                    -- GP, COMPER, PARPOL, GE, GA, ASSEMBLEE…
    libelle            TEXT,
    libelle_abrege     TEXT,
    libelle_edition    TEXT,
    legislature        INTEGER,
    date_debut         TEXT,
    date_fin           TEXT,
    couleur            TEXT,                    -- group colour (hex)
    organe_parent_uid  TEXT,
    raw_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_organes_type ON organes(code_type);
CREATE INDEX IF NOT EXISTS idx_organes_legislature ON organes(legislature);

-- ---------------------------------------------------------------------
-- DEPUTIES — flattened "current state" snapshot for legislature 17.
-- Detailed mandate history lives in `mandates`.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deputies (
    uid                TEXT PRIMARY KEY,        -- e.g. PA841605
    civilite           TEXT,
    prenom             TEXT,
    nom                TEXT,
    nom_complet        TEXT,                    -- "Antoine Golliot"
    date_naissance     TEXT,
    lieu_naissance     TEXT,
    profession         TEXT,
    cat_socpro         TEXT,
    -- Election info (current legislature 17 mandate, if any):
    legislature        INTEGER,
    region             TEXT,
    departement        TEXT,
    departement_code   TEXT,
    circonscription    INTEGER,
    place_hemicycle    TEXT,
    date_debut_mandat  TEXT,
    date_fin_mandat    TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1,
    -- Group / party (current):
    groupe_uid         TEXT,
    groupe_libelle     TEXT,
    groupe_abrege      TEXT,
    groupe_couleur     TEXT,
    parti_uid          TEXT,
    parti_libelle      TEXT,
    -- Contact:
    email_an           TEXT,
    adresse_postale    TEXT,
    uri_hatvp          TEXT,
    photo_url          TEXT,                    -- derived
    raw_json           TEXT,
    FOREIGN KEY (groupe_uid) REFERENCES organes(uid),
    FOREIGN KEY (parti_uid)  REFERENCES organes(uid)
);
CREATE INDEX IF NOT EXISTS idx_deputies_groupe ON deputies(groupe_uid);
CREATE INDEX IF NOT EXISTS idx_deputies_dept ON deputies(departement_code);
CREATE INDEX IF NOT EXISTS idx_deputies_active ON deputies(is_active);
CREATE INDEX IF NOT EXISTS idx_deputies_nom ON deputies(nom);

-- ---------------------------------------------------------------------
-- MANDATES — every mandat (parliamentary, group, commission, party…).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mandates (
    uid              TEXT PRIMARY KEY,          -- e.g. PM843167
    acteur_uid       TEXT NOT NULL,
    organe_uid       TEXT,
    type_organe      TEXT,                      -- ASSEMBLEE, GP, COMPER, GE, GA, PARPOL…
    legislature      INTEGER,
    date_debut       TEXT,
    date_fin         TEXT,
    qualite          TEXT,
    nomin_principale INTEGER,
    raw_json         TEXT,
    FOREIGN KEY (acteur_uid) REFERENCES deputies(uid),
    FOREIGN KEY (organe_uid) REFERENCES organes(uid)
);
CREATE INDEX IF NOT EXISTS idx_mandates_acteur ON mandates(acteur_uid);
CREATE INDEX IF NOT EXISTS idx_mandates_organe ON mandates(organe_uid);
CREATE INDEX IF NOT EXISTS idx_mandates_type ON mandates(type_organe);

-- ---------------------------------------------------------------------
-- QUESTIONS — every Q + answer (QE, QOSD, QG).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS questions (
    uid                       TEXT PRIMARY KEY,  -- e.g. QANR5L17QE12345
    type                      TEXT NOT NULL,     -- QE | QOSD | QG
    legislature               INTEGER,
    numero                    INTEGER,
    auteur_uid                TEXT,
    auteur_nom_complet        TEXT,              -- denormalised for fast list display
    auteur_groupe_uid         TEXT,
    auteur_groupe_abrege      TEXT,
    ministere_interroge       TEXT,
    ministere_interroge_court TEXT,
    ministere_attributaire    TEXT,
    ministere_attrib_court    TEXT,
    rubrique                  TEXT,
    tete_analyse              TEXT,
    analyse                   TEXT,              -- short title
    titre                     TEXT,              -- == analyse for quick display
    texte_question            TEXT,
    texte_reponse             TEXT,
    date_question             TEXT,
    date_reponse              TEXT,
    date_publication_question TEXT,
    statut                    TEXT,              -- 'avec_reponse' | 'sans_reponse' | 'cloturee' | 'autre'
    delai_reponse_jours       INTEGER,           -- date_reponse - date_question, in days
    source_url                TEXT,
    raw_json                  TEXT,
    ingested_at               TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (auteur_uid) REFERENCES deputies(uid),
    FOREIGN KEY (auteur_groupe_uid) REFERENCES organes(uid)
);
CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(type);
CREATE INDEX IF NOT EXISTS idx_questions_auteur ON questions(auteur_uid);
CREATE INDEX IF NOT EXISTS idx_questions_groupe ON questions(auteur_groupe_uid);
CREATE INDEX IF NOT EXISTS idx_questions_date_q ON questions(date_question);
CREATE INDEX IF NOT EXISTS idx_questions_date_r ON questions(date_reponse);
CREATE INDEX IF NOT EXISTS idx_questions_min_int ON questions(ministere_interroge_court);
CREATE INDEX IF NOT EXISTS idx_questions_rubrique ON questions(rubrique);
CREATE INDEX IF NOT EXISTS idx_questions_statut ON questions(statut);
CREATE INDEX IF NOT EXISTS idx_questions_numero ON questions(numero);

-- ---------------------------------------------------------------------
-- FTS5 virtual table over the question texts. We use plain (non-content)
-- because it's simpler to keep in sync via triggers.
-- Tokenizer: unicode61 with diacritics removal — French-friendly.
-- ---------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(
    uid UNINDEXED,
    titre,
    texte_question,
    texte_reponse,
    rubrique,
    analyse,
    ministere_interroge,
    auteur_nom_complet,
    tokenize = "unicode61 remove_diacritics 2"
);

-- ---------------------------------------------------------------------
-- INGESTION RUNS — bookkeeping for each ingestion attempt (used by the
-- auto-refresh loop to know when the last successful run completed).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT,                  -- running | success | partial | failure | skipped
    rows_seen       INTEGER DEFAULT 0,
    rows_inserted   INTEGER DEFAULT 0,
    rows_updated    INTEGER DEFAULT 0,
    rows_skipped    INTEGER DEFAULT 0,
    rows_errors     INTEGER DEFAULT 0,
    bytes_downloaded INTEGER DEFAULT 0,
    source_etag     TEXT,
    source_last_modified TEXT,
    duration_seconds REAL,
    error_message   TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_source_started
    ON ingestion_runs(source, started_at DESC);

-- ---------------------------------------------------------------------
-- HTTP CACHE — remember the last ETag/Last-Modified per source so
-- re-runs can short-circuit when nothing changed (HTTP 304 fallback
-- on metadata equality even if the server doesn't honour conditional
-- requests).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_cache (
    source        TEXT PRIMARY KEY,
    url           TEXT,
    etag          TEXT,
    last_modified TEXT,
    content_length INTEGER,
    fetched_at    TEXT,
    file_path     TEXT
);

-- ---------------------------------------------------------------------
-- METADATA / KV — schema version etc.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '2');
UPDATE meta SET value = '2' WHERE key = 'schema_version' AND value = '1';

-- =====================================================================
-- PHASE 2 — Activité législative : textes, amendements, scrutins.
-- All additions are non-destructive ; Phase 1 schema is untouched.
-- =====================================================================

-- ---------------------------------------------------------------------
-- DOSSIERS — un dossier législatif (PJL, PPL, …) avec sa navette.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dossiers (
    uid                       TEXT PRIMARY KEY,            -- DLR5L17N#####
    legislature               INTEGER,
    titre                     TEXT,
    titre_chemin              TEXT,
    procedure_code            TEXT,
    procedure_libelle         TEXT,
    initiateur_type           TEXT,                        -- 'gouvernement' | 'parlementaire' | 'senat' | 'autre'
    initiateur                TEXT,                        -- nom lisible (ministre ou député(s))
    initiateur_acteur_uids    TEXT,                        -- JSON list des PA####
    commission_fond_uid       TEXT,                        -- FK organes
    commission_fond_libelle   TEXT,
    document_initial_uid      TEXT,                        -- FK documents
    rapporteur_uids           TEXT,                        -- JSON list
    date_depot                TEXT,
    date_dernier_acte         TEXT,
    statut                    TEXT,                        -- 'en_cours' | 'adopte' | 'rejete' | 'retire' | 'caduc'
    nb_amendements_total      INTEGER DEFAULT 0,           -- cache, recalculé à chaque ingestion
    nb_amendements_adoptes    INTEGER DEFAULT 0,
    nb_scrutins               INTEGER DEFAULT 0,
    source_url                TEXT,
    raw_json                  TEXT,
    ingested_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dossiers_statut ON dossiers(statut);
CREATE INDEX IF NOT EXISTS idx_dossiers_legislature ON dossiers(legislature);
CREATE INDEX IF NOT EXISTS idx_dossiers_date_depot ON dossiers(date_depot);
CREATE INDEX IF NOT EXISTS idx_dossiers_initiateur_type ON dossiers(initiateur_type);

-- ---------------------------------------------------------------------
-- DOCUMENTS — projets de loi, propositions, rapports, avis, textes adoptés.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    uid                       TEXT PRIMARY KEY,            -- PRJLANR…, PIONANR…, RAPPANR…, AVISANR…, TADOPTNR…
    dossier_uid               TEXT,                        -- FK dossiers
    legislature               INTEGER,
    type_code                 TEXT,                        -- PRJL | PION | PRIN | RAPP | AVIS | TADOPT | RINF | …
    type_libelle              TEXT,
    sous_type                 TEXT,                        -- 'autorisant la ratification…', etc.
    titre_principal           TEXT,
    titre_court               TEXT,
    numero                    INTEGER,
    date_creation             TEXT,
    date_depot                TEXT,
    date_publication          TEXT,
    auteur_principal_uid      TEXT,                        -- FK deputies (NULL si Gouvernement)
    auteur_qualite            TEXT,                        -- 'auteur', 'rapporteur', etc.
    cosignataires_uids        TEXT,                        -- JSON list
    organe_referent_uid       TEXT,                        -- FK organes
    raw_json                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_dossier ON documents(dossier_uid);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(type_code);
CREATE INDEX IF NOT EXISTS idx_documents_auteur ON documents(auteur_principal_uid);

-- ---------------------------------------------------------------------
-- ACTES_LEGISLATIFS — la navette aplatie en lignes ordonnées.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS actes_legislatifs (
    uid                       TEXT PRIMARY KEY,            -- L17-VD…, L17-AN1-…
    dossier_uid               TEXT NOT NULL,               -- FK dossiers
    parent_uid                TEXT,                        -- chaîne parent dans l'arbre
    ordre                     INTEGER NOT NULL,            -- profondeur DFS, monotone
    code_acte                 TEXT,                        -- AN1-DEPOT, AN1-COM-FOND, …
    libelle                   TEXT,
    libelle_court             TEXT,
    date_acte                 TEXT,
    organe_uid                TEXT,                        -- FK organes
    document_uid              TEXT,                        -- FK documents (texteAssocie)
    texte_adopte_uid          TEXT,                        -- FK documents (texteAdopte)
    type_xsi                  TEXT,                        -- DepotInitiative_Type, EtudeImpact_Type, …
    raw_json                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_actes_dossier ON actes_legislatifs(dossier_uid, ordre);
CREATE INDEX IF NOT EXISTS idx_actes_date ON actes_legislatifs(date_acte);
CREATE INDEX IF NOT EXISTS idx_actes_organe ON actes_legislatifs(organe_uid);

-- ---------------------------------------------------------------------
-- AMENDEMENTS — un par amendement parlementaire.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amendements (
    uid                       TEXT PRIMARY KEY,            -- AMANR5L17PO######B…N######
    legislature               INTEGER,
    numero                    INTEGER,                     -- numéro d'ordre sur le texte visé
    examen_type               TEXT,                        -- 'commission' | 'seance'
    dossier_uid               TEXT,                        -- FK dossiers (peut être NULL si non résolu)
    document_uid              TEXT,                        -- FK documents (le texte visé)
    auteur_uid                TEXT,                        -- FK deputies (premier signataire)
    auteur_nom_complet        TEXT,                        -- denormalisé pour listings rapides
    groupe_uid                TEXT,                        -- FK organes
    groupe_abrege             TEXT,                        -- denormalisé
    cosignataires_count       INTEGER DEFAULT 0,
    article_designation       TEXT,                        -- 'Article 1', 'Article additionnel après l'art. 5'
    article_numero            INTEGER,                     -- ordre stable pour tri (NULL = annexe / additionnel)
    article_addition          TEXT,                        -- 'avant' | 'apres' | NULL
    alinea                    TEXT,
    sort                      TEXT,                        -- 'Adopté' | 'Rejeté' | 'Retiré' | 'Irrecevable' | 'Tombé' | 'Non soutenu' | 'En traitement' | NULL
    sort_brut                 TEXT,                        -- valeur brute du dump pour debug
    date_depot                TEXT,
    date_publication          TEXT,
    date_sort                 TEXT,
    seance_discussion_ref     TEXT,                        -- ref vers séance (PAS un scrutin)
    article_99                INTEGER DEFAULT 0,           -- 0/1 — article 99 du règlement
    parent_uid                TEXT,                        -- sous-amendement → amendement parent
    discussion_commune        TEXT,                        -- ref discussion commune si présente
    discussion_identique      TEXT,                        -- ref discussion identique
    texte                     TEXT,                        -- HTML du dispositif
    expose_sommaire           TEXT,                        -- HTML de l'exposé sommaire
    pdf_url                   TEXT,
    source_url                TEXT,
    ingested_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_amendements_dossier ON amendements(dossier_uid);
CREATE INDEX IF NOT EXISTS idx_amendements_dossier_article ON amendements(dossier_uid, article_numero, numero);
CREATE INDEX IF NOT EXISTS idx_amendements_document ON amendements(document_uid);
CREATE INDEX IF NOT EXISTS idx_amendements_auteur ON amendements(auteur_uid);
CREATE INDEX IF NOT EXISTS idx_amendements_groupe ON amendements(groupe_uid);
CREATE INDEX IF NOT EXISTS idx_amendements_sort ON amendements(sort);
CREATE INDEX IF NOT EXISTS idx_amendements_groupe_sort ON amendements(groupe_uid, sort);
CREATE INDEX IF NOT EXISTS idx_amendements_examen ON amendements(examen_type);

CREATE VIRTUAL TABLE IF NOT EXISTS amendements_fts USING fts5(
    uid UNINDEXED,
    texte,
    expose_sommaire,
    article_designation,
    auteur_nom_complet,
    tokenize = "unicode61 remove_diacritics 2"
);

-- ---------------------------------------------------------------------
-- SCRUTINS — un par scrutin public nominatif.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrutins (
    uid                       TEXT PRIMARY KEY,            -- VTANR5L17V####
    legislature               INTEGER,
    numero                    INTEGER,
    date_scrutin              TEXT,
    seance_ref                TEXT,
    session_ref               TEXT,
    organe_uid                TEXT,                        -- FK organes
    type_vote_code            TEXT,
    type_vote_libelle         TEXT,
    type_majorite             TEXT,
    sort_code                 TEXT,                        -- 'adopté' | 'rejeté' | …
    sort_libelle              TEXT,
    titre                     TEXT,
    objet                     TEXT,
    demandeur                 TEXT,
    mode_publication          TEXT,                        -- 'DecompteNominatif' attendu
    nombre_votants            INTEGER,
    suffrages_exprimes        INTEGER,
    seuil_majorite            INTEGER,
    nb_pour                   INTEGER,
    nb_contre                 INTEGER,
    nb_abstentions            INTEGER,
    nb_non_votants            INTEGER,
    dossier_uid               TEXT,                        -- résolu si on retrouve le lien (souvent NULL)
    source_url                TEXT,
    raw_json                  TEXT,
    ingested_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scrutins_date ON scrutins(date_scrutin);
CREATE INDEX IF NOT EXISTS idx_scrutins_dossier ON scrutins(dossier_uid);
CREATE INDEX IF NOT EXISTS idx_scrutins_numero ON scrutins(numero);
CREATE INDEX IF NOT EXISTS idx_scrutins_sort ON scrutins(sort_code);

-- ---------------------------------------------------------------------
-- VOTES — 1 ligne = 1 position individuelle de député sur un scrutin.
-- Volume : ~3.7M lignes pour la 17e leg.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS votes (
    scrutin_uid               TEXT NOT NULL,               -- FK scrutins
    acteur_uid                TEXT NOT NULL,               -- FK deputies
    groupe_uid                TEXT,                        -- FK organes (groupe au moment du vote)
    position                  TEXT NOT NULL,               -- 'pour' | 'contre' | 'abstention' | 'non_votant'
    par_delegation            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scrutin_uid, acteur_uid)
);
CREATE INDEX IF NOT EXISTS idx_votes_acteur ON votes(acteur_uid);
CREATE INDEX IF NOT EXISTS idx_votes_groupe ON votes(scrutin_uid, groupe_uid);
CREATE INDEX IF NOT EXISTS idx_votes_position ON votes(acteur_uid, position);

-- ---------------------------------------------------------------------
-- SCRUTIN_GROUPES — pré-agrégat par scrutin × groupe.
-- Permet le calcul instantané de la discipline (4.8 s → < 50 ms).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrutin_groupes (
    scrutin_uid          TEXT NOT NULL,
    groupe_uid           TEXT NOT NULL,
    position_majoritaire TEXT,                              -- 'pour' | 'contre' | 'abstention'
    nb_pour              INTEGER NOT NULL DEFAULT 0,
    nb_contre            INTEGER NOT NULL DEFAULT 0,
    nb_abstentions       INTEGER NOT NULL DEFAULT 0,
    nb_non_votants       INTEGER NOT NULL DEFAULT 0,
    nb_membres           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scrutin_uid, groupe_uid)
);
CREATE INDEX IF NOT EXISTS idx_scrutin_groupes_groupe ON scrutin_groupes(groupe_uid);

-- ---------------------------------------------------------------------
-- DISCIPLINE_CACHE — résultat du croisement votes × scrutin_groupes,
-- pré-calculé pour /stats/scrutins (4s → < 50ms).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS groupe_discipline_cache (
    groupe_uid  TEXT PRIMARY KEY,
    expressed   INTEGER NOT NULL DEFAULT 0,
    aligned     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deputy_discipline_cache (
    acteur_uid  TEXT PRIMARY KEY,
    expressed   INTEGER NOT NULL DEFAULT 0,
    aligned     INTEGER NOT NULL DEFAULT 0,
    nb_pour     INTEGER NOT NULL DEFAULT 0,
    nb_contre   INTEGER NOT NULL DEFAULT 0,
    nb_abstention INTEGER NOT NULL DEFAULT 0
);

-- Per-deputy amendement counters (for /stats/amendements top auteurs).
CREATE TABLE IF NOT EXISTS deputy_amd_cache (
    acteur_uid  TEXT PRIMARY KEY,
    total       INTEGER NOT NULL DEFAULT 0,
    adoptes     INTEGER NOT NULL DEFAULT 0,
    rejetes     INTEGER NOT NULL DEFAULT 0,
    retires     INTEGER NOT NULL DEFAULT 0,
    commission  INTEGER NOT NULL DEFAULT 0,
    seance      INTEGER NOT NULL DEFAULT 0
);

-- Per-group amendement counters.
CREATE TABLE IF NOT EXISTS groupe_amd_cache (
    groupe_uid  TEXT PRIMARY KEY,
    total       INTEGER NOT NULL DEFAULT 0,
    adoptes     INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------
-- QUESTIONS_HISTORY — SCD-2 « light » : on garde une ligne PAR transition
-- de statut (sans_reponse → avec_reponse, …) ou changement de date_reponse.
-- Pas un historique exhaustif (cela doublerait la base) mais suffisant
-- pour répondre à : "quand cette question a-t-elle reçu sa réponse ?".
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS questions_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT NOT NULL,
    captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
    statut_avant    TEXT,
    statut_apres    TEXT,
    date_reponse_avant TEXT,
    date_reponse_apres TEXT,
    delai_avant     INTEGER,
    delai_apres     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_questions_history_uid ON questions_history(uid, captured_at);

-- ---------------------------------------------------------------------
-- SEANCES — séances publiques + commissions issues du dump Agenda.json.
-- Une séance porte un compteRenduRef vers le syseron XML qui contient
-- la transcription complète (sommaire + corps).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seances (
    uid                 TEXT PRIMARY KEY,           -- RUANR5L17S2025IDS28624
    legislature         INTEGER,
    type_xsi            TEXT,                        -- seance_type | commission_type | ...
    date_seance         TEXT,
    num_seance_jour     INTEGER,                     -- 1=matin, 2=après-midi, 3=soir
    num_seance_jo       INTEGER,
    quantieme           TEXT,                        -- 'Première', 'Deuxième', …
    date_debut          TEXT,
    date_fin            TEXT,
    organe_uid          TEXT,                        -- organeReuniRef
    session_ref         TEXT,
    compte_rendu_uid    TEXT,                        -- CRSANR5L17S2025O1N037
    captation_video     INTEGER DEFAULT 0,
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_seances_date ON seances(date_seance);
CREATE INDEX IF NOT EXISTS idx_seances_compte_rendu ON seances(compte_rendu_uid);
CREATE INDEX IF NOT EXISTS idx_seances_type ON seances(type_xsi);

-- ---------------------------------------------------------------------
-- SEANCE_INTERVENTIONS — sommaire2 du compte-rendu : un point pris à
-- la séance (une QAG, un débat sur un texte, etc.) avec ses orateurs.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS seance_interventions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    seance_uid          TEXT NOT NULL,                -- FK seances
    compte_rendu_uid    TEXT,
    ordre               INTEGER NOT NULL,             -- 1, 2, 3 …
    sommaire1_titre     TEXT,                          -- "Questions au Gouvernement"
    sommaire2_titre     TEXT,                          -- "Indemnisation des incorporés…"
    speakers_json       TEXT,                          -- JSON list of speaker descriptions
    syceron_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_seance_interventions_seance ON seance_interventions(seance_uid, ordre);
CREATE INDEX IF NOT EXISTS idx_seance_interventions_section ON seance_interventions(sommaire1_titre);

-- ---------------------------------------------------------------------
-- AMENDEMENT_CLUSTERS — quasi-doublons détectés par MinHash.
-- 1 amendement appartient à au plus 1 cluster ; cluster_size>=2.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amendement_clusters (
    cluster_id   INTEGER NOT NULL,
    amendement_uid TEXT NOT NULL,
    PRIMARY KEY (cluster_id, amendement_uid)
);
CREATE INDEX IF NOT EXISTS idx_amendement_clusters_amd
    ON amendement_clusters(amendement_uid);
CREATE INDEX IF NOT EXISTS idx_amendement_clusters_cluster
    ON amendement_clusters(cluster_id);

-- ---------------------------------------------------------------------
-- CIRCO_STATS — population (INSEE) + inscrits/votants (Min. Intérieur)
-- par circonscription. Les Français de l'étranger (11 circos) n'ont pas
-- de population résidente, le champ est NULL pour ces lignes.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS circo_stats (
    dept_code   TEXT NOT NULL,           -- INSEE 01–95, 2A/2B, 971–988
    circo_num   INTEGER NOT NULL,        -- 1, 2, 3, …
    population  INTEGER,                  -- INSEE population municipale 2021
    inscrits    INTEGER,                  -- Min. Intérieur, législatives 2024 T1
    votants     INTEGER,
    abstentions INTEGER,
    source_pop  TEXT,                     -- url/version pop
    source_inscr TEXT,                    -- url/version inscrits
    PRIMARY KEY (dept_code, circo_num)
);
CREATE INDEX IF NOT EXISTS idx_circo_stats_dept ON circo_stats(dept_code);
