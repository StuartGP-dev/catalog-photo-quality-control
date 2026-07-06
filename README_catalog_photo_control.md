# Catalog photo quality control

Ce paquet utilise un nommage neutre et oriente controle qualite des photos catalogue.

## Modules principaux

- `common.catalog_photo_control.photo_quality_control`
- `common.catalog_photo_control.benchmarks`
- `common.catalog_photo_control.decision_margin_search`
- `common.catalog_photo_control.photo_comparison_rules`
- `common.catalog_photo_control.photo_adjustments`
- `common.catalog_photo_control.photo_metadata`
- `common.catalog_photo_control.listing_photo_review`


## Chemins locaux par defaut

Par defaut, le code lit les annonces depuis le projet Bot-Vinted existant :

```text
C:\Users\yanis\Documents\Code\Bot-Vinted\annonces
```

Tous les fichiers generes par ce repo restent dans le repo `catalog-photo-quality-control`, sous :

```text
<repo>\local\debug_catalog_photo_control
```

Cela inclut les catalogues JSON locaux, les rapports Markdown/JSON, les images de controle generees et les ZIP debug. Le dossier `local/` est ignore par Git pour eviter de publier les sorties de benchmark.

Tu peux surcharger les chemins sans modifier le code avec :

```powershell
$env:CATALOG_PHOTO_ANNONCES_ROOT = "C:\Users\yanis\Documents\Code\Bot-Vinted\annonces"
$env:CATALOG_PHOTO_OUTPUT_ROOT = "C:\Users\yanis\Documents\Code\catalog-photo-quality-control\local\debug_catalog_photo_control"
```


## Resolution des dossiers annonce

La valeur passee a `--listing` reste courte et lisible. Par exemple :

```text
bijoux/O18
```

Le code teste d'abord le chemin direct :

```text
C:\Users\yanis\Documents\Code\Bot-Vinted\annonces\bijoux\O18
```

Puis il teste automatiquement le rangement par famille de reference :

```text
C:\Users\yanis\Documents\Code\Bot-Vinted\annonces\bijoux\O\O18
```

Donc meme si le premier `Test-Path` retourne `False`, la commande `--listing bijoux/O18` fonctionne tant que le dossier range dans `bijoux\O\O18` existe.

Verification rapide :

```powershell
Test-Path "C:\Users\yanis\Documents\Code\Bot-Vinted\annonces\bijoux\O\O18"
```

## Commande exemple - controle standard

```powershell
python -m common.catalog_photo_control.photo_quality_control --listing bijoux/O18 --preset default --sensitivity standard --max-other-listings 20
```

Les rapports utilisent les statuts `match/review/clear` pour garder une lecture orientee controle qualite.

## Commande exemple - analyse des marges de decision photo

```powershell
python -m common.catalog_photo_control.benchmarks --listing bijoux/O18 --preset decision_margin_search --policy default --decision-margin-seed 12345 --decision-margin-max-combinations 24
```

Ce mode ajoute une section `Analyse des marges de decision photo` dans les rapports JSON et Markdown. Il teste progressivement plusieurs familles d'ajustements photo plausibles, encadre les zones de transition par dichotomie, puis enregistre les points de transition dans un catalogue separe sous `local/debug_catalog_photo_control/_decision_margin_catalog/`.

Options utiles :

- `--decision-margin-seed` : rend les combinaisons legeres reproductibles.
- `--decision-margin-max-combinations` : limite le nombre de variations photo realistes combinees.
- `--decision-margin-candidates` : limite le nombre d'images sources priorisees depuis les resultats deja produits.
- `--decision-margin-iterations` : controle le nombre d'iterations de dichotomie autour d'une zone de transition.

## Commande avec chemins explicites

```powershell
python -m common.catalog_photo_control.benchmarks --listing bijoux/O18 --preset decision_margin_search --policy default --annonces-root "C:\Users\yanis\Documents\Code\Bot-Vinted\annonces"
```

Sans `--output-root`, la DB locale, les rapports et les ZIP debug restent automatiquement dans le dossier `local/debug_catalog_photo_control` de ce repo.


## Commande exemple - variantes de rendu client

```powershell
python -m common.catalog_photo_control.client_render_sampler --listing bijoux/O18 --profile client_wide --samples 40 --seed 12345
```

Ce mode genere plusieurs recettes de retouche naturelles pour une revue client: luminosite, contraste, saturation, nettete, chaleur, rotation legere, recadrage leger, flou leger et qualite JPEG.

Sorties generees dans `local/debug_catalog_photo_control`:

- images finales par recette dans `rendered/`
- rapport JSON
- rapport CSV
- rapport HTML
- planche avant/apres
- base SQLite locale `_client_render_sampler` pour ne pas rejouer exactement les memes recettes deja generees pour la meme annonce et le meme profil

Profils disponibles:

- `natural`: retouches legeres et prudentes
- `client_wide`: plages un peu plus larges tout en gardant un rendu naturel

