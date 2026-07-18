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

`listing_content.py` reads title, description and price from the listing's
read-only `config.json`, persists them in final variants and writes a portable
`listing.json` beside each complete image set. `metadata_diagnostic.py` is a
read-only two-image inspector producing JSON and HTML; it never edits metadata.

The production envelope permits at most four active parameters and normalized
recipe intensity 1.2. Every rendered image must keep SSIM at least 0.90, pixel
and luminance MAE at most 0.06, and sharpness ratio between 0.70 and 1.60.
Horizontal mirror is the special `mirror_only_family`: at most one light
appearance adjustment may accompany it, its intensity is capped at 0.6, and
the final database permits only one ready mirror per listing/source-set.

The removed `visual_distance.py`, `diversity_analysis.py`, and
`audit_diversity.py` implemented the replaced weighted-score decision and
report path. Repository tests assert that no second perceptual verdict engine
can return unnoticed.
