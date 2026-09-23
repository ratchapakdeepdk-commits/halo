"""Hardware/backend checks and model recommendation.

Lessons baked in from real deployments: a wrong driver can silently push Ollama onto a slow
fallback backend (Vulkan/CPU) that still "works" but prefills 15-20x slower, so we measure
throughput instead of trusting that the GPU is being used.
"""
import os
import platform
import re
import shutil
import subprocess
import time
import uuid

from . import llm
from .config import Config

# (min total VRAM GiB, digest model, code model, note). First match wins. Measured on the
# bundled benchmark (bench/): for code, qwen3.6:35b-a3b passed 13/14 vs 9/14 for
# qwen3:30b-a3b, which in turn digests faster (prefill ~480 vs ~270 tok/s on P100).
RECOMMENDATIONS = [
    (44, "qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3.6:35b-a3b-q4_K_M",
     "both stay resident"),
    (24, "qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3.6:35b-a3b-q4_K_M",
     "models swap between digest and code (~15 s reload from fast disk)"),
    (13, "gpt-oss:20b", "gpt-oss:20b", "MoE ~13 GB; fastest prefill, weaker non-English"),
    (9, "qwen3:14b", "qwen3:14b", "dense ~9 GB, untested with HALO"),
    (4, "qwen3:4b-instruct", "qwen3:4b-instruct", "~2.5 GB; digest only, weak at code"),
]
SLOW_PREFILL = 150  # tok/s; below this, digesting a long log takes minutes


