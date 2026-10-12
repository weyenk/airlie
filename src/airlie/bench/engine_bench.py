"""`task bench`: throughput of any engine through the seam.

Nonce-first prompts defeat prefix caching so prefill is really measured; the `resume` case measures the
opposite, that turn 2 of a conversation *does* hit the cache."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from airlie.config import Sampling
from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, Message, TokenEvent

CHARS_PER_TOKEN = 4
SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "__pycache__", "runs"}


@dataclass(frozen=True)
class BenchCase:
    role: str
    ctx: int
    concurrency: int
    gen: int
    resume: bool = False


@dataclass
class ResumeResult:
    turn1_prompt_tokens: int
    turn1_ttft_ms: float
    turn2_prompt_tokens: int
    turn2_ttft_ms: float

    @property
    def ttft_ratio(self) -> float:
        return self.turn2_ttft_ms / self.turn1_ttft_ms if self.turn1_ttft_ms else 0.0


@dataclass
class BenchResult:
    model: str
    role: str
    ctx: int
    concurrency: int
    gen: int
    runs: int
    ttft_ms: float
    prefill_tps: float
    decode_tps_per_stream: float
    agg_decode_tps: float
    prompt_tokens: int
    completion_tokens: int
    rss_mb: float | None
    resume: ResumeResult | None = None
    short_outputs: int = field(default=0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _corpus_text(corpus_dir: Path | None, seed: int) -> str:
    rng = random.Random(seed)
    files: list[Path] = []
    if corpus_dir and corpus_dir.is_dir():
        for p in sorted(corpus_dir.rglob("*")):
            if (
                p.is_file()
                and p.suffix in {".py", ".md", ".yaml", ".toml", ".txt"}
                and not (set(p.parts) & SKIP_DIRS)
            ):
                files.append(p)
    rng.shuffle(files)
    parts: list[str] = []
    for p in files[:50]:
        try:
            parts.append(p.read_text(errors="replace"))
        except OSError:
            continue
    text = "\n".join(parts)
    if len(text) < 2000:
        words = [
            "engine",
            "memory",
            "team",
            "scheduler",
            "cache",
            "token",
            "agent",
            "budget",
            "prefill",
            "decode",
        ]
        text = " ".join(rng.choice(words) for _ in range(20000))
    return text


def build_prompt(n_tokens: int, *, seed: int, corpus_dir: Path | None) -> str:
    """Nonce first, then repo text until roughly n_tokens (4 chars/token).

    The server's reported prompt_tokens is the truth; this only sizes the prompt."""
    rng = random.Random(seed)
    nonce = f"nonce-{rng.getrandbits(64):016x}"
    budget = max(n_tokens * CHARS_PER_TOKEN - len(nonce) - 80, 0)
    body = _corpus_text(corpus_dir, seed)
    start = rng.randrange(0, max(len(body) - budget, 1)) if len(body) > budget else 0
    chunk = body[start : start + budget]
    return f"{nonce}\nRead the following material carefully, then write a long, detailed summary.\n\n{chunk}"


async def _one(engine: Engine, req: GenRequest) -> tuple[float, float, int, int, str]:
    first: float | None = None
    t0 = time.monotonic()

    def on_token(ev: TokenEvent) -> None:
        nonlocal first
        if first is None:
            first = time.monotonic()

    res = await engine.generate(req, on_token=on_token)
    ttft = ((first or time.monotonic()) - t0) * 1000.0
    total = (time.monotonic() - t0) * 1000.0
    return ttft, total, res.prompt_tokens, res.completion_tokens, res.text


async def run_case(
    engine: Engine,
    case: BenchCase,
    *,
    sampling: Sampling,
    corpus_dir: Path | None,
    runs: int = 1,
    model: str = "",
) -> BenchResult:
    ttfts: list[float] = []
    totals: list[float] = []
    ptoks = ctoks = 0
    short = 0
    resume: ResumeResult | None = None
    for run in range(runs):
        reqs = [
            GenRequest(
                role=case.role,
                messages=[
                    Message(
                        role="user", content=build_prompt(case.ctx, seed=run * 100 + i, corpus_dir=corpus_dir)
                    )
                ],
                sampling=sampling,
                max_tokens=case.gen,
                stop=[],
                thinking=False,
            )
            for i in range(case.concurrency)
        ]
        t0 = time.monotonic()
        outs = await asyncio.gather(*(_one(engine, r) for r in reqs))
        wall = time.monotonic() - t0
        for ttft, total, pt, ct, _ in outs:
            ttfts.append(ttft)
            totals.append(total)
            ptoks += pt
            ctoks += ct
            if ct < 0.8 * case.gen:
                short += 1
        if case.resume:
            r1 = reqs[0]
            t1 = outs[0]
            follow = GenRequest(
                role=case.role,
                messages=[
                    *r1.messages,
                    Message(role="assistant", content=t1[4]),
                    Message(role="user", content="Continue."),
                ],
                sampling=sampling,
                max_tokens=case.gen,
                stop=[],
                thinking=False,
            )
            ttft2, _, pt2, _, _ = await _one(engine, follow)
            resume = ResumeResult(t1[2], t1[0], pt2, ttft2)
        del wall
    n = max(len(ttfts), 1)
    mean_ttft = sum(ttfts) / n
    mean_total = sum(totals) / n
    decode_ms = max(mean_total - mean_ttft, 1e-3)
    per_stream_tokens = ctoks / n
    decode_tps = per_stream_tokens / (decode_ms / 1000.0)
    prompt_per = ptoks / n
    prefill_tps = prompt_per / (mean_ttft / 1000.0) if mean_ttft > 0 else 0.0
    return BenchResult(
        model=model,
        role=case.role,
        ctx=case.ctx,
        concurrency=case.concurrency,
        gen=case.gen,
        runs=runs,
        ttft_ms=round(mean_ttft, 1),
        prefill_tps=round(prefill_tps, 1),
        decode_tps_per_stream=round(decode_tps, 2),
        agg_decode_tps=round(decode_tps * case.concurrency, 2),
        prompt_tokens=ptoks,
        completion_tokens=ctoks,
        rss_mb=None,
        resume=resume,
        short_outputs=short,
    )


async def run_matrix(
    engine: Engine,
    *,
    role: str,
    ctxs: list[int],
    ns: list[int],
    gen: int,
    runs: int,
    sampling: Sampling,
    corpus_dir: Path | None,
    model: str = "",
    resume: bool = False,
) -> list[BenchResult]:
    out: list[BenchResult] = []
    for ctx in ctxs:
        for n in ns:
            out.append(
                await run_case(
                    engine,
                    BenchCase(role, ctx, n, gen, resume=resume and n == 1),
                    sampling=sampling,
                    corpus_dir=corpus_dir,
                    runs=runs,
                    model=model,
                )
            )
    return out


def save(results: list[BenchResult], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.to_dict() for r in results], indent=1))
    return path


def compare(a: Path, b: Path) -> list[dict[str, Any]]:
    ra = {(r["ctx"], r["concurrency"]): r for r in json.loads(a.read_text())}
    rb = {(r["ctx"], r["concurrency"]): r for r in json.loads(b.read_text())}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(ra) & set(rb)):
        x, y = ra[key], rb[key]
        row: dict[str, Any] = {"ctx": key[0], "concurrency": key[1], "a": x["model"], "b": y["model"]}
        for m in ("ttft_ms", "decode_tps_per_stream", "agg_decode_tps", "prefill_tps"):
            base = x[m] or 0.0
            row[f"{m}_delta_pct"] = round(((y[m] or 0.0) - base) / base * 100.0, 1) if base else None
        rows.append(row)
    return rows
