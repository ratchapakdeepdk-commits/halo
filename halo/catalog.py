"""Curated local models to choose from at install time.

Sizes are real download sizes from the Ollama registry (Q4 quantisation). `tested` holds
results measured with HALO's own tests on the maintainer's machine (2x P100):
  code   - bundled coding benchmark (bench/, generate-verify loop): passed / attempts
  digest - known-answer log test (halo tune): correct or not
Untested entries say so; run `halo tune` after downloading to measure on *your* machine.
"""
from dataclasses import dataclass, field


@dataclass
class Model:
    name: str
    size_gib: float           # download / weights size
    good_at: str              # "digest", "code" or "both"
    note: str
    thai: bool = True         # usable for Thai prompts/output
    moe: bool = False         # mixture-of-experts: fast for its size
    tested: dict = field(default_factory=dict)

    @property
    def need_gib(self) -> float:
        """Memory to run with an 8k context: weights + KV cache/overhead."""
        return self.size_gib * 1.08 + 1.2


CATALOG = [
    Model("qwen3.6:35b-a3b-q4_K_M", 22.3, "code",
          "Best coder we tested; digest also fine but slower prefill.", moe=True,
          tested={"code": "13/14", "digest": "correct"}),
    Model("qwen3-coder:30b", 17.3, "code",
          "Qwen's coding model, MoE; fast for its size.", moe=True),
    Model("qwen3:30b-a3b-instruct-2507-q4_K_M", 17.3, "digest",
          "Best log/file reader we tested: fast, accurate, good Thai.", moe=True,
          tested={"code": "9/14", "digest": "correct"}),
    Model("gpt-oss:20b", 12.8, "both",
          "OpenAI open-weight MoE; fastest prefill on big inputs; weaker Thai.",
          thai=False, moe=True, tested={"code": "3/3 hard tasks"}),
    Model("devstral:24b", 13.3, "code",
          "Mistral's agentic-coding model; dense, so slower per token.", thai=False),
    Model("gemma3:27b", 16.2, "digest", "Google Gemma 3; strong multilingual reading, dense."),
    Model("qwen2.5-coder:14b", 8.4, "code", "Solid coder for 12-16 GB cards."),
    Model("qwen3:14b", 8.6, "both", "All-rounder for 12-16 GB cards."),
    Model("phi4:14b", 8.4, "code", "Microsoft Phi-4; good reasoning, English-centric.",
          thai=False),
    Model("gemma3:12b", 7.6, "digest", "Good multilingual reader for 10-12 GB cards."),
    Model("qwen3:8b", 4.9, "both", "Best small all-rounder for 8 GB cards."),
    Model("qwen2.5-coder:7b", 4.4, "code", "Small coder for 6-8 GB cards."),
    Model("gemma3:4b", 3.1, "digest", "Light reader for laptops."),
    Model("qwen3:4b-instruct", 2.3, "digest",
          "Light reader for laptops; too weak for code.",
          tested={"code": "0/3", "digest": "wrong (timestamp)"}),
    Model("qwen3:1.7b", 1.3, "digest", "CPU-only / very small machines; summaries only."),
]


def fitting(budget_gib: float, cpu_only: bool = False) -> list[Model]:
    """Models that fit the budget. CPU-only machines get small models only (speed)."""
    limit = min(budget_gib, 9.0) if cpu_only else budget_gib
    return [m for m in CATALOG if m.need_gib <= limit]


def recommended(budget_gib: float, cpu_only: bool = False) -> list[Model]:
    """Default pick: the best digest model and the best code model that fit (may be one)."""
    fits = fitting(budget_gib, cpu_only)
    if not fits:
        return []
    digest = next((m for m in fits if m.good_at in ("digest", "both")), fits[0])
    code = next((m for m in fits if m.good_at in ("code", "both")), digest)
    picks = [digest] if code is digest else [digest, code]
    # Two big models that cannot both stay loaded still work (Ollama swaps them), but on
    # small budgets prefer one all-rounder to avoid constant reloading.
    if len(picks) == 2 and sum(m.need_gib for m in picks) > budget_gib and budget_gib < 20:
        allround = next((m for m in fits if m.good_at == "both"), None)
        if allround:
            picks = [allround]
    return picks


def by_name(name: str) -> Model | None:
    return next((m for m in CATALOG if m.name == name), None)


def describe(m: Model) -> str:
    tags = [m.good_at if m.good_at != "both" else "digest+code"]
    if m.moe:
        tags.append("MoE")
    if not m.thai:
        tags.append("weak Thai")
    t = ", ".join(f"{k} {v}" for k, v in m.tested.items()) if m.tested else "untested"
    return f"{', '.join(tags)} | tested: {t} | {m.note}"
