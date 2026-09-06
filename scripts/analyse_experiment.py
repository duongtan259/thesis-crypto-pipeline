"""
Batch-size experiment analysis — tests the batch-fill model of generator latency
against the captured data and writes the thesis figures.

Model. Events arrive at rate lambda. A buffer of size B flushes when the B-th event
arrives, so the j-th event of a batch waits for the remaining (B - j) inter-arrival
intervals. Averaging over j gives

    E[latency | B] = T + (B - 1) / (2 * lambda)

where T is the exchange-to-generator transit time. B=1 calibrates T and the capture
manifest supplies lambda. No parameter is fitted to the held-out B>1 outcomes.

Usage:
    python scripts/analyse_experiment.py                       # newest manifest
    python scripts/analyse_experiment.py --manifest <path> --out-dir ../docs/figures
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from measure_latency import parse_iso, percentile

# Colourblind-safe, and distinguishable when printed in greyscale.
COLOURS = ["#0F4C81", "#C1666B", "#48A9A6", "#E4B363"]
INK_ERR = "#3d4752"
STYLES = ["-", "--", "-.", ":"]
plt.rcParams.update(
    {
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.labelsize": 9,
        "figure.dpi": 200,
        "savefig.bbox": "tight",
        "axes.grid": True,
        "grid.alpha": 0.25,
        "grid.linewidth": 0.5,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
    }
)


def newest_manifest(results_dir: Path) -> Path:
    manifests = sorted(results_dir.glob("capture_manifest_*.json"))
    if not manifests:
        raise SystemExit(
            f"no capture manifest in {results_dir} — run capture_latency.py first"
        )
    return manifests[-1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _load_event_records(path: Path, source: str = "coinbase_ws") -> list[dict]:
    records = []
    with path.open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if source and record.get("source") != source:
                continue
            if record.get("event_id"):
                records.append(record)
    return records


def select_common_cohort(manifest: dict, results_dir: Path) -> dict[int, list[dict]]:
    """Return the same ordered, complete event cohort for every batch-size arm."""
    records_by_batch: dict[int, list[dict]] = {}
    for output in manifest["outputs"]:
        records = _load_event_records(results_dir / output["file"])
        partial = int(output.get("final_partial_flush", 0))
        if partial:
            records = records[:-partial]
        records_by_batch[int(output["batch_size"])] = records

    if not records_by_batch:
        return {}
    common_ids = set.intersection(
        *(
            {record["event_id"] for record in records}
            for records in records_by_batch.values()
        )
    )
    reference_batch = min(records_by_batch)
    ordered_ids = [
        record["event_id"]
        for record in records_by_batch[reference_batch]
        if record["event_id"] in common_ids
    ]
    result = {}
    for batch_size, records in records_by_batch.items():
        by_id = {record["event_id"]: record for record in records}
        result[batch_size] = [by_id[event_id] for event_id in ordered_ids]
    return result


def newey_west_mean_ci(values: np.ndarray, lag: int | None = None) -> dict:
    """Estimate a 95% mean interval with Bartlett-weighted HAC covariance."""
    sample = np.asarray(values, dtype=float)
    n = len(sample)
    if n == 0:
        return {"count": 0, "lag": 0, "mean": None, "se": None, "ci95": [None, None]}
    chosen_lag = min(
        n - 1,
        lag if lag is not None else int(np.floor(4 * (n / 100) ** (2 / 9))),
    )
    mean = float(np.mean(sample))
    centred = sample - mean
    long_run_variance = float(np.dot(centred, centred) / n)
    for offset in range(1, chosen_lag + 1):
        weight = 1 - offset / (chosen_lag + 1)
        covariance = float(np.dot(centred[offset:], centred[:-offset]) / n)
        long_run_variance += 2 * weight * covariance
    se = float(np.sqrt(max(long_run_variance, 0.0) / n))
    iid_se = float(np.std(sample, ddof=1) / np.sqrt(n)) if n > 1 else 0.0
    return {
        "count": n,
        "lag": chosen_lag,
        "mean": mean,
        "se": se,
        "ci95": [mean - 1.96 * se, mean + 1.96 * se],
        "iid_se": iid_se,
        "iid_ci95": [mean - 1.96 * iid_se, mean + 1.96 * iid_se],
    }


def paired_model_diagnostics(runs: dict[int, dict], lam: float) -> list[dict]:
    """Estimate uncertainty for the paired contrasts used by the model check.

    Every arm contains the same event IDs in the same order.  Subtracting the
    B=1 latency event by event removes the shared source/network component.  The
    contrasts are then averaged within the treatment arm's batches before HAC
    estimation so events that share one flush are not treated as independent.
    """
    if 1 not in runs:
        raise ValueError("paired diagnostics require a batch_size=1 arm")
    baseline = np.asarray(runs[1]["latencies"], dtype=float)
    diagnostics: list[dict] = []
    for batch_size in sorted(batch for batch in runs if batch > 1):
        treatment = np.asarray(runs[batch_size]["latencies"], dtype=float)
        if len(treatment) != len(baseline):
            raise ValueError("paired arms must contain the same event count")
        contrasts = treatment - baseline
        complete = (len(contrasts) // batch_size) * batch_size
        contrast_batches = contrasts[:complete].reshape(-1, batch_size).mean(axis=1)
        predicted_difference = (batch_size - 1) / (2 * lam) * 1000.0
        residuals = contrast_batches - predicted_difference
        primary = newey_west_mean_ci(residuals)
        default_lag = int(primary["lag"])
        sensitivity_lags = sorted(
            {0, default_lag, min(len(residuals) - 1, default_lag * 2)}
        )
        diagnostics.append(
            {
                "batch_size": batch_size,
                "paired_events": len(contrasts),
                "contrast_batches": len(contrast_batches),
                "paired_mean_difference_ms": round(float(np.mean(contrasts))),
                "predicted_difference_ms": round(predicted_difference),
                "residual_mean_ms": round(float(primary["mean"])),
                "residual_hac_lag_batches": default_lag,
                "residual_ci95_ms": [round(value) for value in primary["ci95"]],
                "lag_sensitivity": [
                    {
                        "lag": lag,
                        "ci95_ms": [
                            round(value)
                            for value in newey_west_mean_ci(residuals, lag=lag)["ci95"]
                        ],
                    }
                    for lag in sensitivity_lags
                ],
            }
        )
    return diagnostics


def _record_latency_ms(record: dict) -> float:
    delta = parse_iso(record["ingestion_time"]) - parse_iso(record["timestamp_utc"])
    return delta.total_seconds() * 1000.0


def summarise(values: np.ndarray, batch_size: int = 1) -> dict:
    """Summarise a latency sample.

    Events inside one batch share a fill period, and adjacent batches can also be
    serially correlated. The primary interval is therefore Newey-West HAC over
    per-batch means. Batch-independent and event-independent intervals are retained
    as sensitivity values.
    """
    ordered = sorted(values.tolist())
    n = len(ordered)
    mean = float(np.mean(ordered))
    sd = float(np.std(ordered, ddof=1)) if n > 1 else 0.0

    # Cluster by batch: the file is written in flush order, batch_size events per flush.
    batches = (
        values[: (len(values) // batch_size) * batch_size].reshape(-1, batch_size)
        if batch_size > 1 and len(values) >= batch_size
        else values.reshape(-1, 1)
    )
    batch_means = batches.mean(axis=1)
    n_eff = len(batch_means)
    sd_batch = float(np.std(batch_means, ddof=1)) if n_eff > 1 else 0.0
    half = 1.96 * sd_batch / (n_eff**0.5) if n_eff > 1 else 0.0
    naive_half = 1.96 * sd / (n**0.5) if n > 1 else 0.0
    hac = newey_west_mean_ci(batch_means)
    hac_low, hac_high = hac["ci95"]
    hac_half = (hac_high - hac_low) / 2
    lag1 = (
        float(np.corrcoef(batch_means[:-1], batch_means[1:])[0, 1])
        if n_eff > 2
        else None
    )

    return {
        "count": n,
        "p50": round(percentile(ordered, 50)),
        "p95": round(percentile(ordered, 95)),
        "p99": round(percentile(ordered, 99)),
        "mean": round(mean),
        "min": round(ordered[0]),
        "max": round(ordered[-1]),
        "sd": round(sd),
        "batches": n_eff,
        "ci_method": "Newey-West HAC over batch means",
        "hac_lag_batches": hac["lag"],
        "lag1_autocorrelation_batch_means": round(lag1, 3)
        if lag1 is not None
        else None,
        "ci95_half_width": round(hac_half, 1),
        "ci95": [round(hac_low), round(hac_high)],
        "ci95_batch_means_independent": [round(mean - half), round(mean + half)],
        "ci95_half_width_batch_means_independent": round(half, 1),
        "ci95_half_width_naive_events_independent": round(naive_half, 1),
    }


def load_runs(manifest: dict, results_dir: Path) -> dict[int, dict]:
    """Build all arms from one ordered intersection of complete event IDs."""
    cohort = select_common_cohort(manifest, results_dir)
    runs = {}
    for output in manifest["outputs"]:
        batch_size = int(output["batch_size"])
        records = cohort[batch_size]
        overall = np.array([_record_latency_ms(record) for record in records])
        per_symbol: dict[str, list[float]] = {}
        for record, latency in zip(records, overall, strict=True):
            per_symbol.setdefault(record.get("symbol", "UNKNOWN"), []).append(
                float(latency)
            )
        runs[batch_size] = {
            "batch_size": batch_size,
            "latencies": overall,
            "per_symbol": {
                symbol: np.array(values) for symbol, values in per_symbol.items()
            },
            "summary": summarise(overall, batch_size),
            "file": output["file"],
        }
    return runs


def test_model(runs: dict[int, dict], lam: float) -> dict:
    """Compare calibration-derived predictions with held-out B>1 means."""
    if 1 not in runs:
        raise SystemExit(
            "the model test needs a batch_size=1 capture to measure transit"
        )
    transit = float(np.mean(runs[1]["latencies"]))

    rows = []
    for batch_size in sorted(runs):
        observed = float(np.mean(runs[batch_size]["latencies"]))
        predicted = transit + (batch_size - 1) / (2 * lam) * 1000.0
        rows.append(
            {
                "batch_size": batch_size,
                "observed_mean_ms": round(observed),
                "predicted_mean_ms": round(predicted),
                "error_ms": round(observed - predicted),
                "error_pct": round((observed - predicted) / predicted * 100, 1)
                if predicted
                else None,
            }
        )

    # Regression of observed mean on batch size: slope should be 1000/(2*lambda).
    sizes = np.array([r["batch_size"] for r in rows], dtype=float)
    means = np.array([r["observed_mean_ms"] for r in rows], dtype=float)
    slope, intercept = np.polyfit(sizes, means, 1)
    predicted_line = slope * sizes + intercept
    ss_res = float(np.sum((means - predicted_line) ** 2))
    ss_tot = float(np.sum((means - means.mean()) ** 2))

    return {
        "arrival_rate_eps": round(lam, 3),
        "transit_ms_from_batch1": round(transit),
        "calibration_arm": "B=1 supplies T; no B>1 outcome is fitted",
        "held_out_arms": [batch_size for batch_size in sorted(runs) if batch_size > 1],
        "rows": rows,
        "fitted_slope_ms_per_event": round(float(slope), 2),
        "model_slope_ms_per_event": round(1000.0 / (2 * lam), 2),
        "fitted_intercept_ms": round(float(intercept)),
        "r_squared": round(1 - ss_res / ss_tot, 4) if ss_tot else None,
    }


def fig_ecdf(runs, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    for i, batch_size in enumerate(sorted(runs)):
        v = np.sort(runs[batch_size]["latencies"])
        ax.plot(
            v,
            np.arange(1, len(v) + 1) / len(v) * 100,
            color=COLOURS[i % 4],
            linestyle=STYLES[i % 4],
            linewidth=1.4,
            label=f"batch size {batch_size}",
        )
    ax.set_xscale("log")
    ax.set_xlabel("latency_ms (log scale)")
    ax.set_ylabel("cumulative % of events")
    ax.set_title("Generator-side latency by batch size (identical event stream)")
    ax.legend(frameon=False, loc="lower right")
    fig.savefig(out)
    plt.close(fig)


def fig_model(model, out: Path) -> None:
    rows = model["rows"]
    sizes = np.array([r["batch_size"] for r in rows], dtype=float)
    observed = [r["observed_mean_ms"] for r in rows]
    predicted = [r["predicted_mean_ms"] for r in rows]

    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    grid = np.linspace(1, sizes.max(), 200)
    ax.plot(
        grid,
        model["transit_ms_from_batch1"]
        + (grid - 1) * model["model_slope_ms_per_event"],
        color=COLOURS[0],
        linewidth=1.4,
        label=r"model: $T + (B-1)/(2\lambda)$",
    )
    ax.plot(sizes, observed, "o", color=COLOURS[1], markersize=6, label="observed mean")
    for s, o, p in zip(sizes, observed, predicted):
        ax.annotate(
            f"{o:,}", (s, o), textcoords="offset points", xytext=(6, -10), fontsize=7.5
        )
    ax.set_xlabel("batch size (B)")
    ax.set_ylabel("mean latency_ms")
    ax.set_title(
        f"Batch-fill model vs measurement "
        f"($\\lambda$ = {model['arrival_rate_eps']} eps, T = {model['transit_ms_from_batch1']} ms)"
    )
    ax.legend(frameon=False, loc="upper left")
    fig.savefig(out)
    plt.close(fig)


def fig_distribution(runs, batch_size, lam, out: Path) -> None:
    """At a given B the wait should be near-uniform on [0, (B-1)/lambda], not exponential."""
    if batch_size not in runs:
        return
    v = runs[batch_size]["latencies"]
    fig, ax = plt.subplots(figsize=(5.5, 3.4))
    ax.hist(v, bins=60, color=COLOURS[0], alpha=0.8, edgecolor="white", linewidth=0.3)
    ceiling = (batch_size - 1) / lam * 1000.0
    ax.axvline(
        ceiling,
        color=COLOURS[1],
        linestyle="--",
        linewidth=1.3,
        label=f"constant-rate reference $(B-1)/\\lambda$ = {ceiling:,.0f} ms",
    )
    ax.axvline(
        float(np.mean(v)),
        color=COLOURS[2],
        linestyle="-.",
        linewidth=1.3,
        label=f"observed mean = {np.mean(v):,.0f} ms",
    )
    ax.set_xlabel("latency_ms")
    ax.set_ylabel("events")
    ax.set_title(f"Latency distribution at batch size {batch_size}")
    ax.legend(frameon=False)
    fig.savefig(out)
    plt.close(fig)


def fig_per_symbol(runs, batch_size, out: Path) -> None:
    if batch_size not in runs:
        return
    per = runs[batch_size]["per_symbol"]
    order = sorted(per, key=lambda s: -len(per[s]))
    means = [float(np.mean(per[s])) for s in order]
    counts = [len(per[s]) for s in order]

    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    bars = ax.bar(order, means, color=COLOURS[0], width=0.6)
    for bar, n in zip(bars, counts):
        ax.annotate(
            f"n={n:,}",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center",
            va="bottom",
            fontsize=7.5,
        )
    ax.set_ylabel("mean latency_ms")
    ax.set_title(f"Mean latency by trading pair (batch size {batch_size})")
    ax.margins(y=0.15)
    fig.savefig(out)
    plt.close(fig)


def cloud_commit_p50(results_dir: Path) -> float | None:
    """Return the highest-rate p50 from the newest cloud validation report."""
    reports = sorted(results_dir.glob("cloud_e2e_validation_*.json"))
    if not reports:
        return None
    payload = json.loads(reports[-1].read_text())
    runs = payload.get("runs", [])
    if not runs:
        return None
    highest_rate = max(runs, key=lambda row: row["target_events_per_second"])
    return float(highest_rate["commit_latency_ms"]["p50"])


def fig_decomposition(
    model,
    batch50_mean: float,
    out: Path,
    cloud_commit_p50_ms: float | None = None,
) -> list[dict]:
    """Show measured boundaries without assigning values to missing components."""
    transit = model["transit_ms_from_batch1"]
    batch_wait = max(0.0, batch50_mean - transit)
    segments = [
        ("Exchange \u2192 serialisation\n(B = 1 baseline)", transit, True),
        ("Generator batch buffer\n(B = 50)", batch_wait, True),
        (
            "Serialisation → Bronze commit\n(Event Hubs + Eventstream)",
            cloud_commit_p50_ms,
            cloud_commit_p50_ms is not None,
        ),
        ("Broker acknowledgement", None, False),
        ("Event Hub \u2192 Eventstream", None, False),
        ("Eventstream \u2192 Bronze", None, False),
        ("Update policy\nBronze \u2192 Silver", None, False),
        ("Materialized view\nSilver \u2192 Gold", None, False),
    ]
    labels = [s[0] for s in segments]

    fig, ax = plt.subplots(figsize=(6.2, 3.8))
    y = np.arange(len(segments))[::-1]
    for yi, (_, value, measured) in zip(y, segments):
        if measured:
            ax.barh(yi, value, height=0.6, color=COLOURS[0])
            text = f"{value:,.0f} ms"
            x = value
        else:
            text = "not measured"
            x = 0
        ax.annotate(
            text,
            (x, yi),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            fontsize=7.5,
            color="black" if measured else COLOURS[1],
        )
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7.5)
    ax.set_xlabel("observed interval or modelled contribution, ms")
    ax.set_title("Pipeline measurement boundary")
    measured_values = [value for _, value, measured in segments if measured]
    ax.set_xlim(0, max(measured_values) * 1.25)
    fig.savefig(out)
    plt.close(fig)
    return [
        {
            "segment": label.replace("\n", " "),
            "value_ms": value,
            "measured": measured,
        }
        for label, value, measured in segments
    ]


def fig_throughput(results_dir: Path, out: Path) -> dict | None:
    """Aggregate schema-v2 broker-acknowledged runs from the newest test date."""
    runs = []
    for path in sorted(results_dir.glob("load_test_v2_*eps_*.json")):
        m = re.search(r"_(\d{8})_\d{6}_\d{6}\.json$", path.name)
        if not m:
            continue
        d = json.loads(path.read_text())
        runs.append(
            {
                "date": m.group(1),
                "target": d["test_config"]["target_eps"],
                "duration": d["test_config"]["requested_duration_s"],
                "achieved": d["rates_eps"]["broker_acknowledged"],
                "errors": d["counts"]["errors"],
                "sent": d["counts"]["broker_acknowledged"],
                "file": path.name,
            }
        )
    if not runs:
        return None

    latest_date = max(r["date"] for r in runs)
    runs = [r for r in runs if r["date"] == latest_date]
    evidence_duration = max(r["duration"] for r in runs)
    runs = [r for r in runs if r["duration"] == evidence_duration]

    by_target: dict[int, list[dict]] = {}
    for r in runs:
        by_target.setdefault(r["target"], []).append(r)

    targets = sorted(by_target)
    stats = []
    for t in targets:
        achieved = [r["achieved"] for r in by_target[t]]
        stats.append(
            {
                "target_eps": t,
                "runs": len(achieved),
                "mean_achieved_eps": round(float(np.mean(achieved)), 1),
                "sd_achieved_eps": round(float(np.std(achieved, ddof=1)), 1)
                if len(achieved) > 1
                else None,
                "min_achieved_eps": round(min(achieved), 1),
                "max_achieved_eps": round(max(achieved), 1),
                "total_errors": sum(r["errors"] for r in by_target[t]),
                "total_sent": sum(r["sent"] for r in by_target[t]),
                "attainment_pct": round(float(np.mean(achieved)) / t * 100, 1),
                "files": [r["file"] for r in by_target[t]],
            }
        )

    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    x = np.arange(len(targets))
    means = [s["mean_achieved_eps"] for s in stats]
    errs = [s["sd_achieved_eps"] or 0 for s in stats]
    ax.bar(x - 0.2, targets, width=0.4, color=COLOURS[3], label="target rate")
    ax.bar(
        x + 0.2,
        means,
        width=0.4,
        color=COLOURS[0],
        label="achieved rate",
        yerr=errs,
        capsize=3,
        ecolor=INK_ERR,
    )
    for i, s in enumerate(stats):
        ax.annotate(
            f"{s['mean_achieved_eps']:,.0f}",
            (i + 0.2, s["mean_achieved_eps"]),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            fontsize=7.5,
        )
        ax.annotate(
            f"{s['attainment_pct']:.0f}% of target",
            (i, max(s["target_eps"], s["mean_achieved_eps"])),
            xytext=(0, 14),
            textcoords="offset points",
            ha="center",
            fontsize=7.2,
            color="#5b6570",
        )
    ax.set_xticks(x)
    ax.set_xticklabels([f"{t:,}" for t in targets])
    ax.set_xlabel("target events per second")
    ax.set_ylabel("events per second")
    n_runs = stats[0]["runs"] if stats else 0
    total_errors = sum(s["total_errors"] for s in stats)
    ax.set_title(
        "Load test: broker-acknowledged vs scheduled rate\n"
        f"{n_runs} runs per rate, {total_errors} producer errors in total",
        fontsize=9.5,
    )
    ax.legend(frameon=False, loc="upper left")
    ax.margins(y=0.24)
    fig.savefig(out)
    plt.close(fig)
    return {
        "test_date": latest_date,
        "requested_duration_s": evidence_duration,
        "per_target": stats,
    }


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--results-dir", type=Path, default=here / "results")
    ap.add_argument("--out-dir", type=Path, default=here.parent / "docs" / "figures")
    args = ap.parse_args()

    manifest_path = args.manifest or newest_manifest(args.results_dir)
    manifest = json.loads(manifest_path.read_text())
    args.out_dir.mkdir(parents=True, exist_ok=True)

    runs = load_runs(manifest, args.results_dir)
    lam = manifest["arrival_rate_eps"]
    model = test_model(runs, lam)
    paired_diagnostics = paired_model_diagnostics(runs, lam)

    fig_ecdf(runs, args.out_dir / "fig_latency_ecdf_by_batch.png")
    fig_model(model, args.out_dir / "fig_batch_fill_model.png")
    fig_distribution(
        runs, 50, lam, args.out_dir / "fig_latency_distribution_batch50.png"
    )
    fig_per_symbol(runs, 50, args.out_dir / "fig_latency_by_symbol.png")
    throughput = fig_throughput(
        args.results_dir, args.out_dir / "fig_load_test_throughput.png"
    )
    decomposition = fig_decomposition(
        model,
        float(np.mean(runs[50]["latencies"]))
        if 50 in runs
        else model["transit_ms_from_batch1"],
        args.out_dir / "fig_latency_decomposition.png",
        cloud_commit_p50_ms=cloud_commit_p50(args.results_dir),
    )

    input_checksums = {
        output["file"]: sha256_file(args.results_dir / output["file"])
        for output in manifest["outputs"]
    }
    report = {
        "schema_version": 2,
        "analysed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest": manifest_path.name,
        "provenance": {
            "git_commit": git_commit(here.parent),
            "manifest_sha256": sha256_file(manifest_path),
            "input_sha256": input_checksums,
            "selection": (
                "source=coinbase_ws; remove each arm's final partial flush; "
                "intersect event_id across all arms; preserve B=1 order"
            ),
            "common_cohort_events": min(
                run["summary"]["count"] for run in runs.values()
            ),
        },
        "capture": {
            k: manifest[k]
            for k in (
                "captured_at",
                "duration_s",
                "events_received",
                "arrival_rate_eps",
                "symbols",
            )
        },
        "per_batch_size": {str(b): runs[b]["summary"] for b in sorted(runs)},
        "model_test": model,
        "paired_model_diagnostics": paired_diagnostics,
        "per_symbol_batch50": {
            s: summarise(v)
            for s, v in sorted(
                runs[50]["per_symbol"].items(), key=lambda kv: -len(kv[1])
            )
        }
        if 50 in runs
        else {},
        "load_test": throughput,
        "latency_decomposition": decomposition,
    }
    out = args.results_dir / (
        f"experiment_report_v2_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    )
    out.write_text(json.dumps(report, indent=2) + "\n")

    print(
        f"capture: {manifest['events_received']:,} events over {manifest['duration_s']}s "
        f"= {lam} eps\n"
    )
    print(
        f"{'B':>5}{'n':>9}{'batches':>9}{'mean':>8}{'ci95':>16}{'p50':>8}{'p95':>8}"
        f"{'p99':>8}{'predicted':>11}{'err %':>8}"
    )
    for row in model["rows"]:
        b = row["batch_size"]
        s = runs[b]["summary"]
        ci = f"{s['ci95'][0]:,}-{s['ci95'][1]:,}"
        print(
            f"{b:>5}{s['count']:>9,}{s['batches']:>9,}{s['mean']:>8,}{ci:>16}{s['p50']:>8,}"
            f"{s['p95']:>8,}{s['p99']:>8,}{row['predicted_mean_ms']:>11,}{row['error_pct']:>8}"
        )
    print(f"\ntransit T (from B=1)      : {model['transit_ms_from_batch1']} ms")
    print(f"model slope  1/(2*lambda) : {model['model_slope_ms_per_event']} ms/event")
    print(
        f"fitted slope (regression) : {model['fitted_slope_ms_per_event']} ms/event"
        f"   R^2 = {model['r_squared']}"
    )
    print(f"\nfigures -> {args.out_dir}\nreport  -> {out}")


if __name__ == "__main__":
    main()
