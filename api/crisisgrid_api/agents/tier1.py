"""Tier 1 end to end: paper PDF -> claims, gap list, artifact inventory. Needs no code, data or compute."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from crisisgrid_core.harness import HarnessGuard
from crisisgrid_core.models import Gap
from crisisgrid_core.papers import ArtifactInventory, ParsedPaper

from ..gateway.gateway import Gateway
from ..ingest.embed import Embedder
from ..ingest.pdf import IngestedPaper, ingest_pdf
from .artifact_finder import RepoFetcher, find_artifacts
from .gap_auditor import GapAuditorAgent
from .paper_parser import PaperParserAgent


class Tier1Result(BaseModel):
    paper_hash: str
    needs_ocr: bool = False
    parsed: ParsedPaper | None = None
    inventory: ArtifactInventory | None = None
    gaps: list[Gap] = []


def run_tier1(pdf: bytes, *, gateway: Gateway, guard: HarnessGuard, embedder: Embedder, fetcher: RepoFetcher,
              run_id: str | None = None, paper_date: date | None = None, ingested: IngestedPaper | None = None) -> Tier1Result:
    paper = ingested or ingest_pdf(pdf)
    if paper.needs_ocr:  # say so instead of "analysing" an empty paper
        return Tier1Result(paper_hash=paper.paper_hash, needs_ocr=True)
    parsed = PaperParserAgent(gateway, guard, embedder).parse(paper.pages, run_id=run_id)
    inventory = find_artifacts(paper.text, parsed, fetcher, paper_date=paper_date or parsed.published or paper.metadata_date)
    gaps = GapAuditorAgent(gateway, guard).audit(parsed, inventory, paper.text, run_id=run_id)
    return Tier1Result(paper_hash=paper.paper_hash, parsed=parsed, inventory=inventory, gaps=gaps)
