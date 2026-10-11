"""Paper parser agent: paper text -> ParsedPaper. Every stated fact must carry a quote; quotes are verified against the
paper afterwards (enforce_sources), so a hallucinated hyperparameter cannot survive."""
from __future__ import annotations

from crisisgrid_core.harness import HarnessGuard
from crisisgrid_core.papers import ParsedPaper, enforce_sources

from ..gateway.gateway import Gateway
from ..gateway.prompts import SYSTEM_RULES, untrusted_block
from ..gateway.structured import run_structured
from ..gateway.types import Message
from ..ingest.chunk import chunk_pages
from ..ingest.embed import Embedder, InMemoryVectorIndex

INSTRUCTIONS = """You extract the experimental setup of an ML paper into the given schema.
Rules:
- Every value you mark provenance "stated" MUST have source.quote: a verbatim excerpt (<=200 chars) from the paper.
- If you only infer a value, use provenance "inferred" and no quote. If the paper does not say it, omit it. Never guess.
- hyperparameter keys: learning_rate, batch_size, epochs, optimizer, weight_decay, momentum, ... (snake_case).
- For each claim: metric, dataset, split, reported_value, unit, a tolerance (use the paper's std if given, else 0.5 for percent,
  0.005 for fractions), and the exact sentence as source.quote.
- summary: 4 to 6 plain sentences for a non-expert: the problem, the idea, how it was tested, the main result. method: 3 to 5
  sentences on how the approach works (architecture, training procedure). Both are your own words and need no quote; state only
  what the paper says. Keep each under 1200 characters.
- If two places in the paper disagree, record a contradiction with BOTH spans; do not pick one."""

QUERIES = [
    "hyperparameters learning rate batch size epochs optimizer weight decay",
    "dataset train validation test split preprocessing augmentation",
    "results accuracy table comparison baseline state of the art",
    "random seed runs standard deviation variance hardware GPU training time",
    "evaluation metric top-1 top-5 F1 macro micro",
]


class PaperParserAgent:
    def __init__(self, gateway: Gateway, guard: HarnessGuard, embedder: Embedder, *, max_context_chars: int = 24_000) -> None:
        self.gateway, self.guard, self.embedder, self.max_chars = gateway, guard, embedder, max_context_chars

    def context(self, pages: list[str]) -> str:
        """Whole paper if it fits; otherwise the opening (title/abstract) plus the chunks that best match the questions
        we need answered, in reading order."""
        full = "\n\n".join(pages)
        if len(full) <= self.max_chars:
            return full
        chunks = chunk_pages(pages)
        index = InMemoryVectorIndex(self.embedder)
        index.add(chunks)
        # priority order: the opening chunk, then the best matches per question (round-robin). Fill the budget by
        # priority and only then restore reading order, so early filler can never crowd out the relevant passages.
        ranked = [index.search(q, k=4) for q in QUERIES]
        priority = [0] + [r[i].index for i in range(4) for r in ranked if i < len(r)]
        picked: list[int] = []
        size = 0
        for i in dict.fromkeys(priority):
            if size + len(chunks[i].text) <= self.max_chars:
                picked.append(i)
                size += len(chunks[i].text)
        out = []
        for i in sorted(picked):
            c = chunks[i]
            out.append(f"[pages {c.page_start}-{c.page_end}] {c.text}")
        return "\n\n".join(out)

    def parse(self, pages: list[str], *, run_id: str | None = None, hint: str | None = None) -> ParsedPaper:
        full_text = "\n\n".join(pages)
        msgs = [
            Message(role="system", content=SYSTEM_RULES + "\n" + INSTRUCTIONS),
            Message(role="user", content=untrusted_block("paper", self.context(pages))),
        ]
        if hint:
            msgs.append(Message(role="user", content=f"Reviewer hint (from the human operator; schema and quotes still apply): {hint}"))
        self.guard.start_stage("understand")
        parsed = run_structured(self.gateway, self.guard, "understand", "paper_parser", msgs, ParsedPaper, run_id=run_id)
        return enforce_sources(parsed, full_text)  # quotes are checked against the WHOLE paper, not just the excerpt
