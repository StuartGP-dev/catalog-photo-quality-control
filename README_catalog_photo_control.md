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
