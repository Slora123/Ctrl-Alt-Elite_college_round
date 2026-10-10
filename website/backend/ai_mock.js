import crypto from 'crypto';

export const PROVIDER = "mock";
const METRIC_WORDS = ["accuracy", "f1", "precision", "recall", "auc", "silhouette"];

function _rng(...parts) {
  const str = parts.map(String).join('|');
  const hash = crypto.createHash('sha256').update(str).digest('hex').substring(0, 12);
  let seed = parseInt(hash, 16);
  return () => {
    let t = seed += 0x6D2B79F5;
    t = Math.imul(t ^ t >>> 15, t | 1);
    t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

function _clamp(x, lo = 0.0, hi = 0.999) {
  return Math.max(lo, Math.min(hi, x));
}

function choice(arr, rng) {
  return arr[Math.floor(rng() * arr.length)];
}

export function understand(title, text, analysis) {
  const rng = _rng("understand", title, (text || "").length);
  const params = (analysis || {}).parameters || {};
  const kw = (analysis || {}).keywords || {};
  let family = ["transformer", "cnn", "resnet", "bert", "lstm"].filter(k => kw[k]);
  if (family.length === 0) family = ["unspecified"];
  
  const missing = Object.entries(params).filter(([k, v]) => !v).map(([k]) => k);
  const t = text || "";
  const m = t.match(/abstract\s*[:\-]?\s*([^]{80,600}?)(?:\n\s*\n|\n\s*1\.?\s|$)/i);
  const snippet = (m ? m[1] : t.substring(0, 400)).replace(/\s+/g, ' ').trim();
  let summary = snippet.length > 420 ? snippet.substring(0, 420) + "…" : snippet;
  
  if (!summary) {
    summary = "No extractable text, so a summary cannot be produced for this PDF (it may be scanned).";
  }
  
  const points = [
    `Model family detected: ${family.join(', ')}.`,
    `${Object.keys(params).length - missing.length} of ${Object.keys(params).length} training parameters were stated in the text.`
  ];
  if (missing.length > 0) points.push(`Not specified: ${missing.join(', ')}.`);
  if (!kw["baseline"]) points.push("No baseline comparison is mentioned.");
  
  return {
    provider: PROVIDER,
    summary,
    key_points: points,
    model_family: family,
    missing,
    confidence: Math.round((0.6 + rng() * 0.3) * 100) / 100
  };
}

export function extract_claims(text, title = "") {
  let out = [];
  const sentences = (text || "").split(/(?<=[.!?])\s+|\n/);
  for (let s of sentences) {
    const w = new RegExp("\\b(" + METRIC_WORDS.join("|") + ")\\b", "i").test(s);
    if (!w) continue;
    const n = s.match(/(\d{1,3}(?:\.\d+)?)\s*%/) || s.match(/(?<![\d.])(0?\.\d+)(?!\d)/);
    if (!n) continue;
    let v = parseFloat(n[1]);
    if (n[0].includes('%')) v = v / 100;
    if (v > 0 && v < 1) {
      out.push([s.replace(/\s+/g, ' ').trim().substring(0, 200), Math.round(v * 10000) / 10000]);
    }
    if (out.length >= 5) break;
  }
  
  if (out.length === 0) {
    const rng = _rng("claims", title);
    out = [["Primary reported metric (placeholder claim from mock extractor)", Math.round((0.80 + rng() * 0.15) * 1000) / 1000]];
  }
  return out;
}

export function reproduce_claim(statement, reported, salt = 0) {
  const rng = _rng("repro", statement, reported, salt);
  const gap = choice([0.002, 0.004, 0.006, 0.012, 0.02, 0.035], rng) * (rng() < 0.85 ? 1 : -1);
  return {
    reproduced: Math.round(_clamp(parseFloat(reported) - gap) * 10000) / 10000,
    runtime_s: Math.round((40 + rng() * 260) * 10) / 10,
    note: "Simulated reproduction. No experiment was executed."
  };
}

export function run_experiment(config) {
  const c = config || {};
  function num(k, d) {
    const v = c[k];
    if (v === null || v === undefined || v === "") return d;
    const p = parseFloat(v);
    return isNaN(p) ? d : p;
  }
  
  const lr = num("learning_rate", 1e-3);
  const bs = num("batch_size", 64);
  let ep = Math.max(1, Math.min(Math.floor(num("epochs", 20)), 200));
  
  const sortedEntries = Object.entries(c).sort((a, b) => a[0].localeCompare(b[0])).map(([k,v]) => String(k) + String(v));
  const rng = _rng("run", ...sortedEntries);
  
  const lr_fit = Math.exp(-Math.pow(Math.log10(Math.max(lr, 1e-8)) + 3, 2) / 2);
  const bs_fit = Math.exp(-Math.pow(Math.log2(Math.max(bs, 1)) - 6.5, 2) / 18);
  const ceiling = 0.72 + 0.16 * lr_fit + 0.04 * bs_fit + (rng() - 0.5) * 0.012;
  
  const n = Math.min(ep, 25);
  const curve = [];
  for (let i = 0; i < n; i++) {
    curve.push(Math.round(_clamp(ceiling * (1 - Math.exp(-5 * (i + 1) / n)) + (rng() - 0.5) * 0.01) * 10000) / 10000);
  }
  
  return {
    metric: curve[curve.length - 1],
    curve,
    runtime_s: Math.round(ep * (3 + rng() * 2) * 10) / 10
  };
}

export function audit(text, analysis) {
  const low = (text || "").toLowerCase();
  const p = (analysis || {}).parameters || {};
  const kw = (analysis || {}).keywords || {};
  const F = [];
  
  function add(cat, desc, sev, ev, nxt) {
    F.push({
      category: cat, description: desc, severity: sev,
      status: "Needs human review", evidence: ev, next: nxt, source: "mock-ai"
    });
  }
  
  if (!low.trim()) {
    add("Incomplete evidence", "No text could be extracted, so the paper cannot be audited.", "High", "Mock audit: empty text", "Provide a text-based PDF");
    return F;
  }
  if (!p["random seed"]) {
    add("Randomness", "Random seed is not reported; results may vary between runs.", "Medium", "Mock audit: no seed found", "Fix a seed and re-run several times");
  }
  if (kw["augmentation"] && !low.includes("split")) {
    add("Possible data leakage", "Augmentation is mentioned but the train/test split procedure is not described.", "High", "Mock audit: 'augmentation' without 'split'", "Check that the split happens before augmentation");
  }
  if (!kw["baseline"]) {
    add("Missing baseline models", "No baseline comparison found in the text.", "Medium", "Mock audit: no 'baseline'", "Add a simple baseline run");
  }
  if (!p["epochs"] && !p["batch size"]) {
    add("Missing methodology details", "Neither epochs nor batch size is stated.", "Medium", "Mock audit: parameters missing", "Request training details from the authors");
  }
  if (!/github\.com|gitlab\.com|code (is )?available/.test(low)) {
    add("Artifact unavailable", "No code repository link found.", "Low", "Mock audit: no repository link", "Search for an official implementation");
  }
  
  return F;
}
