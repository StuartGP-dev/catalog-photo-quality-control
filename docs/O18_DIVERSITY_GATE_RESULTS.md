# O18 perceptual consensus calibration

Calibration date: 2026-07-15. O18 sources were read only. The generated report
is ignored under `local/perceptual_calibration/O18/index.html`.

The observed full distributions over 55 pairs were:

| hash | minimum | median | maximum |
|---|---:|---:|---:|
| pHash | 0 | 4 | 32 |
| dHash | 0 | 4 | 37 |
| wHash | 0 | 4 | 32 |

Identical files produced `(0,0,0) / exact`. JPEG recompression produced
`(0,0..1,0) / same`; light brightness changes `(0,0..2,0..2) / same`; crop and
zoom mostly `(2..4,2..4,2..4) / same`. Rotation ranged from `same` to
`near_duplicate`; offsets were `near_duplicate`. Dezoom ranged from
`near_duplicate` to `different`, with the boundary example `(6,13,18)`.
Clearly altered images were pHash 28..32 and dHash 27..37, all `different`.
Other O18 views and the sampled other product were all `different`.

The retained corpus-specific bands are strong <= 4, review <= 10, then weak
<= 16 for pHash/dHash and <= 18 for wHash. These are initial O18 calibration
values, not universal perceptual thresholds.
