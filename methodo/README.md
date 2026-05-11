# Note méthodologique — 577deputes.fr

Source LaTeX de la note méthodologique : formulation des indicateurs du site,
analyse de sensibilité aux paramètres, rapprochement à la littérature, limites.
Trilingue (français / anglais / espagnol).

## Compilation

Avec `pdfLaTeX` (sur Overleaf : compilateur « pdfLaTeX », rien d'exotique) :

```
pdflatex main.tex
pdflatex main.tex   # 2e passe pour la table des matières et les renvois
```

Fichiers :
- `main.tex` — préambule, page de titre, résumés trilingues, Partie I (français), bibliographie
- `en.tex` — Partie II (English)
- `es.tex` — Parte III (español)

La bibliographie est en `thebibliography` directement dans `main.tex` (pas de
BibTeX requis).

## Artefacts reproductibles

L'analyse de sensibilité est calculée par
[`../scripts/methodo_sensitivity.py`](../scripts/methodo_sensitivity.py)
(bibliothèque standard uniquement, lecture seule sur une copie de la base
SQLite). Le PDF rendu, ce script et ses tableaux de sortie (CSV / Markdown) sont
déposés sur Zenodo — voir le DOI indiqué en tête du PDF.
