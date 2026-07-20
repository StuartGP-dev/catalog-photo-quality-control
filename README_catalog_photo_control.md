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

Pour appliquer aux seules copies sélectionnées le profil ICC et la résolution
d'une référence, sans copier ni fabriquer de données de capture, ajouter :

```powershell
  --metadata-reference "C:\Users\yanis\Downloads\IMG_3206.jpg"
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

## Calibration visuelle

La commande séparée de calibration explore des paliers géométriques plus larges
sans alimenter la base finale des variants :

```powershell
python -m common.catalog_photo_control.calibrate `
  --listing "C:\catalogue\bijoux\O\O18" `
  --families rotation,crop,zoom,dezoom,offset,geometry-combinations `
  --output-root "local/calibration_runs" `
  --coarse-steps 6 `
  --bisection-steps 4
```

Elle produit un seul `index.html`, accompagné de données JSON et d’outils
d’inspection sous `local/calibration_runs`. Les métriques ne sont que des aides
au choix : la validation visuelle de toutes les images reste obligatoire. Voir
[`docs/CALIBRATION.md`](docs/CALIBRATION.md).

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

## Enveloppe de fidélité

L'espace livré limite chaque recette à quatre paramètres non neutres et à une
intensité maximale de `1.2`. L'intensité est la somme déterministe, pour chaque
paramètre actif hors encodage, de `abs(value-default) / max(abs(min-default),
abs(max-default))`. `jpeg_quality` et les valeurs neutres ne sont pas comptés.

Chaque image doit respecter : SSIM ≥ `0.90`, pixel MAE ≤ `0.06`, luminance MAE
≤ `0.06`, ratio de netteté entre `0.70` et `1.60`, fraction écrêtée ≤ `0.08`.
Une seule image hors enveloppe rejette le variant complet. Le hash de cache
inclut l'intégralité de `filter_space.json` et la version des métriques.

Les modes de canevas subtils sont `none`, `white`, `light_gray`,
`sampled_background`, `sampled_edge`, `side_bands` et `uniform_frame`. Une
signature déterministe de quelques pixels, dérivée du hash de recette et de
l'index source, garantit que chaque sortie diffère des dimensions originales.
La base finale refuse également une signature de dimensions déjà sélectionnée
pour la même annonce et la même version source.

## Purge des résultats générés

```powershell
# Une annonce, chemin direct
python -m common.catalog_photo_control.purge --listing "C:\catalogue\bijoux\O\O18"

# Une annonce, racine et clé relative
python -m common.catalog_photo_control.purge --source-root "C:\catalogue" --listing "bijoux/O/O18"

# Prévisualisation ou version source courante seulement
python -m common.catalog_photo_control.purge --listing "C:\catalogue\bijoux\O\O18" --dry-run
python -m common.catalog_photo_control.purge --listing "C:\catalogue\bijoux\O\O18" --current-source-only

# Toutes les données gérées, avec réinitialisation des schémas
python -m common.catalog_photo_control.purge --all --yes --reinitialize
```

La purge ne touche jamais aux sources ni aux autres fichiers de `local/`. Pour
une annonce, la base variants est attachée à la connexion benchmark avec SQLite
`ATTACH DATABASE` et les deux ensembles de lignes sont validés dans une seule
transaction. Les dossiers collectés avant transaction ne sont supprimés
qu'après le commit ; un échec DB conserve donc tous les fichiers.

## Confidentialité des métadonnées

Le benchmark peut appliquer des métadonnées techniques aux copies sélectionnées.
La commande autonome équivalente n'écrase jamais son entrée :

```powershell
python -m common.catalog_photo_control.apply_metadata `
  --input "C:\chemin\image-entree.jpg" `
  --reference "C:\Users\yanis\Downloads\IMG_3206.jpg" `
  --output "C:\chemin\image-sortie.jpg"
```

Seuls ICC et résolution JFIF sont repris. Les données EXIF/XMP de capture sont
omises. Pour indexer les métadonnées factuelles de chaque image d'un variant :

```powershell
python -m common.catalog_photo_control.image_metadata `
  --database local\databases\catalog_variants.sqlite3 `
  --variant-id 42
```

Sur une base existante, `--metadata-reference` au lancement du benchmark migre
et traite automatiquement les variants `ready` encore non indexés. Le même
backfill peut être lancé sans benchmark :

```powershell
python -m common.catalog_photo_control.image_metadata `
  --database local\databases\catalog_variants.sqlite3 `
  --reference "C:\Users\yanis\Downloads\IMG_3206.jpg"
```

L'utilitaire de confidentialité racine reste disponible et ne modifie pas les
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
# Barrière de similarité perceptuelle

Le benchmark contrôle séparément la fidélité à la source et la similarité aux
variantes finales. Chaque sortie candidate est comparée uniquement aux images
`ready` du même `image_index`. SHA-256 détecte l'identité stricte ; pHash,
dHash et wHash 64 bits fournissent un verdict par consensus. Un seul verdict
`exact`, `same` ou `near_duplicate` rejette atomiquement le variant complet
avant toute sélection max-min.

Calibration courte en lecture seule des sources :

```powershell
python -m common.catalog_photo_control.perceptual_calibration `
  --listing "C:\Users\yanis\Documents\Code\Bot-Vinted\annonces\bijoux\O\O18" `
  --output "local\perceptual_calibration\O18"
```