def gpus() -> list[tuple[str, int]]:
    """[(name, MiB)] from nvidia-smi; empty if not available (AMD/Apple not detected yet)."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    res = []
    for line in out.strip().splitlines():
        name, _, mib = line.rpartition(",")
        try:
            res.append((name.strip(), int(mib.strip())))
        except ValueError:
            pass
    return res


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def system_ram_gib() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        pass
    if platform.system() == "Windows":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"])
        try:
            return int(out.strip()) / 2**30
        except ValueError:
            return 0.0
    return 0.0


def accelerator() -> dict:
    """Memory a local model can use on this machine.

    NVIDIA/AMD: total VRAM. Apple Silicon: ~70% of unified memory (macOS keeps the rest).
    Otherwise CPU inference from system RAM (works, but prefill is slow).
    """
    g = gpus()
    if g:
        counts: dict = {}
        for n, m in g:
            counts[(n, m)] = counts.get((n, m), 0) + 1
        detail = ", ".join(f"{c}× {n}" if c > 1 else n for (n, _), c in counts.items())
        total = sum(m for _, m in g) / 1024
        return {"kind": "nvidia", "gib": total, "detail": f"{detail} ({total:.0f} GiB)"}
    if shutil.which("rocm-smi"):
        total = sum(int(x) for x in re.findall(r"VRAM Total Memory \(B\):\s*(\d+)",
                                                _run(["rocm-smi", "--showmeminfo", "vram"])))
        if total:
            return {"kind": "amd", "gib": total / 2**30, "detail": "AMD ROCm GPU(s)"}
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            mem = int(_run(["sysctl", "-n", "hw.memsize"]).strip()) / 2**30
        except ValueError:
            mem = 0.0
        return {"kind": "apple", "gib": mem * 0.7,
                "detail": f"Apple Silicon, {mem:.0f} GiB unified memory"}
    ram = system_ram_gib()
    return {"kind": "cpu", "gib": ram * 0.5, "detail": f"CPU only, {ram:.0f} GiB RAM (slow)"}


def recommend(vram_gib: float) -> tuple[str, str, str] | None:
    """(digest model, code model, note) for the given total VRAM."""
    for min_gib, model, code_model, note in RECOMMENDATIONS:
        if vram_gib >= min_gib:
            return model, code_model, note
    return None


def benchmark(cfg: Config, model: str) -> dict:
    """Measure prefill and generation speed with a unique prompt (a repeated prompt hits
    Ollama's prompt cache and reports absurd prefill numbers)."""
    nonce = uuid.uuid4().hex
    filler = " ".join(f"line {i}: sensor value {i * 7 % 13} nominal" for i in range(300))
    prompt = f"[{nonce}] {filler}\nHow many lines are there? Answer with one number."
    t0 = time.time()
    d = llm.raw(cfg, "/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                       "think": False, "options": {"num_predict": 16}},
                timeout=cfg.timeout)
    wall = time.time() - t0
    pe, pd = d.get("prompt_eval_count", 0), d.get("prompt_eval_duration", 0)
    ge, gd = d.get("eval_count", 0), d.get("eval_duration", 0)
    return {"prefill_tps": pe / (pd / 1e9) if pd else 0.0,
            "gen_tps": ge / (gd / 1e9) if gd else 0.0,
            "load_s": d.get("load_duration", 0) / 1e9, "wall_s": wall}


def run(cfg: Config, bench: bool = True) -> tuple[list[str], dict]:
    """Returns (report lines, facts). Never raises for expected problems."""
    lines, facts = [], {"ok": True}

    def bad(msg):
        facts["ok"] = False
        lines.append(f"  ✗ {msg}")

    g = gpus()
    vram = sum(m for _, m in g) / 1024
    facts["vram_gib"] = vram
    if g:
        for name, mib in g:
            lines.append(f"  ✓ GPU {name} ({mib / 1024:.0f} GiB)")
    else:
        lines.append("  ? no NVIDIA GPU detected (CPU-only works but is slow)")

    try:
        tags = llm.raw(cfg, "/api/tags")
        installed = [m["name"] for m in tags.get("models", [])]
        lines.append(f"  ✓ Ollama reachable at {cfg.ollama_url} ({len(installed)} models)")
    except llm.LocalModelError as e:
        bad(f"{e}\n    install Ollama from https://ollama.com and start it, or set "
            f"HALO_OLLAMA_URL")
        return lines, facts
    facts["installed"] = installed

    rec = recommend(vram) if g else None
    facts["recommended"] = rec[0] if rec else None
    if rec:
        lines.append(f"  · recommended for {vram:.0f} GiB VRAM: digest {rec[0]}, code {rec[1]} "
                     f"({rec[2]})")

    if cfg.model not in installed:
        bad(f"configured model {cfg.model} is not pulled — run: halo setup")
        return lines, facts
    lines.append(f"  ✓ configured model: {cfg.model}")
    for m in [cfg.code_model, *cfg.fallback_models]:
        if m and m != cfg.model:
            if m in installed:
                lines.append(f"  ✓ code/fallback model: {m}")
            else:
                bad(f"code/fallback model {m} is not pulled — run: ollama pull {m}")

    if bench:
        try:
            b = benchmark(cfg, cfg.model)
        except llm.LocalModelError as e:
            bad(f"benchmark failed: {e}")
            return lines, facts
        facts["bench"] = b
        lines.append(f"  · prefill {b['prefill_tps']:.0f} tok/s, generation {b['gen_tps']:.0f} "
                     f"tok/s (load {b['load_s']:.1f}s)")
        try:
            ps = llm.raw(cfg, "/api/ps").get("models", [])
            m = next((x for x in ps if x.get("name") == cfg.model), None)
            if m and m.get("size") and m.get("size_vram", 0) < m["size"]:
                pct = 100 * (1 - m["size_vram"] / m["size"])
                bad(f"{pct:.0f}% of the model is running on CPU — pick a smaller model")
        except llm.LocalModelError:
            pass
        if b["prefill_tps"] and b["prefill_tps"] < SLOW_PREFILL:
            bad(f"prefill below {SLOW_PREFILL} tok/s: Ollama may be on a fallback backend "
                f"(check `ollama serve` logs for 'library=' — expect cuda/rocm/metal, "
                f"an outdated GPU driver is the usual cause)")
    return lines, facts
