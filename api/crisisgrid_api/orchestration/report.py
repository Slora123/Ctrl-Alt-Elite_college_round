"""Report stub (P4): an honest Tier-1 summary. The full evidence report (verdicts, attribution, deviations, PDF export) is P8."""
from __future__ import annotations

from .models import RunState


def build_report(state: RunState) -> str:
    L: list[str] = ["# CrisisGrid report (Tier 1: static audit only)", ""]
    if state.needs_ocr:
        L += ["**The PDF has no extractable text (scanned?).** Nothing was analysed. Provide a text PDF or enable OCR.", ""]
    if state.parsed:
        p = state.parsed
        L += [f"## Paper: {p.title or '(untitled)'}", "", f"## Claims ({len(p.claims)})", ""]
        for c in p.claims:
            L.append(f"- {c.text} — reported {c.reported_value:g} ({c.metric} on {c.dataset}/{c.split}), provenance: {c.provenance}")
        if p.dropped:
            L += ["", f"_{len(p.dropped)} extracted item(s) were dropped because their quote was not found in the paper._"]
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
    L += ["", "## Limits of this report", "",
          "- **No experiment was executed**, so no claim has a verdict. Findings above come from the paper and its public artifacts only."]
    if state.data_resolution == "stopped":
        L.append("- The run was stopped at Tier 1 at the user's request (data unavailable).")
    if state.data_resolution == "substitute":
        L.append("- Substitute data was chosen; any run on it is **not a reproduction** of the paper's results.")
    L += [f"- {c}" for c in state.caveats]
    return "\n".join(L) + "\n"
