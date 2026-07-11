# Catalog Photo Control

Pipeline locale et reproductible de génération, benchmark qualité et sélection
de variants photo complets pour une annonce. Une recette canonique unique est
appliquée à toutes les images d’un variant ; un échec d’image invalide le variant
entier.

## Installation

Sous PowerShell :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_venv.ps1
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

Les sources d’annonces restent externes et sont uniquement lues. Toutes les
sorties sont placées sous `local/`, ignoré par Git.

## Benchmark unique

Avec un chemin d’annonce direct :

```powershell
python -m common.catalog_photo_control.bench `
  --listing "C:\catalogue\bijoux\O\O18" `
  --target-variants 50 `
  --max-tests 20000 `
  --max-duration-minutes 180 `
  --patience 3000
```

Avec une racine externe et un code relatif :

```powershell
python -m common.catalog_photo_control.bench `
  --source-root "C:\catalogue" `
  --listing "bijoux/O/O18" `
  --target-variants 50
```

Chaque invocation crée un run distinct, mais réutilise automatiquement les tests
déjà présents pour la même empreinte ordonnée des sources. Les variants finaux
existants comptent dans `--target-variants`. Les limites positives
`--max-tests`, `--max-duration-minutes` et `--patience` garantissent l’arrêt.

La répartition des propositions vient de `config/filter_space.json` (50 %
exploration, 30 % recettes éprouvées, 20 % mutations par défaut). Elle peut être
surchargée avec les trois options `--random-share`, `--proven-share` et
`--mutation-share`, dont la somme doit être 1.

La sélection attend par défaut un pool de trois candidats éligibles par place
restante avant d’appliquer le max-min. Ce multiplicateur est configurable via
`selection_pool_multiplier` dans le même fichier. Si une limite d’arrêt survient
avant, le pool valide disponible est tout de même sélectionné.

## Sorties

```text
local/
├── databases/
│   ├── catalog_bench.sqlite3
│   └── catalog_variants.sqlite3
├── bench_work/
└── bench_runs/<listing>/<run_id>/
    ├── index.html
    └── selected_variants/variant_0001/...
```

Un run produit exactement un fichier HTML : `index.html`. Il affiche le motif
d’arrêt, les compteurs, toutes les images de chaque variant dans l’ordre source,
la recette, les métriques et les champs réservés de contenu/métadonnées.

Les deux bases vides peuvent aussi être initialisées sans lancer de benchmark :

```powershell
python -m common.catalog_photo_control.bench_db --local-root local
```

Le détail des tables et invariants se trouve dans
[`docs/SCHEMA.md`](docs/SCHEMA.md). Le seul espace de filtres est
[`config/filter_space.json`](config/filter_space.json) ; il n’existe aucun profil
fixe.

## Confidentialité des métadonnées

Le refactor ne contient pas de pipeline de métadonnées. L’utilitaire autonome
conservé crée des copies sans métadonnées de deux images sans modifier les
sources :

```powershell
python .\metadata_privacy_two_images.py image1.jpg image2.jpg `
  --output-dir local\metadata_copies
```

## Nettoyage

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\clean_generated_artifacts.ps1 -WhatIfOnly
powershell -ExecutionPolicy Bypass -File .\scripts\clean_generated_artifacts.ps1
```
