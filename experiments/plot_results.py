#!/usr/bin/env python3
"""Turn results/*.json into charts (results/*.png) and a markdown summary (results/SUMMARY.md)."""
import json
import os
import statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    plt = None
    print("matplotlib not installed - writing SUMMARY.md only (sudo apt install python3-matplotlib)")

COLORS = {"static": "#8c8c8c", "dynamic": "#2a6fdb", "failover_on": "#2a6fdb",
          "failover_off": "#d9534f", "no_failure": "#8c8c8c"}


def load(name):
    p = os.path.join(RES, name + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def grouped_bar(ax, groups, series, values, ylabel, title):
    width = 0.8 / len(series)
    for i, s in enumerate(series):
        xs = [g + (i - (len(series) - 1) / 2) * width for g in range(len(groups))]
        ys = [values.get((s, g)) or 0 for g in groups]
        ax.bar(xs, ys, width, label=s, color=COLORS.get(s))
        for x, y in zip(xs, ys):
            ax.text(x, y, "%.1f" % y, ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([str(g) for g in groups])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    ax.spines[["top", "right"]].set_visible(False)


def main():
    md = ["# Experiment results\n"]

    e0 = load("e0_functional")
    if e0:
        md.append("## E0 Functional tests\n\n| Test | Result |\n|---|---|")
        md += ["| %s | %s |" % (t["test"], "PASS" if t["passed"] else "FAIL") for t in e0]
        md.append("")

    e1 = load("e1_concurrency")
    if e1:
        agg, comp, fair = defaultdict(list), defaultdict(list), defaultdict(list)
        for r in e1:
            k = (r["mode"], r["clients"])
            agg[k].append(r.get("aggregate_mbps"))
            comp[k].append(r.get("avg_completion_s"))
            fair[k].append(r.get("jain_fairness"))
        ns = sorted({r["clients"] for r in e1})
        md.append("## E1 Concurrent downloads: static vs dynamic routing\n")
        md.append("| Clients | Static agg. Mbps | Dynamic agg. Mbps | Static avg time (s) | Dynamic avg time (s) | Static fairness | Dynamic fairness |")
        md.append("|---|---|---|---|---|---|---|")
        for n in ns:
            md.append("| %d | %s | %s | %s | %s | %s | %s |" % (
                n, mean(agg[("static", n)]), mean(agg[("dynamic", n)]), mean(comp[("static", n)]),
                mean(comp[("dynamic", n)]), mean(fair[("static", n)]), mean(fair[("dynamic", n)])))
        md.append("")
        if plt:
            fig, axes = plt.subplots(1, 2, figsize=(11, 4))
            grouped_bar(axes[0], ns, ["static", "dynamic"], {k: mean(v) for k, v in agg.items()},
                        "Aggregate throughput (Mbps)", "Throughput vs concurrent clients")
            grouped_bar(axes[1], ns, ["static", "dynamic"], {k: mean(v) for k, v in comp.items()},
                        "Avg completion time (s)", "Completion time vs concurrent clients")
            for ax in axes:
                ax.set_xlabel("Concurrent clients")
            fig.tight_layout()
            fig.savefig(os.path.join(RES, "e1_concurrency.png"), dpi=130)

    e2 = load("e2_loss")
    if e2:
        agg = defaultdict(list)
        for r in e2:
            agg[(r["mode"], r["loss_pct"])].append(r.get("aggregate_mbps"))
        losses = sorted({r["loss_pct"] for r in e2})
        md.append("## E2 Packet loss on core links (4 clients)\n\n| Loss % | Static Mbps | Dynamic Mbps |\n|---|---|---|")
        for l in losses:
            md.append("| %s | %s | %s |" % (l, mean(agg[("static", l)]), mean(agg[("dynamic", l)])))
        md.append("")
        if plt:
            fig, ax = plt.subplots(figsize=(6, 4))
            grouped_bar(ax, losses, ["static", "dynamic"], {k: mean(v) for k, v in agg.items()},
                        "Aggregate throughput (Mbps)", "Throughput under packet loss")
            ax.set_xlabel("Core-link loss (%)")
            fig.tight_layout()
            fig.savefig(os.path.join(RES, "e2_loss.png"), dpi=130)

    e3 = load("e3_failover")
    if e3:
        by = defaultdict(list)
        for r in e3:
            by[r["scenario"]].append(r)
        md.append("## E3 Link failure (s1-s2) during a 10 MB download\n")
        md.append("| Scenario | Success | Avg completion (s) | Avg max stall (s) | Reconnects |\n|---|---|---|---|---|")
        for s in ("no_failure", "failover_on", "failover_off"):
            rs = by.get(s, [])
            md.append("| %s | %d/%d | %s | %s | %s |" % (s, sum(1 for r in rs if r["ok"]), len(rs),
                                                     mean([r["completion_s"] for r in rs]),
                                                     mean([r["max_gap_s"] for r in rs]),
                                                     mean([r["reconnects"] for r in rs])))
        md.append("")
        if plt:
            fig, ax = plt.subplots(figsize=(6, 4))
            scen = [s for s in ("no_failure", "failover_on", "failover_off") if s in by]
            vals = [mean([r["max_gap_s"] for r in by[s]]) or 0 for s in scen]
            ax.bar(scen, vals, color=[COLORS[s] for s in scen])
            for i, v in enumerate(vals):
                ax.text(i, v, "%.2f s" % v, ha="center", va="bottom")
            ax.set_ylabel("Longest transfer stall (s)")
            ax.set_title("Recovery after link failure")
            ax.spines[["top", "right"]].set_visible(False)
            fig.tight_layout()
            fig.savefig(os.path.join(RES, "e3_failover.png"), dpi=130)

    e4 = load("e4_security")
    if e4:
        md.append("## E4 Security / access policy\n\n| Check | Observed | Expected | Result |\n|---|---|---|---|")
        md += ["| %s | %s | %s | %s |" % (c["check"], c["observed"], c["expected"], "PASS" if c["passed"] else "FAIL")
               for c in e4["checks"]]
        md.append("\nBrute-force attempts:\n")
        md += ["- attempt %d at t=%.2fs: %s" % (a["attempt"], a["t"], a["error"]) for a in e4["brute_force"]]
        md.append("")

    e5 = load("e5_overload")
    if e5:
        md.append("## E5 Server overload (3 slots, 10 simultaneous clients)\n\n| Client retries | Succeeded | BUSY replies | Avg completion (s) |\n|---|---|---|---|")
        for r in e5:
            md.append("| %d | %d/%d | %d | %s |" % (r["client_retries"], r.get("ok", 0), r["n"], r.get("busy", 0),
                                                 r.get("avg_completion_s")))
        md.append("")

    with open(os.path.join(RES, "SUMMARY.md"), "w") as f:
        f.write("\n".join(md))
    print("\n".join(md))
    print("\nwrote results/SUMMARY.md" + (" and PNG charts" if plt else ""))


if __name__ == "__main__":
    main()
