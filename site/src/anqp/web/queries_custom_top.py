"""Générateur de tops personnalisés.

Idée : laisser l'utilisateur choisir une entité + une métrique + des filtres
optionnels, et calculer le classement à la volée.

ZÉRO concaténation de SQL avec input utilisateur. Tout est lookupé dans des
tables Python (METRICS, FILTERS, ENTITY_TEMPLATES) et seuls les paramètres
typés/sanitisés vont en `:param` de l'exec SQLite.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field

from .legislature import current_legislature

# ----------------------------------------------------------------------
# Filtres exposés à l'UI — chaque filtre dit comment se valider, comment
# rendre son sélecteur HTML et comment se brancher dans le SQL.
# ----------------------------------------------------------------------
FILTER_TYPES = {
    "groupe_uid": {"label": "Groupe parlementaire", "ui": "select_groupes", "type": "uid"},
    "date_min":   {"label": "Date min",              "ui": "date",            "type": "date"},
    "date_max":   {"label": "Date max",              "ui": "date",            "type": "date"},
    "question_type": {"label": "Type de question",   "ui": "select_qtype",    "type": "enum",
                      "values": [("QE", "Écrite"), ("QOSD", "Orale"), ("QG", "Au Gouv.")]},
    "ministere":  {"label": "Ministère",             "ui": "text",            "type": "str"},
    "amd_sort":   {"label": "Sort de l'amendement",  "ui": "select_sort",     "type": "enum",
                   "values": [("Adopté", "Adopté"), ("Rejeté", "Rejeté"), ("Retiré", "Retiré"),
                              ("Tombé", "Tombé"), ("Irrecevable", "Irrecevable"),
                              ("Non soutenu", "Non soutenu")]},
    "examen":     {"label": "Examen",                "ui": "select_examen",   "type": "enum",
                   "values": [("commission", "Commission"), ("seance", "Séance")]},
    "texte_type": {"label": "Type de texte",         "ui": "select_texte_type", "type": "enum",
                   "values": [("pjl", "PJL — Projet de loi"),
                              ("ppl", "PPL — Proposition de loi"),
                              ("resolution", "Résolution"),
                              ("rapport", "Rapport / mission")]},
    "statut_dossier": {"label": "Statut du texte",   "ui": "select_statut",   "type": "enum",
                       "values": [("en_cours", "En cours"), ("adopte", "Adopté"),
                                  ("promulgue", "Promulguée"), ("rejete", "Rejeté"),
                                  ("retire", "Retiré"), ("caduc", "Caduc")]},
    "scrutin_dossier": {"label": "Lié à un texte particulier (UID)",
                        "ui": "text", "type": "str"},
}


# ----------------------------------------------------------------------
# Templates SQL par entité — chaque métrique se branche dans le template
# de son entité via {value_expr} / {joins} / {extra_where} / {having}.
# ----------------------------------------------------------------------
_TPL_DEPUTES = """
SELECT d.uid                                    AS uid,
       d.nom_complet                            AS title,
       coalesce(d.groupe_abrege, '—')           AS subtitle,
       coalesce(d.groupe_couleur, '#94a3b8')    AS color,
       '/photo/' || d.uid                        AS image,
       '/deputes/' || d.uid                      AS link,
       {value_expr}                             AS value
  FROM deputies d
  {joins}
 WHERE d.legislature = :leg
   {extra_where}
 GROUP BY d.uid
HAVING {having}
 ORDER BY value {direction}
 LIMIT :limit
"""

_TPL_TEXTES = """
SELECT t.uid                                    AS uid,
       t.titre                                  AS title,
       coalesce(t.statut, '—')                  AS subtitle,
       NULL                                     AS color,
       NULL                                     AS image,
       '/textes/' || t.uid                       AS link,
       {value_expr}                             AS value
  FROM dossiers t
  {joins}
 WHERE t.legislature = :leg
   {extra_where}
 GROUP BY t.uid
HAVING {having}
 ORDER BY value {direction}
 LIMIT :limit
"""

_TPL_SCRUTINS = """
SELECT s.uid                                    AS uid,
       'Scrutin n°' || s.numero                 AS title,
       coalesce(nullif(s.titre, ''), s.objet, '—')  AS subtitle,
       NULL                                     AS color,
       NULL                                     AS image,
       '/scrutins/' || s.uid                     AS link,
       {value_expr}                             AS value
  FROM scrutins s
  {joins}
 WHERE s.legislature = :leg
   {extra_where}
 ORDER BY value {direction}
 LIMIT :limit
