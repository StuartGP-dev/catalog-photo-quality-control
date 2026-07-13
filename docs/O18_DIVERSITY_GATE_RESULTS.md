# Calibration de la barrière de diversité O18 — 13 juillet 2026

La calibration a été exécutée **avant toute purge**, en lecture seule, sur les
150 variantes `ready` (750 images) de O18. Elle a comparé uniquement les images
de même index. Le rapport est généré sous
`local/diversity_calibration/O18/fd5c46392c1d-bcec4472ecbf/index.html`.

- 56 625 paires intra-annonce calculées ;
- 750 voisins minimaux, soit un par image rendue ;
- aucune référence inter-annonces disponible dans la base actuelle ;
- base SQLite inchangée pendant la calibration ;
- cinq hashes source identiques à ceux du précédent benchmark.

## Définition du score

La métrique `image-distance-v1` produit un score déterministe borné entre 0 et
1. Elle combine des signatures RGB 32 × 32 et six composantes normalisées :

| Composante | Poids général | Définition résumée |
| --- | ---: | --- |
| structure | 0,30 | distance issue du SSIM à 32 × 32 et 16 × 16 |
| luminance | 0,10 | MAE de luminance normalisée |
| couleur | 0,12 | MAE RGB normalisée |
| contours | 0,16 | différence des gradients horizontaux et verticaux |
| géométrie | 0,24 | déplacement du centre et changement d’échelle de la boîte de contenu |
| canevas | 0,08 | différence de fraction de marge et de couleur de bord |

Des pondérations spécifiques renforcent les composantes couleur/luminance pour
`appearance_only`, et géométrie/canevas pour les familles de dézoom et les
combinaisons. Ce score est une barrière interne calibrée ; il ne constitue pas
une preuve absolue de perception humaine.

## Distribution des voisins minimaux

| Statistique | Distance |
| --- | ---: |
| minimum | 0,000648 |
| p1 | 0,001113 |
| p5 | 0,001567 |
| p10 | 0,002222 |
| médiane | 0,005501 |
| moyenne | 0,007339 |
| p90 | 0,013445 |
| maximum | 0,042533 |

Les index 0 à 4 ont respectivement une moyenne de 0,006891, 0,009551,
0,007892, 0,005359 et 0,007001. L’index 3 est le plus souvent limitant dans
le benchmark historique.

## Inspection visuelle et seuils retenus

L’inspection côte à côte, par alternance et avec les cartes de différence du
rapport donne les zones suivantes :

- 0,003–0,005 : changement généralement quasi imperceptible ;
- autour de 0,010 : changement visible surtout en comparaison directe ;
- 0,012–0,015 : cadrage, échelle ou angle clairement perceptible, avec les
  exemples inspectés encore naturels ;
- au-delà de 0,03 : différence forte, à contrôler avec les barrières de
  fidélité existantes.

Le seuil intra-annonce général retenu est **0,012**. Les familles de dézoom et
de géométrie mixte utilisent respectivement **0,014** et **0,015** ;
`appearance_only` utilise **0,010**. Sur l’ancien ensemble dense, le seuil
général 0,012 aurait rejeté 145 variantes sur 150, avec 651 images sous le
seuil. Cela décrit la redondance de l’ancien ensemble et non le nombre maximal
de variantes qu’une exploration séquentielle peut construire.

Le seuil catalogue est fixé prudemment à **0,006**, avec des variantes par
famille entre 0,005 et 0,0075. Il est volontairement inférieur au seuil
intra-annonce. La base ne contenant aucune autre annonce, ce seuil n’a pas pu
être validé sur des paires inter-annonces réelles et devra être recalibré dès
qu’un corpus multi-annonces représentatif sera disponible.

## Réglage de l’espace de recherche

Les transformations géométriques sont davantage privilégiées : rotation
0,8–1,8°, crop 0,8–2 %, zoom 1,01–1,03, offsets 0,8–1,8 % et dézoom 2–5 % avec
canevas obligatoire. Les plages de luminosité, saturation, chaleur et teinte
n’ont pas été renforcées. Les contrôles de fidélité, de netteté, clipping,
proportions, fond et visibilité complète du produit restent indépendants et
continuent de s’appliquer avant la barrière de diversité.
