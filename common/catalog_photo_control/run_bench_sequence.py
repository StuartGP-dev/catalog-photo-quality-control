from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


FOCUS_PARAMS = "angle,crop,blur,quality,zoom,canvas_pad,canvas_gray,canvas_auto"


def _safe_listing(listing: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", listing).strip("_") or "listing"


def _run(cmd: list[str], log_path: Path) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print("")
    print("RUN:", " ".join(cmd))
    print("LOG:", log_path)

    captured: list[str] = []

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
            captured.append(line)

        code = proc.wait()

    if code != 0:
        raise RuntimeError(f"Commande echouee avec code {code}: {' '.join(cmd)}")

    return "".join(captured)


def _extract_report_json(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Rapport JSON:"):
            return Path(line.split(":", 1)[1].strip())
    raise RuntimeError("Rapport JSON introuvable dans la sortie")


def _extract_cluster_json(report_json: Path) -> Path:
    candidate = report_json.parent / "filter_clusters" / "filter_clusters.json"
    if not candidate.exists():
        raise RuntimeError(f"filter_clusters.json introuvable: {candidate}")
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lance une sequence de benchs avec DB partagee et selection finale diversifiee.")
    parser.add_argument("--listing", required=True)
    parser.add_argument("--annonce-key", default=None, help="Cle annonce en DB. Par defaut: meme valeur que --listing.")
    parser.add_argument("--profile", default="client_wide")
    parser.add_argument("--minutes-stage1", type=float, default=45.0)
    parser.add_argument("--minutes-stage2", type=float, default=45.0)
    parser.add_argument("--reset-client-render-db", action="store_true", help="Reset seulement avant le stage 1. Le stage 2 reutilise toujours la DB.")
    parser.add_argument("--review-threshold", type=float, default=0.48)
    parser.add_argument("--suspect-threshold", type=float, default=0.76)
    parser.add_argument("--min-score", type=float, default=0.48)
    parser.add_argument("--min-distance", type=float, default=0.12, help="Seuil combine pour la selection finale diverse.")
    parser.add_argument("--min-param-distance", type=float, default=0.07)
    parser.add_argument("--min-image-distance", type=float, default=0.035)
    parser.add_argument("--max-per-family", type=int, default=4)
    parser.add_argument("--max-filters", type=int, default=0, help="0 = maximum sans limite")
    parser.add_argument("--report-row-limit", type=int, default=50000)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--stage1-min-fraction", type=float, default=0.62)
    parser.add_argument("--stage2-min-fraction", type=float, default=0.45)
    parser.add_argument("--run-label", default=None)
    parser.add_argument("--write-catalog-db", action="store_true", help="Importe la selection finale diverse dans la DB partagee.")
    args = parser.parse_args(argv)

    run_id = args.run_label or datetime.now().strftime("bench_sequence_%Y%m%d_%H%M%S")
    annonce_key = args.annonce_key or args.listing
    seq_dir = Path("local") / "debug_catalog_photo_control" / "bench_sequences" / _safe_listing(args.listing) / run_id
    seq_dir.mkdir(parents=True, exist_ok=True)

    base_cmd = [
        sys.executable,
        "-m",
        "common.catalog_photo_control.client_render_sampler",
        "--listing",
        args.listing,
        "--profile",
        args.profile,
        "--bench-evaluator",
        "local_delta",
        "--bench-evaluator-param",
        f"focus_params={FOCUS_PARAMS}",
        "--bench-evaluator-param",
        f"review_threshold={args.review_threshold}",
        "--bench-evaluator-param",
        f"suspect_threshold={args.suspect_threshold}",
        "--report-row-limit",
        str(args.report_row_limit),
        "--no-bench-summary",
        "--progress-every",
        str(args.progress_every),
    ]

    # Stage 1 : exploration symetrique forte.
    stage1 = base_cmd + [
        "--duration-minutes",
        str(args.minutes_stage1),
        "--search-strategy",
        "symmetric_target_hunt",
        "--strategy-param",
        "levels=31",
        "--strategy-param",
        f"min_fraction={args.stage1_min_fraction}",
        "--strategy-param",
        "max_pairwise=2200",
        "--strategy-param",
        "max_plan=300000",
        "--strategy-param",
        "jitter_ratio=0.0022",
        "--strategy-param",
        f"focus_params={FOCUS_PARAMS}",
    ]

    if args.reset_client_render_db:
        stage1.append("--reset-client-render-db")

    stage1_out = _run(stage1, seq_dir / "stage1_symmetric_target_hunt.log")
    stage1_report = _extract_report_json(stage1_out)

    # Clustering sur le stage 1.
    cluster_cmd = [
        sys.executable,
        "-m",
        "common.catalog_photo_control.filter_cluster_builder",
        "--source-report",
        str(stage1_report),
        "--min-score",
        str(args.min_score),
        "--cluster-threshold",
        "0.090",
        "--param-weight",
        "0.45",
        "--image-weight",
        "0.55",
        "--family-key-mode",
        "strict",
        "--reset-cluster-db",
    ]
    _run(cluster_cmd, seq_dir / "stage1_filter_clusters.log")
    clusters_json = _extract_cluster_json(stage1_report)

    # Stage 2 : exploration autour des clusters du stage 1.
    # Important : pas de reset DB ici.
    stage2 = base_cmd + [
        "--duration-minutes",
        str(args.minutes_stage2),
        "--search-strategy",
        "cluster_aware_hunt",
        "--strategy-param",
        f"clusters_json={clusters_json}",
        "--strategy-param",
        "cluster_source=top",
        "--strategy-param",
        f"min_fraction={args.stage2_min_fraction}",
        "--strategy-param",
        "pool_size=30000",
        "--strategy-param",
        "max_plan=300000",
        "--strategy-param",
        "min_plan_distance=0.014",
        "--strategy-param",
        "jitter_ratio=0.0025",
        "--strategy-param",
        f"focus_params={FOCUS_PARAMS}",
    ]

    stage2_out = _run(stage2, seq_dir / "stage2_cluster_aware_hunt.log")
    stage2_report = _extract_report_json(stage2_out)

    # Clustering stage 2.
    cluster2_cmd = [
        sys.executable,
        "-m",
        "common.catalog_photo_control.filter_cluster_builder",
        "--source-report",
        str(stage2_report),
        "--min-score",
        str(args.min_score),
        "--cluster-threshold",
        "0.090",
        "--param-weight",
        "0.50",
        "--image-weight",
        "0.50",
        "--family-key-mode",
        "strict",
        "--reset-cluster-db",
    ]
    _run(cluster2_cmd, seq_dir / "stage2_filter_clusters.log")

    # Selection finale : maximum de filtres cibles distants, sur stage 1 + stage 2.
    diverse_dir = seq_dir / "diverse_target_filters"
    diverse_json = diverse_dir / "diverse_target_filters.json"
    diverse_cmd = [
        sys.executable,
        "-m",
        "common.catalog_photo_control.diverse_target_selector",
        "--source-report",
        str(stage1_report),
        "--source-report",
        str(stage2_report),
        "--output-dir",
        str(diverse_dir),
        "--min-score",
        str(args.min_score),
        "--min-distance",
        str(args.min_distance),
        "--min-param-distance",
        str(args.min_param_distance),
        "--min-image-distance",
        str(args.min_image_distance),
        "--max-per-family",
        str(args.max_per_family),
        "--max-filters",
        str(args.max_filters),
    ]
    _run(diverse_cmd, seq_dir / "diverse_target_selector.log")

    if args.write_catalog_db:
        import_cmd = [
            sys.executable,
            "-m",
            "common.catalog_photo_control.import_diverse_filters_to_db",
            "--annonce-key",
            annonce_key,
            "--diverse-json",
            str(diverse_json),
            "--source-run-label",
            run_id,
        ]
        _run(import_cmd, seq_dir / "import_catalog_db.log")

    print("")
    print("BENCH SEQUENCE DONE")
    print(f"sequence_dir: {seq_dir}")
    print(f"stage1_report: {stage1_report}")
    print(f"stage2_report: {stage2_report}")
    print(f"clusters_stage1: {clusters_json}")
    print(f"diverse_html: file:///{(diverse_dir / 'diverse_target_filters.html').as_posix()}")
    print(f"diverse_csv: file:///{(diverse_dir / 'diverse_target_filters.csv').as_posix()}")
    print(f"diverse_json: file:///{diverse_json.as_posix()}")
    if args.write_catalog_db:
        print(f"catalog_db_import: {seq_dir / 'import_catalog_db.log'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