"""

_TPL_MINISTERES = """
SELECT q.ministere_interroge                    AS uid,
       q.ministere_interroge                    AS title,
       '—'                                       AS subtitle,
       '#0891b2'                                 AS color,
       NULL                                     AS image,
       '/questions?ministere=' || replace(q.ministere_interroge, ' ', '%20')  AS link,
       {value_expr}                             AS value
  FROM questions q
 WHERE q.legislature = :leg
   AND q.ministere_interroge IS NOT NULL
   AND q.ministere_interroge <> ''
   {extra_where}
 GROUP BY q.ministere_interroge
HAVING {having}
 ORDER BY value {direction}
 LIMIT :limit
"""

_TPL_GROUPES = """
SELECT o.uid                                    AS uid,
       o.libelle_abrege || ' — ' || o.libelle  AS title,
       coalesce(o.libelle, '—')                 AS subtitle,
       coalesce(o.couleur, '#94a3b8')           AS color,
       NULL                                     AS image,
       '/deputes?groupe_uid=' || o.uid           AS link,
       {value_expr}                             AS value
  FROM organes o
  {joins}
 WHERE o.code_type = 'GP'
   AND o.legislature = :leg
   {extra_where}
 GROUP BY o.uid
HAVING {having}
 ORDER BY value {direction}
 LIMIT :limit
"""

_TPL_AMENDEMENTS = """
SELECT a.uid                                    AS uid,
       'Amendement n°' || coalesce(a.numero, '?') AS title,
       coalesce(a.auteur_nom_complet, '—')      AS subtitle,
       coalesce(o.couleur, '#94a3b8')           AS color,
       NULL                                     AS image,
       '/amendements/' || a.uid                  AS link,
       {value_expr}                             AS value
  FROM amendements a
  LEFT JOIN organes o ON o.uid = a.groupe_uid
  {joins}
 WHERE a.legislature = :leg
   {extra_where}
 ORDER BY value {direction}
 LIMIT :limit
"""

_TPL_QUESTIONS = """
SELECT q.uid                                    AS uid,
       coalesce(q.titre, '(sans titre)')        AS title,
       coalesce(q.auteur_nom_complet, '—') || ' · ' || coalesce(q.ministere_interroge_court, '?') AS subtitle,
       coalesce(o.couleur, '#94a3b8')           AS color,
       NULL                                     AS image,
       '/questions/' || q.uid                    AS link,
       {value_expr}                             AS value
  FROM questions q
  LEFT JOIN organes o ON o.uid = q.auteur_groupe_uid
  {joins}
 WHERE q.legislature = :leg
   {extra_where}
 ORDER BY value {direction}
 LIMIT :limit
