# Catalog photo quality control

Ce paquet utilise un nommage neutre et oriente controle qualite des photos catalogue.

## Objectif actuel

Construire une pipeline de filtres par annonce :

1. scanner les annonces locales ;
2. tester beaucoup de recettes de rendu ;
3. stocker les resultats dans une DB partagee ;
4. clusteriser les filtres par annonce ;
5. selectionner progressivement des filtres differents entre eux ;
6. exporter une annonce complete avec le meme filtre applique a toutes ses images.

Une annonce contient plusieurs images. Une recette de filtre doit donc etre evaluee sur toute l'annonce, avec des stats par image puis une agregation annonce : moyenne, minimum, maximum et stabilite.

## Modules principaux

- `common.catalog_photo_control.client_render_sampler`
- `common.catalog_photo_control.filter_cluster_builder`
- `common.catalog_photo_control.diverse_target_selector`
- `common.catalog_photo_control.catalog_config`
- `common.catalog_photo_control.catalog_db`
- `common.catalog_photo_control.init_catalog_db`
- `common.catalog_photo_control.ingest_annonces`

## Chemins locaux par defaut

Par defaut, le code lit les annonces depuis :

```text
C:\Users\yanis\Documents\Code\Bot\annonces
```

Les sorties generees restent dans :

```text
<repo>\local\debug_catalog_photo_control
<repo>\local\catalog_filter_engine
```

Le dossier `local/` est ignore par Git pour eviter de publier les sorties de benchmark, les DB SQLite locales et les images generees.

## Environnement Python

Creation du venv Windows :

```powershell
cd C:\Users\yanis\Documents\Code\Catalog-Photo-Control
powershell -ExecutionPolicy Bypass -File .\scripts\setup_venv.ps1
.\.venv\Scripts\Activate.ps1
```

## DB partagee multi-PC

La DB principale doit etre PostgreSQL, pas SQLite.

Demarrage local via Docker sur le PC qui heberge la DB :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_shared_db.ps1 -Password "CHANGE_ME_STRONG"
$env:CATALOG_DB_DSN = "postgresql://catalog_user:CHANGE_ME_STRONG@localhost:5432/catalog_filter_engine"
python -m common.catalog_photo_control.init_catalog_db --require-postgres
```

Depuis un autre PC via Tailscale, remplacer `localhost` par l'IP Tailscale du PC qui heberge Postgres :

```powershell
$env:CATALOG_DB_DSN = "postgresql://catalog_user:CHANGE_ME_STRONG@100.x.x.x:5432/catalog_filter_engine"
python -m common.catalog_photo_control.init_catalog_db --require-postgres
```

Fallback local SQLite seulement pour smoke tests isoles :

```powershell
$env:CATALOG_DB_DSN = "sqlite:///local/catalog_filter_engine/catalog_filters.sqlite3"
```

Les autres variables utiles :

```powershell
$env:CATALOG_PHOTO_ANNONCES_ROOT = "C:\Users\yanis\Documents\Code\Bot\annonces"
$env:CATALOG_PHOTO_OUTPUT_ROOT = "local\catalog_filter_engine"
```

## Ingestion annonces

Dry-run sans ecriture DB :

```powershell
python -m common.catalog_photo_control.ingest_annonces --dry-run --limit 5
```

Ecriture DB partagee :

```powershell
$env:CATALOG_DB_DSN = "postgresql://catalog_user:CHANGE_ME_STRONG@localhost:5432/catalog_filter_engine"
python -m common.catalog_photo_control.ingest_annonces --limit 5
```

Run complet :

```powershell
python -m common.catalog_photo_control.ingest_annonces
```

## Nettoyage local

Voir ce qui serait supprime :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\clean_generated_artifacts.ps1 -WhatIfOnly
```

Supprimer les artifacts locaux ignores par Git :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\clean_generated_artifacts.ps1 -IncludeArchives
```
