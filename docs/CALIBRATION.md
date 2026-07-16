# Calibration visuelle géométrique

La calibration est une voie d’inspection séparée du benchmark de production.
Elle explore des transformations géométriques plus larges sur toutes les images
d’une annonce, sans écrire dans `catalog_variants.sqlite3` et sans modifier les
sources.

Le rapport possède une revue humaine hors ligne. Chaque recette peut être
acceptée, refusée ou marquée « à revoir », avec une note facultative. Les choix
sont conservés dans le stockage local du navigateur et le bouton
`Sauvegarder mes choix` télécharge immédiatement un fichier JSON autonome à transmettre pour la
calibration ultérieure des limites. Ce fichier n'est jamais appliqué
automatiquement aux seuils de production.

La comparaison principale ne mélange plus les deux images : l'originale
complète est affichée à gauche et la photo filtrée complète à droite.

L'enveloppe exploratoire couvre volontairement jusqu'à ±8° de rotation, 12 %
de crop, zoom 1,20, dézoom 20 % et offsets ±12 %. Une classification automatique
rouge ne masque pas le rendu : elle aide seulement la revue humaine.

La campagne de revue v3 repart avec un stockage de décisions vide. Une recette
n'est proposée à la revue que si ses cinq images sont `different` de leurs
originales et si la distance limitante `pHash + dHash + wHash` est au moins
40 sur 192. Ce seuil interne conservateur a gardé 21 des 302 recettes O18 ; il
ne représente le seuil d'aucune plateforme externe.

```powershell
python -m common.catalog_photo_control.calibrate `
  --listing "C:\catalogue\bijoux\O\O18" `
  --families rotation,crop,zoom,dezoom,offset,geometry-combinations `
  --output-root "local/calibration_runs" `
  --coarse-steps 6 `
  --bisection-steps 4
```

Chaque paramètre non étudié conserve sa valeur neutre. Une famille est d’abord
échantillonnée sur des paliers fixes, puis les intervalles entre
`very_subtle` et le premier candidat perceptible sont affinés de façon
déterministe. Rotation et offsets couvrent les deux directions. Chaque dézoom
utilise obligatoirement l’un des canevas clairs ou échantillonnés pris en charge.
Les combinaisons restent bornées à quatre paramètres actifs.

Les artefacts sont placés sous
`local/calibration_runs/<listing>/<source-hash>-<config-hash>/` :

- `index.html` est l’unique rapport utilisateur ;
- `manifest.json` porte les hashes, les compteurs et le résumé par famille ;
- `calibration_results.json` conserve les métriques exhaustives par exemple et
  par image ;
- `examples/` contient les variantes et outils d’inspection.

Une seconde exécution avec les mêmes sources et la même configuration réutilise
le run existant. `--force` le reconstruit. Aucune base du catalogue final n’est
ouverte par cette commande.

## Lecture du rapport

Le rapport affiche les cinq images dans l’ordre source avec comparaison
côte-à-côte/superposée, alternance, curseur avant/après, zoom 100 %, carte de
différence amplifiée, crops central et de bords et boîte du contenu. Pour les
canevas, il donne la couleur détectée, la couleur réellement utilisée, son
origine, la confiance, le fallback, la fraction de canevas et l’échelle du
premier plan.

Les classes automatiques sont des aides à la revue :

- `very_subtle` : sous le début géométrique estimé de la zone perceptible ;
- `perceptible_candidate` : changement mesurable dans la zone à inspecter ;
- `strong_candidate` : changement fort encore techniquement valide ;
- `rejected` : au moins une barrière exploratoire ou de conservation échoue.

Les barrières exploratoires sont SSIM ≥ 0,94, pixel MAE ≤ 0,04, luminance MAE
≤ 0,035, ratio de netteté entre 0,7 et 1,6 et clipping ≤ 0,02. Elles ne sont pas
des seuils de production. Aucune métrique ne prouve seule qu’un changement est
perçu ou naturel : la décision finale exige l’inspection visuelle de toutes les
images réelles.

## Calibration O18 du 13 juillet 2026

Les 192 exemples (960 rendus) ont été comparés sur les cinq images sources.
Le début de la zone perceptible et naturelle a été observé vers 0,6° pour la
rotation, 0,6 % pour le crop, 1,006 pour le zoom, 1,5 % de dézoom et 0,5 %
pour les offsets. Les maxima prudents retenus sont respectivement 1,2°, 1,5 %,
1,02, 3,5 % et 1,2 %. Les combinaisons inspectées à ces niveaux conservent le
produit entier et des proportions naturelles.

| Paramètre | Ancienne plage active | Nouvelle plage active |
| --- | --- | --- |
| Rotation | environ ±0,18–1,2° | ±0,6–1,2° |
| Crop | 0,3–2 % | 0,6–1,5 % |
| Zoom | 1,001–1,025 | 1,006–1,02 |
| Dézoom | 0,5–3,5 % | 1,5–3,5 % |
| Offset horizontal/vertical | proche de 0–2 % | ±0,5–1,2 % |

Le SSIM direct reste stocké et affiché, mais il chute fortement avec un simple
décalage ou une rotation sous le degré. Pour la seule barrière de fidélité des
recettes géométriques, la production utilise donc aussi un SSIM déterministe
sur luminance réduite à 64 × 64 et légèrement lissée, avec un minimum de 0,97.
Ce garde-fou multi-échelle ne remplace ni les MAE, le clipping, la netteté, ni
la revue visuelle. Les recettes d’apparence conservent le SSIM direct minimum
de 0,97.