"""

_ENTITY_TEMPLATES = {
    "deputes":     _TPL_DEPUTES,
    "textes":      _TPL_TEXTES,
    "scrutins":    _TPL_SCRUTINS,
    "ministeres":  _TPL_MINISTERES,
    "groupes":     _TPL_GROUPES,
    "amendements": _TPL_AMENDEMENTS,
    "questions":   _TPL_QUESTIONS,
}

ENTITY_LABELS = {
    "deputes":     "Députés",
    "textes":      "Textes (PJL/PPL/résolutions)",
    "scrutins":    "Scrutins publics",
    "ministeres":  "Ministères",
    "groupes":     "Groupes parlementaires",
    "amendements": "Amendements individuels",
    "questions":   "Questions individuelles",
}


# ----------------------------------------------------------------------
# Spec d'une métrique — décrit son SQL, ses filtres acceptés, son label.
# ----------------------------------------------------------------------
@dataclass
class MetricSpec:
    key: str
    entity: str
    label: str
    category: str
    value_label: str           # e.g. "questions", "amendements", "%"
    description: str           # tooltip
    value_expr: str            # SQL agg, e.g. "COUNT(q.uid)"
    joins: str = ""            # extra JOINs to inject
    extra_where: str = ""      # base extra WHERE conditions (always on)
    having: str = "value > 0"  # condition on the aggregated value
    # filter_key -> SQL fragment using :filter_key for the bind
    filter_map: dict = field(default_factory=dict)
    # ratio metrics: pretty print as percentage
    is_percentage: bool = False
    # rounding for display
    digits: int = 0


# ======================================================================
# CATALOGUE DES MÉTRIQUES
# ======================================================================
METRICS: dict[str, MetricSpec] = {}


def _add(spec: MetricSpec) -> None:
    METRICS[spec.key] = spec


# ----- DÉPUTÉS -----
_add(MetricSpec(
    key="deputes_questions_total", entity="deputes",
    label="Questions posées (toutes)", category="Activité — questions",
    value_label="questions",
    description="Total des questions parlementaires (écrites, orales, au Gouvernement).",
    value_expr="COUNT(q.uid)",
    joins="LEFT JOIN questions q ON q.auteur_uid = d.uid",
    filter_map={
        "date_min":      "AND q.date_publication_question >= :date_min",
        "date_max":      "AND q.date_publication_question <= :date_max",
        "question_type": "AND q.type = :question_type",
        "ministere":     "AND q.ministere_interroge_court LIKE :ministere",
        "groupe_uid":    "AND d.groupe_uid = :groupe_uid",
    },
))

_add(MetricSpec(
    key="deputes_questions_sans_reponse", entity="deputes",
    label="Questions sans réponse",
    category="Activité — questions",
    value_label="questions",
    description="Nombre de questions du député toujours sans réponse publiée.",
    value_expr="SUM(CASE WHEN q.statut = 'sans_reponse' THEN 1 ELSE 0 END)",
    joins="LEFT JOIN questions q ON q.auteur_uid = d.uid",
    filter_map={
        "date_min":   "AND q.date_publication_question >= :date_min",
        "date_max":   "AND q.date_publication_question <= :date_max",
        "groupe_uid": "AND d.groupe_uid = :groupe_uid",
    },
))

_add(MetricSpec(
    key="deputes_delai_moyen_reponse", entity="deputes",
    label="Délai moyen de réponse à leurs questions (jours)",
    category="Activité — questions",
    value_label="j",
    description="Délai moyen avant qu'une question du député reçoive une réponse.",
    value_expr="AVG(q.delai_reponse_jours)",
    joins="LEFT JOIN questions q ON q.auteur_uid = d.uid AND q.delai_reponse_jours IS NOT NULL",
    having="value IS NOT NULL",
    digits=1,
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_amendements_total", entity="deputes",
    label="Amendements déposés (total)",
    category="Activité — amendements",
    value_label="amendements",
    description="Tous les amendements déposés par le député (commission + séance).",
    value_expr="COUNT(a.uid)",
    joins="LEFT JOIN amendements a ON a.auteur_uid = d.uid",
    filter_map={
        "date_min":   "AND a.date_depot >= :date_min",
        "date_max":   "AND a.date_depot <= :date_max",
        "amd_sort":   "AND a.sort = :amd_sort",
        "examen":     "AND a.examen_type = :examen",
        "groupe_uid": "AND d.groupe_uid = :groupe_uid",
    },
))

_add(MetricSpec(
    key="deputes_amendements_adoptes", entity="deputes",
    label="Amendements adoptés",
    category="Activité — amendements",
    value_label="amendements",
    description="Amendements du député dont le sort officiel est « Adopté ».",
    value_expr="COUNT(a.uid)",
    joins="LEFT JOIN amendements a ON a.auteur_uid = d.uid AND a.sort = 'Adopté'",
    filter_map={
        "date_min":   "AND a.date_depot >= :date_min",
        "date_max":   "AND a.date_depot <= :date_max",
        "examen":     "AND a.examen_type = :examen",
        "groupe_uid": "AND d.groupe_uid = :groupe_uid",
    },
))

_add(MetricSpec(
    key="deputes_amendements_taux_adoption", entity="deputes",
    label="Taux d'adoption de leurs amendements (%)",
    category="Activité — amendements",
    value_label="%", is_percentage=True, digits=1,
    description="Ratio d'amendements adoptés sur le total déposé. ≥ 20 amendements requis.",
    value_expr="100.0 * SUM(CASE WHEN a.sort = 'Adopté' THEN 1 ELSE 0 END) / COUNT(a.uid)",
    joins="LEFT JOIN amendements a ON a.auteur_uid = d.uid",
    having="COUNT(a.uid) >= 20",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_votes_exprimes", entity="deputes",
    label="Votes exprimés (pour/contre/abstention)",
    category="Activité — scrutins",
    value_label="votes",
    description="Nombre total de votes exprimés en scrutin public nominatif.",
    value_expr="c.expressed",
    joins="LEFT JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid",
    having="c.expressed > 0",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_pour", entity="deputes",
    label="Votes « pour »", category="Activité — scrutins",
    value_label="pour",
    description="Nombre de fois où le député a voté pour.",
    value_expr="c.nb_pour",
    joins="LEFT JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid",
    having="c.nb_pour > 0",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_contre", entity="deputes",
    label="Votes « contre »", category="Activité — scrutins",
    value_label="contre",
    description="Nombre de fois où le député a voté contre.",
    value_expr="c.nb_contre",
    joins="LEFT JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid",
    having="c.nb_contre > 0",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_abstention", entity="deputes",
    label="Abstentions", category="Activité — scrutins",
    value_label="abst.",
    description="Nombre de fois où le député s'est abstenu.",
    value_expr="c.nb_abstention",
    joins="LEFT JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid",
    having="c.nb_abstention > 0",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_discipline", entity="deputes",
    label="Discipline (% alignés sur le groupe)",
    category="Vote — discipline",
    value_label="%", is_percentage=True, digits=1,
    description="Part des votes alignés sur la majorité du groupe parlementaire. ≥ 50 votes exprimés.",
    value_expr="100.0 * c.aligned / NULLIF(c.expressed, 0)",
    joins="LEFT JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid",
    having="c.expressed >= 50",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_dissidences", entity="deputes",
    label="Dissidences (votes contre la majorité du groupe)",
    category="Vote — discipline",
    value_label="dissid.",
    description="Nombre de votes du député contraires à la majorité de son groupe. ≥ 50 votes exprimés.",
    value_expr="(c.expressed - c.aligned)",
    joins="LEFT JOIN deputy_discipline_cache c ON c.acteur_uid = d.uid",
    having="c.expressed >= 50 AND (c.expressed - c.aligned) > 0",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_age", entity="deputes",
    label="Âge (années)",
    category="Profil",
    value_label="ans", digits=0,
    description="Âge actuel calculé depuis la date de naissance.",
    value_expr="CAST((julianday('now') - julianday(d.date_naissance)) / 365.25 AS INT)",
    having="d.date_naissance IS NOT NULL",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_anciennete", entity="deputes",
    label="Ancienneté (jours depuis le début du mandat)",
    category="Profil",
    value_label="jours", digits=0,
    description="Nombre de jours depuis la date de début de mandat.",
    value_expr="CAST(julianday('now') - julianday(d.date_debut_mandat) AS INT)",
    having="d.date_debut_mandat IS NOT NULL",
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_activite_totale", entity="deputes",
    label="Activité totale (questions + amendements + votes)",
    category="Activité — agrégée",
    value_label="actes",
    description="Cumul questions posées + amendements déposés + votes exprimés.",
    value_expr=(
        "(SELECT COUNT(*) FROM questions WHERE auteur_uid = d.uid) "
        "+ (SELECT COUNT(*) FROM amendements WHERE auteur_uid = d.uid) "
        "+ coalesce((SELECT expressed FROM deputy_discipline_cache WHERE acteur_uid = d.uid), 0)"
    ),
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

_add(MetricSpec(
    key="deputes_amdts_dans_clusters", entity="deputes",
    label="Amendements en doublons (cluster)",
    category="Doublons",
    value_label="amendements",
    description="Nombre d'amendements du député qui appartiennent à un cluster de quasi-doublons (MinHash≥0,80).",
    value_expr=(
        "(SELECT COUNT(*) FROM amendements a "
         "JOIN amendement_clusters ac ON ac.amendement_uid = a.uid "
         "WHERE a.auteur_uid = d.uid)"
    ),
    filter_map={"groupe_uid": "AND d.groupe_uid = :groupe_uid"},
))

# ----- TEXTES -----
_add(MetricSpec(
    key="textes_amendements_deposes", entity="textes",
    label="Amendements déposés",
    category="Volume",
    value_label="amendements",
    description="Nombre total d'amendements déposés sur le texte.",
    value_expr="COUNT(a.uid)",
    joins="LEFT JOIN amendements a ON a.dossier_uid = t.uid",
    filter_map={
        "amd_sort":       "AND a.sort = :amd_sort",
        "examen":         "AND a.examen_type = :examen",
        "statut_dossier": "AND t.statut = :statut_dossier",
    },
))

_add(MetricSpec(
    key="textes_amendements_adoptes", entity="textes",
    label="Amendements adoptés",
    category="Volume",
    value_label="adoptés",
    description="Amendements adoptés sur le texte.",
    value_expr="SUM(CASE WHEN a.sort = 'Adopté' THEN 1 ELSE 0 END)",
    joins="LEFT JOIN amendements a ON a.dossier_uid = t.uid",
    filter_map={"statut_dossier": "AND t.statut = :statut_dossier"},
))

_add(MetricSpec(
    key="textes_taux_adoption", entity="textes",
    label="Taux d'adoption des amendements (%)",
    category="Ratio",
    value_label="%", is_percentage=True, digits=1,
    description="Ratio amendements adoptés / déposés. ≥ 50 amendements requis.",
    value_expr="100.0 * SUM(CASE WHEN a.sort = 'Adopté' THEN 1 ELSE 0 END) / COUNT(a.uid)",
    joins="LEFT JOIN amendements a ON a.dossier_uid = t.uid",
    having="COUNT(a.uid) >= 50",
    filter_map={"statut_dossier": "AND t.statut = :statut_dossier"},
))

_add(MetricSpec(
    key="textes_auteurs_uniques", entity="textes",
    label="Auteurs uniques d'amendements",
    category="Diversité",
    value_label="auteurs",
    description="Nombre de députés différents ayant déposé au moins un amendement.",
    value_expr="COUNT(DISTINCT a.auteur_uid)",
    joins="LEFT JOIN amendements a ON a.dossier_uid = t.uid",
    filter_map={"examen": "AND a.examen_type = :examen"},
))

_add(MetricSpec(
    key="textes_clusters_doublons", entity="textes",
    label="Clusters d'amendements quasi-identiques",
    category="Doublons",
    value_label="clusters",
    description="Groupes d'amendements MinHash≥0,80 détectés sur le texte (toutes typologies confondues).",
    value_expr="COUNT(DISTINCT ac.cluster_id)",
    joins=("LEFT JOIN amendements a ON a.dossier_uid = t.uid "
           "LEFT JOIN amendement_clusters ac ON ac.amendement_uid = a.uid"),
))

_add(MetricSpec(
    key="textes_clusters_obstruction", entity="textes",
    label="Clusters d'obstruction (≥ 15 doublons)",
    category="Doublons",
    value_label="clusters",
    description="Clusters classés « obstruction » (≥ 15 amendements quasi-identiques) sur le texte.",
    value_expr=(
        "(SELECT COUNT(*) FROM ("
        "  SELECT cluster_id FROM ("
        "    SELECT ac.cluster_id, COUNT(*) AS sz"
        "      FROM amendement_clusters ac"
        "      JOIN amendements am ON am.uid = ac.amendement_uid"
        "     WHERE am.dossier_uid = t.uid"
        "     GROUP BY ac.cluster_id"
        "  ) WHERE sz >= 15"
        "))"
    ),
))

_add(MetricSpec(
    key="textes_amdts_dans_clusters", entity="textes",
    label="Amendements en doublons (volume)",
    category="Doublons",
    value_label="amendements",
    description="Nombre d'amendements sur le texte qui appartiennent à un cluster de doublons.",
    value_expr="COUNT(ac.amendement_uid)",
    joins=("LEFT JOIN amendements a ON a.dossier_uid = t.uid "
           "LEFT JOIN amendement_clusters ac ON ac.amendement_uid = a.uid"),
))

_add(MetricSpec(
    key="textes_delai_navette", entity="textes",
    label="Délai dépôt → dernier acte (jours)",
    category="Temporel",
    value_label="jours", digits=0,
    description="Nombre de jours entre la date de dépôt et le dernier acte connu.",
    value_expr="CAST(julianday(t.date_dernier_acte) - julianday(t.date_depot) AS INT)",
    having="t.date_depot IS NOT NULL AND t.date_dernier_acte IS NOT NULL",
    filter_map={"statut_dossier": "AND t.statut = :statut_dossier"},
))

# ----- SCRUTINS -----
_add(MetricSpec(
    key="scrutins_ecart", entity="scrutins",
    label="Écart pour vs contre (resserrement)",
    category="Marges",
    value_label="voix",
    description="Différence absolue entre les voix pour et contre.",
    value_expr="ABS(s.nb_pour - s.nb_contre)",
    having="s.nb_pour IS NOT NULL AND s.nb_contre IS NOT NULL",
    filter_map={
        "date_min":         "AND s.date_scrutin >= :date_min",
        "date_max":         "AND s.date_scrutin <= :date_max",
        "scrutin_dossier":  "AND s.dossier_uid = :scrutin_dossier",
    },
))

_add(MetricSpec(
    key="scrutins_participation", entity="scrutins",
    label="Participation (suffrages exprimés)",
    category="Volume",
    value_label="votes",
    description="Nombre total de suffrages exprimés.",
    value_expr="s.suffrages_exprimes",
    having="s.suffrages_exprimes IS NOT NULL",
    filter_map={
        "date_min": "AND s.date_scrutin >= :date_min",
        "date_max": "AND s.date_scrutin <= :date_max",
    },
))

_add(MetricSpec(
    key="scrutins_non_votants", entity="scrutins",
    label="Non-votants",
    category="Volume",
    value_label="non-votants",
    description="Nombre de députés non-votants sur ce scrutin.",
    value_expr="s.nb_non_votants",
    having="s.nb_non_votants IS NOT NULL",
    filter_map={
        "date_min": "AND s.date_scrutin >= :date_min",
        "date_max": "AND s.date_scrutin <= :date_max",
    },
))

_add(MetricSpec(
    key="scrutins_pour", entity="scrutins",
    label="Voix « pour »",
    category="Volume",
    value_label="pour",
    description="Nombre de voix pour.",
    value_expr="s.nb_pour",
    having="s.nb_pour IS NOT NULL",
    filter_map={
        "date_min": "AND s.date_scrutin >= :date_min",
        "date_max": "AND s.date_scrutin <= :date_max",
    },
))

# ----- MINISTÈRES -----
_add(MetricSpec(
    key="ministeres_questions_recues", entity="ministeres",
    label="Questions reçues (toutes)",
    category="Volume",
    value_label="questions",
    description="Nombre total de questions adressées au ministère.",
    value_expr="COUNT(q.uid)",
    filter_map={
        "date_min":      "AND q.date_publication_question >= :date_min",
        "date_max":      "AND q.date_publication_question <= :date_max",
        "question_type": "AND q.type = :question_type",
    },
))

_add(MetricSpec(
    key="ministeres_taux_reponse", entity="ministeres",
    label="Taux de réponse (%)",
    category="Réactivité",
    value_label="%", is_percentage=True, digits=1,
    description="Pourcentage de questions ayant reçu une réponse. ≥ 50 questions requises.",
    value_expr="100.0 * SUM(CASE WHEN q.statut = 'avec_reponse' THEN 1 ELSE 0 END) / COUNT(q.uid)",
    having="COUNT(q.uid) >= 50",
))

_add(MetricSpec(
    key="ministeres_delai_moyen", entity="ministeres",
    label="Délai moyen de réponse (jours)",
    category="Réactivité",
    value_label="j", digits=1,
    description="Délai moyen de réponse en jours, calculé sur les questions répondues. ≥ 30 questions requises.",
    value_expr="AVG(q.delai_reponse_jours)",
    extra_where="AND q.delai_reponse_jours IS NOT NULL",
    having="COUNT(q.uid) >= 30",
))

# ----- GROUPES PARLEMENTAIRES -----
_add(MetricSpec(
    key="groupes_effectif", entity="groupes",
    label="Effectif (nombre de députés)",
    category="Taille",
    value_label="députés",
    description="Nombre de députés actifs membres du groupe.",
    value_expr="(SELECT COUNT(*) FROM deputies WHERE groupe_uid = o.uid AND is_active = 1 AND legislature = :leg)",
))

_add(MetricSpec(
    key="groupes_amendements_total", entity="groupes",
    label="Amendements déposés (cumul du groupe)",
    category="Activité",
    value_label="amendements",
    description="Total des amendements déposés par les membres du groupe.",
    value_expr="(SELECT COUNT(*) FROM amendements WHERE groupe_uid = o.uid)",
))

_add(MetricSpec(
    key="groupes_discipline", entity="groupes",
    label="Discipline interne (% alignés)",
    category="Cohésion",
    value_label="%", is_percentage=True, digits=1,
    description="Part des votes alignés sur la majorité du groupe (≥ 100 votes au total).",
    value_expr="100.0 * c.aligned / NULLIF(c.expressed, 0)",
    joins="LEFT JOIN groupe_discipline_cache c ON c.groupe_uid = o.uid",
    having="c.expressed >= 100",
))

# ----- AMENDEMENTS -----
_add(MetricSpec(
    key="amendements_longueur", entity="amendements",
    label="Longueur du texte (caractères)",
    category="Forme",
    value_label="car.",
    description="Taille du dispositif en nombre de caractères (HTML strippé).",
    value_expr="LENGTH(a.texte)",
    extra_where="AND a.texte IS NOT NULL",
    filter_map={
        "amd_sort":   "AND a.sort = :amd_sort",
        "examen":     "AND a.examen_type = :examen",
        "groupe_uid": "AND a.groupe_uid = :groupe_uid",
    },
))

# ----- QUESTIONS -----
_add(MetricSpec(
    key="questions_delai", entity="questions",
    label="Délai de réponse (jours)",
    category="Réactivité",
    value_label="j",
    description="Nombre de jours écoulés entre la question et sa réponse.",
    value_expr="q.delai_reponse_jours",
    extra_where="AND q.delai_reponse_jours IS NOT NULL",
    filter_map={"question_type": "AND q.type = :question_type"},
))

_add(MetricSpec(
    key="questions_longueur_question", entity="questions",
    label="Longueur de la question (caractères)",
    category="Forme",
    value_label="car.",
    description="Taille du texte de la question.",
    value_expr="LENGTH(q.texte_question)",
    extra_where="AND q.texte_question IS NOT NULL",
    filter_map={"question_type": "AND q.type = :question_type"},
))


# ----------------------------------------------------------------------
# Validation des paramètres venant de l'utilisateur.
# ----------------------------------------------------------------------
def list_metrics_for_entity(entity: str) -> list[MetricSpec]:
    return sorted(
        [m for m in METRICS.values() if m.entity == entity],
        key=lambda m: (m.category, m.label),
    )


def list_filters_for_metric(metric: MetricSpec) -> list[dict]:
    """Filtres disponibles pour cette métrique, dans l'ordre de l'UI."""
    out = []
    for k in metric.filter_map:
        if k in FILTER_TYPES:
            f = dict(FILTER_TYPES[k])
            f["key"] = k
            out.append(f)
    return out


def _sanitize_filters(metric: MetricSpec, raw: dict) -> dict:
    """Garde uniquement les filtres acceptés par la métrique, et valide leur type."""
    out = {}
    for k, v in (raw or {}).items():
        if not v:
            continue
        if k not in metric.filter_map or k not in FILTER_TYPES:
            continue
        ftype = FILTER_TYPES[k]
        # validation de surface — les binds SQL feront le reste
        if ftype["type"] == "uid":
            if isinstance(v, str) and v.startswith("PO") and v[2:].isdigit():
                out[k] = v
        elif ftype["type"] == "date":
            if isinstance(v, str) and len(v) == 10 and v[4] == '-' and v[7] == '-':
                out[k] = v
        elif ftype["type"] == "enum":
            allowed = {x for x, _ in ftype["values"]}
            if v in allowed:
                out[k] = v
        elif ftype["type"] == "str":
            v = str(v).strip()[:120]
            if v:
                # Pour le filtre "ministère" on enrobe avec %
                if k == "ministere":
                    out[k] = f"%{v}%"
                else:
                    out[k] = v
    return out


# ----------------------------------------------------------------------
# Exécution
# ----------------------------------------------------------------------
DIRECTIONS = {"desc": "DESC", "asc": "ASC"}


def run_top(
    conn: sqlite3.Connection,
    *,
    metric_key: str,
    direction: str = "desc",
    limit: int = 25,
    filters: dict | None = None,
) -> dict:
    """Exécute le top et renvoie {metric, rows, filters_applied, sql}."""
    if metric_key not in METRICS:
        raise ValueError(f"Métrique inconnue : {metric_key}")
    m = METRICS[metric_key]
    if direction not in DIRECTIONS:
        direction = "desc"
    direction_sql = DIRECTIONS[direction]
    try:
        limit = max(5, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 25

    safe = _sanitize_filters(m, filters or {})
    template = _ENTITY_TEMPLATES[m.entity]

    # Concatène les fragments de filtres correspondant à la sélection user.
    filter_sql = " ".join(m.filter_map[k] for k in safe)
    extra_where = m.extra_where + " " + filter_sql

    sql = template.format(
        value_expr=m.value_expr,
        joins=m.joins,
        extra_where=extra_where,
        having=m.having,
        direction=direction_sql,
    )

    params: dict = {"leg": current_legislature(), "limit": limit}
    params.update(safe)

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {
        "metric": m,
        "rows": rows,
        "direction": direction,
        "limit": limit,
        "filters_applied": safe,
    }


# ----------------------------------------------------------------------
# Suggestions surprises (footer "découverte")
# ----------------------------------------------------------------------
SUGGESTIONS = [
    {"metric_key": "deputes_amendements_taux_adoption", "direction": "desc", "limit": 10,
     "title": "Députés au meilleur taux d'adoption d'amendements"},
    {"metric_key": "deputes_dissidences", "direction": "desc", "limit": 10,
     "title": "Députés les plus dissidents face à leur groupe"},
    {"metric_key": "deputes_age", "direction": "asc", "limit": 10,
     "title": "Plus jeunes députés"},
    {"metric_key": "deputes_age", "direction": "desc", "limit": 10,
     "title": "Doyens de l'Assemblée"},
    {"metric_key": "deputes_anciennete", "direction": "desc", "limit": 10,
     "title": "Députés au mandat le plus long"},
    {"metric_key": "textes_amendements_deposes", "direction": "desc", "limit": 10,
     "title": "Textes les plus amendés"},
    {"metric_key": "textes_taux_adoption", "direction": "desc", "limit": 10,
     "title": "Textes au plus haut taux d'adoption d'amendements"},
    {"metric_key": "textes_clusters_doublons", "direction": "desc", "limit": 10,
     "title": "Textes avec le plus de doublons potentiels d'amendements"},
    {"metric_key": "textes_clusters_obstruction", "direction": "desc", "limit": 10,
     "title": "Textes les plus visés par l'obstruction (clusters ≥ 15 doublons)"},
    {"metric_key": "deputes_amdts_dans_clusters", "direction": "desc", "limit": 10,
     "title": "Députés signataires du plus d'amendements en doublons"},
    {"metric_key": "textes_delai_navette", "direction": "desc", "limit": 10,
     "title": "Textes ayant passé le plus de jours en navette"},
    {"metric_key": "scrutins_ecart", "direction": "asc", "limit": 10,
     "title": "Scrutins les plus serrés (écart minimal)"},
    {"metric_key": "scrutins_non_votants", "direction": "desc", "limit": 10,
     "title": "Scrutins avec le plus de non-votants"},
    {"metric_key": "ministeres_taux_reponse", "direction": "asc", "limit": 10,
     "title": "Ministères au plus mauvais taux de réponse"},
    {"metric_key": "ministeres_delai_moyen", "direction": "desc", "limit": 10,
     "title": "Ministères les plus lents à répondre"},
    {"metric_key": "groupes_discipline", "direction": "desc", "limit": 10,
     "title": "Groupes les plus disciplinés en vote"},
    {"metric_key": "groupes_discipline", "direction": "asc", "limit": 10,
     "title": "Groupes les moins disciplinés en vote"},
    {"metric_key": "amendements_longueur", "direction": "desc", "limit": 10,
     "title": "Amendements les plus longs (caractères)"},
    {"metric_key": "deputes_questions_sans_reponse", "direction": "desc", "limit": 10,
     "title": "Députés dont le plus de questions sont restées sans réponse"},
    {"metric_key": "deputes_activite_totale", "direction": "desc", "limit": 10,
     "title": "Députés les plus actifs (Q + A + V cumulés)"},
]


def random_suggestions(n: int = 4, exclude_metric_key: str | None = None) -> list[dict]:
    pool = [s for s in SUGGESTIONS if s["metric_key"] != exclude_metric_key]
    return random.sample(pool, min(n, len(pool)))
