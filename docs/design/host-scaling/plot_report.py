"""Render report PNGs from retained, explicitly classified aggregate CSVs."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
BLUE, GOLD, PINK = "#245A81", "#B47719", "#9A4E79"


def read(name):
    with (ROOT / "data" / name).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def setup():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 11,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.axisbelow": True, "axes.grid": True,
        "grid.color": "#E0E5E8", "grid.linewidth": .7,
        "text.color": "#26323B", "axes.labelcolor": "#26323B",
        "savefig.facecolor": "white",
    })


def baseline():
    data = read("recovered-baseline.csv")
    profiles = ["gpu-parakeet", "gpu-whisper-turbo",
                "cpu-parakeet", "cpu-whisper-small"]
    for measure in ["latency", "unserved"]:
        fig, axes = plt.subplots(2, 2, figsize=(12, 8))
        for ax, profile in zip(axes.flat, profiles):
            for duty, linestyle, marker in [("realistic", "-", "o"),
                                            ("stress", "--", "s")]:
                subset = sorted([r for r in data if r["profile"] == profile
                                 and r["duty"] == duty],
                                key=lambda r: int(r["clients"]))
                values = []
                for r in subset:
                    missing = sum(int(r[k]) for k in
                                  ["busy", "not_started", "not_connected"])
                    total = int(r["final_successes"]) + missing
                    values.append(float(r["small_final_p95_s"]) if measure == "latency"
                                  else 100 * missing / total)
                label = ".05 releases/s" if duty == "realistic" else ".2 releases/s"
                ax.plot([int(r["clients"]) for r in subset], values,
                        color=BLUE, linestyle=linestyle, marker=marker,
                        markersize=4, label=label)
            if measure == "latency":
                ax.set_yscale("log")
                ax.set_ylim(.03, 100)
                ax.axhline(1, color="#777777", linewidth=1, linestyle=":")
                ax.axhline(2, color="#999999", linewidth=1, linestyle=":")
                ax.set_ylabel("Successful <15s finals: p95 (s, log scale)")
            else:
                ax.set_ylim(-2, 102)
                ax.set_ylabel("Failed or unserved offered finals (%)")
            ax.set_title(profile + " (simulated)", loc="left", fontsize=12)
            ax.set_xlabel("Offered clients")
            ax.set_xticks([1, 4, 8, 12, 16, 24, 32])
            ax.legend(frameon=False, fontsize=10)
        title = ("Recovered baseline latency" if measure == "latency"
                 else "Recovered baseline: rejection, censoring and refusal")
        fig.suptitle(title, x=.08, ha="left", fontsize=17)
        fig.text(.08, .92,
                 "Original raw files lost after environment reset; exact retained aggregates, not a fresh rerun.",
                 fontsize=11)
        fig.text(.08, .025,
                 "Real TLS + serialized sleeping engines. N>12 is constrained by connection policy. "
                 "Censoring is not a host rejection.", fontsize=10)
        fig.subplots_adjust(left=.08, right=.98, bottom=.11, top=.85,
                            hspace=.42, wspace=.26)
        fig.savefig(ROOT / f"recovered-baseline-{measure}.png", dpi=150)
        plt.close(fig)


def codecs():
    data = [r for r in read("recovered-codec-pipeline-summary.csv")
            if float(r["audio_seconds"]) == 30]
    data.sort(key=lambda r: float(r["rtt_ms"]))
    assert len(data) == 3
    fig, ax = plt.subplots(figsize=(11, 5.8))
    for offset, (key, label, color) in enumerate([
            ("pcm_median_ms", "PCM", BLUE), ("flac_median_ms", "FLAC", GOLD),
            ("opus24_median_ms", "Opus 24 kbit/s", PINK)]):
        values = [float(r[key]) / 1000 for r in data]
        bars = ax.bar([i + (offset - 1) * .24 for i in range(3)], values,
                      width=.22, color=color, label=label)
        ax.bar_label(bars, labels=[f"{x:.3f}" for x in values],
                     padding=4, fontsize=10)
    ax.set_xticks(range(3), ["Loopback", "20 ms / 20 Mbit/s", "100 ms / 5 Mbit/s"])
    ax.set(ylabel="Median result latency (seconds)", ylim=(0, 2.15))
    ax.legend(frameon=False, loc="upper left")
    fig.suptitle("Codec crossover: thirty-second clean speech",
                 x=.1, ha="left", fontsize=17)
    fig.text(.1, .89, "Recovered aggregates · n=5 per bar · real codecs/TLS, simulated ASR",
             fontsize=11)
    fig.text(.1, .025, "Original raw files lost. User-space pacing, not WAN/loss. "
             "Opus recognition quality was not evaluated.", fontsize=10)
    fig.subplots_adjust(left=.1, right=.98, bottom=.13, top=.82)
    fig.savefig(ROOT / "recovered-codec-latency.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    setup()
    baseline()
    codecs()
