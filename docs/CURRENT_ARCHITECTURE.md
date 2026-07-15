# Current architecture

The supported pipeline is `python -m common.catalog_photo_control.bench`.
It loads an ordered listing, generates one canonical recipe per complete
variant, renders atomically, checks fidelity, applies the per-index perceptual
barrier, then performs max-min selection among valid candidates only.

`image_similarity.py` is the sole source of perceptual identity decisions. It
computes SHA-256 and EXIF-normalized pHash/dHash/wHash 64-bit values, raw Hamming
distances, calibrated bands, consensus verdicts, and readable reasons.
`diversity_gate.py` first compares each output with its exact source, then with
every ready output from the same listing, source version, and image index. It
deduplicates ready hashes and rejects the complete candidate on one failing
image. Quality-only SSIM, MAE, sharpness, clipping, geometry, and canvas
diagnostics remain in `metrics.py` and `quality.py`; they never decide image
identity.

`bench_db.py` owns append-oriented recipe history and explicit perceptual pair
records. `variants_db.py` owns complete ready variants only. `selector.py`
rechecks the barrier immediately before its atomic final insert. `html_report.py`
is the single benchmark report writer. `perceptual_calibration.py` creates a
portable one-file HTML report plus local assets without modifying source images.

The removed `visual_distance.py`, `diversity_analysis.py`, and
`audit_diversity.py` implemented the replaced weighted-score decision and
report path. Repository tests assert that no second perceptual verdict engine
can return unnoticed.
