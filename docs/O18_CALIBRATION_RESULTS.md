# Résultats de calibration O18 — 13 juillet 2026

La calibration réelle a évalué 192 recettes, soit 960 rendus sur les cinq
images de O18. Le rapport local est
`local/calibration_runs/O18/fd5c46392c1d-78a18c300219/index.html`.
Les métriques ont servi à proposer les candidats ; les seuils ci-dessous ont
été retenus après comparaison visuelle des cinq images, et non comme preuve
automatique de perception humaine.

| Famille | Exemples | Quasi imperceptible | Début perceptible estimé | Maximum prudent |
| --- | ---: | --- | --- | --- |
| Rotation | 12 | 0,25° | 0,6° | 1,2° |
| Crop | 10 | 0,3–0,45 % | 0,6 % | 1,5 % |
| Zoom | 6 | 1,003 | 1,006 | 1,02 |
| Dézoom + canevas | 56 | 0,75 % | 1–1,5 % | 3,5 % |
| Offset horizontal/vertical | 24 | 0,3 % | 0,5–0,6 % | 1,2 % |
| Rotation + crop compensé | 12 | sous 0,6° + 0,6 % | 0,6° + 0,6 % | 1° + 1,2 % |
| Rotation + zoom | 12 | sous 0,6° + 1,006 | 0,6° + 1,006 | 1° + 1,015 |
| Zoom + offset | 24 | sous 1,006 + 0,5 % | 1,01 + 0,5 % | 1,015 + 1 % |
| Rotation + dézoom + canevas | 12 | sous 0,6° + 1,5 % | 0,6° + 2 % | 1° + 3 % |
| Crop + offset | 24 | sous 0,6 % + 0,5 % | 0,6 % + 0,5 % | 1,2 % + 0,8 % |

Les contrôles automatiques directs ont laissé passer respectivement 16, 33,
13, 252 et 4 images pour rotation, crop, zoom, dézoom et offset. Leur faible
taux sur les transformations déplacées provient principalement du SSIM direct,
très sensible à l’alignement pixel. La barrière de production géométrique
multi-échelle conserve un seuil de 0,97 tout en gardant SSIM direct, MAE,
netteté et clipping visibles et actifs.

## Configuration retenue

| Paramètre | Avant | Après |
| --- | --- | --- |
| Rotation | environ ±0,18–1,2° | ±0,6–1,2° |
| Crop | 0,3–2 % | 0,6–1,5 % |
| Zoom | 1,001–1,025 | 1,006–1,02 |
| Dézoom | 0,5–3,5 % | 1,5–3,5 % |
| Offset X/Y | proche de 0–2 % | ±0,5–1,2 % |
| Probabilité des modèles géométriques | 0,52 | 0,68 |

Les probabilités d’activation géométriques ont été relevées et les
compatibilités resserrées pour les combinaisons fortes. Les plages d’apparence
(luminance, saturation, chaleur et teinte) n’ont pas été renforcées.

## Benchmark final

Le run `20260713T163018-bcb56fe1` a duré 1 209,4 secondes, testé 652
propositions (208 réutilisations de cache) et atteint 150 variantes. Toutes
sont `ready`, contiennent exactement cinq images et existent sur disque. Le run
contient exactement un fichier `index.html`.

| Distribution sélectionnée | Ancien benchmark | Nouveau benchmark |
| --- | --- | --- |
| SSIM direct minimum (min/moy/max) | 0,9712 / 0,9892 / 0,9995 | 0,7667 / 0,9279 / 0,9992 |
| Pixel MAE moyen | 0,0035 / 0,0092 / 0,0184 | 0,0038 / 0,0156 / 0,0279 |
| Luminance MAE moyenne | 0,0034 / 0,0090 / 0,0182 | 0,0037 / 0,0154 / 0,0278 |
| Rotation absolue | 0 / 0,033° / 0,263° | 0 / 0,156° / 0,991° |
| Crop | 0 / 0,040 % / 0,456 % | 0 / 0,281 % / 1,152 % |
| Zoom avant | 0 / 0,067 % / 0,408 % | 0 / 0,194 % / 1,242 % |
| Dézoom | 0 / 0,362 % / 2,461 % | 0 / 0,415 % / 2,835 % |
| Offset absolu | 0 / 0,0002 % / 0,014 % | 0 / 0,051 % / 0,657 % |
| Distance min inter-variantes | 0,00030 / 0,01182 / 0,16569 | 0,00053 / 0,01559 / 0,17244 |

La répartition finale est : 28 apparence, 27 crop, 29 dézoom, 8 géométrie
mixte, 1 offset seul, 28 rotation et 29 zoom. En comptant les combinaisons, 36
variantes utilisent une rotation, 54 un crop, 34 un zoom, 29 un dézoom et 14
un offset. Les 235 images avec canevas ne présentent aucun fond sombre ou
fortement saturé selon les diagnostics, ce que confirme l’inspection visuelle.

## Intégrité des sources

- `0.jpg` : `0d139ae409734224121b757f53fea6a36b7302cdafe33b5c0a41add008c62aeb`
- `1.jpg` : `7bd0e9ed8275d0c81081ab34252dfbc3edcc0313fbbc949e6f0a0458e32c06f2`
- `2.JPEG` : `fdd42d20c6029c8bd79159af22b8e084d5851392654a9d12eacc02660bc9bf76`
- `3.jpg` : `11e3a69d45d6077532d169f66fcda7ad834a8a30bb294daf1e33b68201499e2b`
- `4.jpg` : `c455f64e21f769032739e0425d2a66667bb6c13963bf08bca9db7fc77c0c3366`

Ces hashes sont identiques avant calibration, avant purge et après benchmark.
