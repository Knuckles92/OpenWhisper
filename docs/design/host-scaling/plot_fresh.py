"""Reconstruct fresh figures from preserved aggregate JSON (original script was lost).

Usage: python plot_fresh.py PATH/TO/analysis/focused-summary.json --output DIR
Requires matplotlib. This preserves the data, axes, labels and layout of the
surviving original PNGs; pixel-identical recreation is not asserted.
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads(args.summary.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.size': 12})
    colors = {'baseline': '#3774a5', 'candidate': '#df812d'}
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8))
    for index, label in enumerate(colors):
        row = next(r for r in rows if r['category'] == 'sustained' and r['label'] == label)
        axes[0].bar([x + (index - .5) * .36 for x in range(3)],
                    [row[f'small_final_latency_p{p}_s'] for p in (50, 95, 99)],
                    width=.36, label=label, color=colors[label])
        subset = sorted((r for r in rows if r['category'] == 'cap64' and r['label'] == label), key=lambda r: r['clients'])
        axes[1].plot([r['clients'] for r in subset], [r['small_final_latency_p95_s'] for r in subset], 'o-', label=label, color=colors[label])
    axes[0].set_xticks(range(3), ['p50', 'p95', 'p99'])
    axes[0].set_ylabel('Observed small-final latency (s)')
    axes[0].set_title('N=12, production connection cap\n353/353 finals per source; 329 clips <15s')
    axes[1].set_xticks([16, 24, 32])
    axes[1].set_xlabel('Simulated dictating clients')
    axes[1].set_ylabel('Observed small-final p95 (s)')
    axes[1].set_title('Cap relaxed to 64 (not production default)\nAll 99 / 153 / 202 finals completed per source')
    for ax in axes:
        ax.set_ylim(0, 1.05)
        ax.axhline(1, color='gray', linestyle='--', linewidth=.8)
        ax.grid(axis='y', alpha=.2)
        ax.set_axisbelow(True)
        ax.legend(loc='center right')
    fig.suptitle('Real loopback TLS; owner-calibrated GPU-Parakeet timing simulation')
    fig.text(.5, .02, '0.2 arrivals/client/s, seed 7. Sustained: 150s; cap sensitivity: 30s. These are not real-GPU capacity measurements.', ha='center', fontsize=9)
    fig.tight_layout(rect=(0, .08, 1, .88))
    fig.savefig(args.output / 'fresh-gpu-parakeet.png', dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4.5))
    for ax, profile in zip(axes, ('cpu-parakeet', 'gpu-whisper-turbo', 'cpu-whisper-small')):
        for label in colors:
            subset = sorted((r for r in rows if r['category'] == 'compact' and r['profile'] == profile and r['label'] == label), key=lambda r: r['clients'])
            ax.plot([r['clients'] for r in subset], [r['small_final_latency_p95_s'] for r in subset], 'o-', label=label, color=colors[label])
        ax.set_title(profile + '\n' + ('(owner-calibrated timing model)' if profile == 'cpu-parakeet' else '(hypothetical)'))
        ax.set_xticks([1, 6, 12])
        ax.set_xlabel('Simulated clients')
        ax.set_ylabel('Successful small-final p95 (s)')
        ax.axhline(1, color='gray', linestyle='--', linewidth=.8)
        ax.grid(alpha=.2)
        ax.legend()
    fig.suptitle('Fresh isolated compact screens: 20s stress generation, 5s issue grace')
    fig.text(.5, .02, 'Low sample counts and overload censoring: not certified capacity. Successful-tail latency excludes busy/unissued attempts; see CSV counts.', ha='center', fontsize=9)
    fig.tight_layout(rect=(0, .08, 1, .88))
    fig.savefig(args.output / 'fresh-compact-profiles.png', dpi=160)
    plt.close(fig)


if __name__ == '__main__':
    main()
