"""
MOCK AI/ML LAYER  --  placeholder until the real AI/ML models are plugged in.

main.py only talks to the five functions below. To go live, keep the same function
signatures and return shapes, and replace the bodies (or set PROVIDER and branch on it).
Everything returned here is simulated: deterministic (same input -> same output) so the
UI behaves predictably, but NOT the result of any real model, training run or reproduction.

Contract (what main.py expects):

  understand(title, text, analysis) -> {provider, summary, key_points[list[str]],
                                        model_family[list[str]], missing[list[str]],
                                        confidence[0..1]}
  extract_claims(text, title)       -> [(statement:str, reported_metric:float 0..1), ...]
  reproduce_claim(statement, reported, salt) -> {reproduced:float 0..1, runtime_s:float, note:str}
  run_experiment(config:dict)       -> {metric:float 0..1, curve:[float,...], runtime_s:float}
  audit(text, analysis)             -> [{category, description, severity, status, evidence, next}, ...]
"""
import hashlib
import math
import random
import re

PROVIDER = "mock"
METRIC_WORDS = ("accuracy", "f1", "precision", "recall", "auc", "silhouette")


def _rng(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def _clamp(x, lo=0.0, hi=0.999):
    return max(lo, min(hi, x))


def understand(title, text, analysis):
    r = _rng("understand", title, len(text or ""))
    params = (analysis or {}).get("parameters", {})
    kw = (analysis or {}).get("keywords", {})
    family = [k for k in ("transformer", "cnn", "resnet", "bert", "lstm") if k in kw] or ["unspecified"]
    missing = [k for k, v in params.items() if not v]
    t = text or ""
    m = re.search(r"abstract\s*[:\-]?\s*(.{80,600}?)(?:\n\s*\n|\n\s*1\.?\s|$)", t, re.I | re.S)
    snippet = " ".join((m.group(1) if m else t[:400]).split())
    summary = (snippet[:420] + "…") if len(snippet) > 420 else snippet
    if not summary:
        summary = "No extractable text, so a summary cannot be produced for this PDF (it may be scanned)."
    points = [f"Model family detected: {', '.join(family)}.",
              f"{len(params) - len(missing)} of {len(params)} training parameters were stated in the text."]
    if missing:
        points.append("Not specified: " + ", ".join(missing) + ".")
    if "baseline" not in kw:
        points.append("No baseline comparison is mentioned.")
    return {"provider": PROVIDER, "summary": summary, "key_points": points, "model_family": family,
            "missing": missing, "confidence": round(0.6 + r.random() * 0.3, 2)}


def extract_claims(text, title=""):
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        w = re.search(r"\b(" + "|".join(METRIC_WORDS) + r")\b", s, re.I)
        if not w:
            continue
        n = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", s) or re.search(r"(?<![\d.])(0?\.\d+)(?!\d)", s)
        if not n:
            continue
        v = float(n.group(1))
        v = v / 100 if "%" in n.group(0) else v
        if 0 < v < 1:
            out.append((" ".join(s.split())[:200], round(v, 4)))
        if len(out) >= 5:
            break
    if not out:
        r = _rng("claims", title)
        out = [("Primary reported metric (placeholder claim from mock extractor)", round(0.80 + r.random() * 0.15, 3))]
    return out


def reproduce_claim(statement, reported, salt=0):
    r = _rng("repro", statement, reported, salt)
    gap = r.choice([0.002, 0.004, 0.006, 0.012, 0.02, 0.035]) * (1 if r.random() < 0.85 else -1)
    return {"reproduced": round(_clamp(float(reported) - gap), 4), "runtime_s": round(40 + r.random() * 260, 1),
            "note": "Simulated reproduction. No experiment was executed."}


def run_experiment(config):
    c = config or {}

    def num(k, d):
        try:
            return float(c.get(k)) if c.get(k) not in (None, "") else d
        except (TypeError, ValueError):
            return d

    lr, bs, ep = num("learning_rate", 1e-3), num("batch_size", 64), int(num("epochs", 20))
    ep = max(1, min(ep, 200))
    r = _rng("run", sorted((str(k), str(v)) for k, v in c.items()))
    lr_fit = math.exp(-((math.log10(max(lr, 1e-8)) + 3) ** 2) / 2)  # best near 1e-3
    bs_fit = math.exp(-((math.log2(max(bs, 1)) - 6.5) ** 2) / 18)    # best near 64-128
    ceiling = 0.72 + 0.16 * lr_fit + 0.04 * bs_fit + (r.random() - 0.5) * 0.012
    n = min(ep, 25)
    curve = [round(_clamp(ceiling * (1 - math.exp(-5 * (i + 1) / n)) + (r.random() - 0.5) * 0.01), 4) for i in range(n)]
    return {"metric": curve[-1], "curve": curve, "runtime_s": round(ep * (3 + r.random() * 2), 1)}


def audit(text, analysis):
    low = (text or "").lower()
    p = (analysis or {}).get("parameters", {})
    kw = (analysis or {}).get("keywords", {})
    F = []

    def add(cat, desc, sev, ev, nxt):
        F.append({"category": cat, "description": desc, "severity": sev, "status": "Needs human review",
                  "evidence": ev, "next": nxt, "source": "mock-ai"})

    if not low.strip():
        add("Incomplete evidence", "No text could be extracted, so the paper cannot be audited.", "High",
            "Mock audit: empty text", "Provide a text-based PDF")
        return F
    if not p.get("random seed"):
        add("Randomness", "Random seed is not reported; results may vary between runs.", "Medium",
            "Mock audit: no seed found", "Fix a seed and re-run several times")
    if "augmentation" in kw and "split" not in low:
        add("Possible data leakage", "Augmentation is mentioned but the train/test split procedure is not described.",
            "High", "Mock audit: 'augmentation' without 'split'", "Check that the split happens before augmentation")
    if "baseline" not in kw:
        add("Missing baseline models", "No baseline comparison found in the text.", "Medium",
            "Mock audit: no 'baseline'", "Add a simple baseline run")
    if not p.get("epochs") and not p.get("batch size"):
        add("Missing methodology details", "Neither epochs nor batch size is stated.", "Medium",
            "Mock audit: parameters missing", "Request training details from the authors")
    if not re.search(r"github\.com|gitlab\.com|code (is )?available", low):
        add("Artifact unavailable", "No code repository link found.", "Low",
            "Mock audit: no repository link", "Search for an official implementation")
    return F
