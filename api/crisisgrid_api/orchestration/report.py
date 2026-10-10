"""Report (P4 stub, extended in P6): an honest summary of what was found and, only if clean runs happened, what was measured.
The full evidence report (attribution, agent-edit deviations, PDF export) is P8."""
from __future__ import annotations

from crisisgrid_core.report_check import render_deviations

from .models import RunState


def build_report(state: RunState, summary: str | None = None) -> str:
    ran = state.execution == "clean_runs"
    title = f"Tier {state.route.tier}" if ran and state.route else "Tier 1: static audit only"
    L: list[str] = [f"# CrisisGrid report ({title})", ""]
    if summary:
        L += ["## Summary", "", summary, "", "_Every number in this summary was checked against the measured evidence below._", ""]
    if state.needs_ocr:
        L += ["**The PDF has no extractable text (scanned?).** Nothing was analysed. Provide a text PDF or enable OCR.", ""]
    if state.parsed:
        p = state.parsed
        L += [f"## Paper: {p.title or '(untitled)'}", "", f"## Claims ({len(p.claims)})", ""]
        for c in p.claims:
            L.append(f"- {c.text} — reported {c.reported_value:g} ({c.metric} on {c.dataset}/{c.split}), provenance: {c.provenance}")
        if p.dropped:
            L += ["", f"_{len(p.dropped)} extracted item(s) were dropped because their quote was not found in the paper._"]
    if state.verdicts:
        L += ["", "## Verdicts (clean runs only)", ""]
        by_id = {c.id: c for c in (state.parsed.claims if state.parsed else [])}
        for v in state.verdicts:
            c = by_id.get(v.claim_id)
            mean = f"{v.measured_mean:.4g}" if v.measured_mean is not None else "n/a"
            std = f" ± {v.measured_std:.2g}" if v.measured_std is not None else ""
            what = c.text if c else v.claim_id
            L.append(f"- **{v.status}** — {what}: reported {v.reported:g}, measured {mean}{std} over {v.n_seeds} seed(s)")
            L += [f"  - caveat: {cv}" for cv in v.caveats]
    if state.route:
        L += ["", f"## Route: case {state.route.case}, Tier {state.route.tier}", ""] + [f"- {r}" for r in state.route.reasons]
    if state.gaps:
        L += ["", f"## Reproducibility gaps ({len(state.gaps)})", ""]
        for g in state.gaps:
            L.append(f"- **{g.severity}** `{g.field}` ({g.detected_by}): {g.description}")
    if state.inventory:
        inv = state.inventory
        L += ["", "## Artifacts", "", f"- Code: {inv.repo.url + ' @ ' + (inv.repo.commit or 'unpinned') if inv.repo else 'not found'}",
              f"- Datasets: {', '.join(d.name for d in inv.datasets) or 'none found'}",
              f"- Weights: {', '.join(inv.weights_urls) or 'none found'}"]
        if inv.missing_components:
            L.append(f"- Missing: {', '.join(inv.missing_components)}")
    if ran:
        L += ["", "## Deviations from the paper", "", render_deviations([])]  # nothing in the pipeline edits code yet (repair loop: P7)
    L += ["", "## Limits of this report", ""]
    if ran:
        L.append(f"- {len(state.run_results)} clean run(s) (fresh process, fixed seed, top to bottom) were executed; "
                 "interactive runs are never evidence.")
    else:
        L.append("- **No experiment was executed**, so no claim has a verdict. Findings above come from the paper and its public "
                 "artifacts only.")
    if state.data_resolution == "stopped":
        L.append("- The run was stopped at Tier 1 at the user's request (data unavailable).")
    if state.data_resolution == "substitute":
        L.append("- Substitute data was chosen; any run on it is **not a reproduction** of the paper's results.")
    L += [f"- {c}" for c in state.caveats]
    return "\n".join(L) + "\n"
