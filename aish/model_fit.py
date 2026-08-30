"""Whether a local Ollama model can actually run on THIS machine.

Ollama does not refuse a model that is too large. It computes the fit, finds
it does not fit, and starts anyway with the surplus layers on the CPU::

    msg="model requires more gpu memory than is currently available, evicting..."
    msg="offloaded 35/41 layers to GPU"
    msg="model weights" device=CPU size="2.0 GiB"

On 2026-08-27 that is what qwen3:14b did on a 16 GB M4 that also carries a
Home Assistant VM and Colima: the runner started, the machine thrashed, and
seventeen minutes later Ollama died mid-answer. The chat had been given the
model by aish's own picker, which offered every model `ollama list` returned.
So the guard has to be here; there is no upstream one to lean on.

The quantity that decides is whether the model fits ENTIRELY in the GPU
working set. Free memory does not decide it: across every load Ollama has
logged on that machine, "system memory free" reads 3.8-4.8 GiB whether the
load is a 4B model that works or the 14B one that took the machine down —
macOS compresses on demand rather than leaving memory idle, so the number is
flat regardless of what is about to happen.

Scope: unified-memory Macs only. On a discrete GPU the model's budget is VRAM,
which is not a fraction of system RAM, and no incident here established what
that budget should be — so `check` returns None (no opinion) and nothing is
hidden. That is deliberate: a fit rule guessing at hardware it has never been
run on would hide working models and claim a reason it did not check.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass

# Share of physical RAM a model may occupy. Metal reports the real ceiling as
# `recommendedMaxWorkingSetSize` — 12713 MB of 16384 MB on the M4 above, or
# 77.6% — but reading it needs pyobjc, so this sits just under it. Being
# wrong-low hides a model that would have run; being wrong-high is the crash.
GPU_SHARE = 0.72

# Ollama's default KV cache is f16 and it does not quantize it unless told to
# (OLLAMA_KV_CACHE_TYPE unset). Two bytes per element, one K and one V.
KV_BYTES_PER_ELEMENT = 2

# Below this a "it would fit at a smaller context" suggestion is not worth
# making — aish's own history trimming assumes a window it can work in.
MIN_USEFUL_NUM_CTX = 8192

# aish's own default context. `make_chat` checks against this when a caller
# does not say, so the fence is never silently OFF for a path that forgot to
# thread num_ctx through — being slightly wrong beats not checking.
DEFAULT_NUM_CTX = 32768

# GGUF metadata is immutable per blob, so a digest that has been read once
# never needs reading again. Without this the picker pays an `ollama show`
# per model every time it opens.
_INFO_CACHE: dict[str, dict] = {}

# config.toml's `model_memory_gb`, when the owner has measured their own
# machine and wants to say so outright. Module state because it is a fact
# about the HARDWARE, one per process, read once at startup — threading it
# through make_chat and every picker call would put a machine constant in
# six signatures that have nothing else to do with it.
_MEMORY_CAP: int | None = None


def configure(memory_gb: float | None) -> None:
    """Set an absolute ceiling on what a local model may occupy, in GB,
    overriding the share of RAM. None restores the default."""
    global _MEMORY_CAP
    _MEMORY_CAP = int(memory_gb * 1e9) if memory_gb and memory_gb > 0 else None


@dataclass(frozen=True)
class Verdict:
    """What a model needs, what the machine can give it, and the parts —
    every number in the refusal comes from here, so the sentence aish prints
    cannot say more than was actually computed."""

    model: str
    fits: bool
    weights: int
    kv: int
    budget: int
    num_ctx: int
    largest_num_ctx: int  # 0 when no context size fits

    @property
    def needed(self) -> int:
        return self.weights + self.kv


def unified_memory() -> bool:
    """True on the hardware this rule was established on: Apple Silicon,
    where the GPU and the rest of the machine spend the same RAM."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def total_ram() -> int:
    """Physical RAM in bytes."""
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def kv_bytes_per_token(model_info: dict) -> int:
    """Bytes of KV cache one token costs, from the GGUF metadata `ollama show`
    returns. Zero when the metadata is not there to read — the caller then has
    no footprint and states no opinion, rather than filling the gap with a
    guess about a model it could not measure.

    Checked against Ollama's own accounting for qwen3:14b at num_ctx 32768:
    this arithmetic gives 5.0 GiB, and the runner logged `kv cache 5.0 GiB`.
    """
    arch = model_info.get("general.architecture")
    if not arch:
        return 0

    def field(name: str) -> int:
        try:
            return int(model_info.get(f"{arch}.{name}") or 0)
        except (TypeError, ValueError):
            return 0

    layers = field("block_count")
    heads = field("attention.head_count")
    kv_heads = field("attention.head_count_kv") or heads
    key_length = field("attention.key_length")
    value_length = field("attention.value_length") or key_length
    if not key_length and heads:
        # Older conversions omit key_length; it is the head dimension.
        key_length = value_length = field("embedding_length") // heads
    if not (layers and kv_heads and key_length and value_length):
        return 0
    return layers * kv_heads * (key_length + value_length) * KV_BYTES_PER_ELEMENT


def budget(*, ram: int, vram_in_use: int = 0, share: float = GPU_SHARE) -> int:
    """Bytes a model may occupy on this machine.

    `vram_in_use` is what other models Ollama already has resident are holding
    (`ollama ps` reports it per model). It is subtracted because it is real,
    measured, and contended — aish loads an embedding model of its own during
    retrieval, so "nothing else is running" is not the usual case.

    Ordinary process memory is NOT subtracted on top of that, and the omission
    is deliberate rather than forgotten. macOS exposes no number that predicts
    what another process truly needs resident: the VM guests on the machine
    this rule came from hold their memory as ordinary swappable anonymous
    pages, indistinguishable from a browser's. Subtracting the one figure that
    IS unswappable (wired, ~2 GB there) on top of the 28% this share already
    reserves would double-count it and hide qwen3:8b, which runs fine. What
    the share does not cover, `model_memory_gb` in config.toml does.
    """
    ceiling = _MEMORY_CAP if _MEMORY_CAP is not None else int(ram * share)
    return max(0, ceiling - vram_in_use)


def largest_num_ctx(weights: int, per_token: int, allowance: int) -> int:
    """The biggest context that would fit, rounded down to a power of two.
    Zero when the weights alone overrun the budget, or when what is left only
    buys a context too small to work in."""
    spare = allowance - weights
    if spare <= 0 or per_token <= 0:
        return 0
    tokens = spare // per_token
    fitting = 1 << (int(tokens).bit_length() - 1) if tokens else 0
    return fitting if fitting >= MIN_USEFUL_NUM_CTX else 0


def verdict(entry, info: dict, num_ctx: int, allowance: int) -> Verdict | None:
    """The whole decision, as a pure function of what was measured. None when
    the inputs do not answer the question — see `check`."""
    weights = int(getattr(entry, "size", 0) or 0)
    per_token = kv_bytes_per_token(info)
    if not weights or not per_token or num_ctx <= 0:
        return None
    kv = per_token * num_ctx
    return Verdict(
        model=entry.model,
        fits=weights + kv <= allowance,
        weights=weights,
        kv=kv,
        budget=allowance,
        num_ctx=num_ctx,
        largest_num_ctx=largest_num_ctx(weights, per_token, allowance),
    )


def _ollama_calls(show, listed, running):
    if show and listed and running:
        return show, listed, running
    import ollama

    return (
        show or ollama.show,
        listed or (lambda: ollama.list().models),
        running or (lambda: ollama.ps().models),
    )


def _info(entry, show) -> dict:
    digest = getattr(entry, "digest", "") or entry.model
    if digest not in _INFO_CACHE:
        _INFO_CACHE[digest] = dict(show(entry.model).modelinfo or {})
    return _INFO_CACHE[digest]


def verdicts(
    num_ctx: int,
    *,
    show=None,
    listed=None,
    running=None,
    ram: int | None = None,
    share: float = GPU_SHARE,
) -> dict[str, Verdict]:
    """{model name: Verdict} for every installed model the machine can be
    asked about — one round of Ollama calls for the whole picker. Models with
    no verdict are simply absent, so a caller iterating this cannot mistake
    "not measured" for "does not fit"."""
    if not unified_memory() or num_ctx <= 0:
        return {}
    try:
        # Inside the try with everything else: resolving the seams imports and
        # reads attributes off the ollama module, which is itself a way for
        # this to fail, and no failure here may reach the picker.
        show, listed, running = _ollama_calls(show, listed, running)
        entries = list(listed())
        resident = {
            m.model: int(getattr(m, "size_vram", 0) or 0) for m in running()
        }
        held = sum(resident.values())
        machine_ram = total_ram() if ram is None else ram
        found = {}
        for entry in entries:
            # A model already resident is not competing with itself: what it
            # holds is subtracted for every OTHER model and not for its own,
            # or the model you are running would report that it does not fit.
            others = held - resident.get(entry.model, 0)
            allowance = budget(ram=machine_ram, vram_in_use=others, share=share)
            try:
                result = verdict(entry, _info(entry, show), num_ctx, allowance)
            except Exception:
                continue  # one unreadable model must not blank the picker
            if result is not None:
                found[entry.model] = result
        return found
    except Exception:
        return {}  # Ollama unreachable; the picker already degrades to empty


def check(
    model: str,
    num_ctx: int = DEFAULT_NUM_CTX,
    **kwargs,
) -> Verdict | None:
    """A Verdict for one local Ollama model, or None for "no opinion".

    None means the question was not answered, never that the answer was yes:
    hardware this rule has no evidence for, an Ollama that is not running, a
    model that is not installed, or one whose metadata does not carry what the
    KV cache costs. Callers offer the model in that case — a fit check that
    could not measure must not refuse.
    """
    return verdicts(num_ctx, **kwargs).get(model)


def _gb(size: int) -> str:
    return f"{size / 1e9:.1f} GB"


def refusal(verdict: Verdict) -> str:
    """Why the model was not offered, in the numbers that decided it.

    It says what Ollama would DO — run part of the model on the CPU — and not
    what that would feel like, because the split is in Ollama's log and the
    consequence varies with what else the machine is carrying.
    """
    line = (
        f"{verdict.model} does not fit on this machine: it needs "
        f"{_gb(verdict.needed)} ({_gb(verdict.weights)} of weights plus "
        f"{_gb(verdict.kv)} of context at {verdict.num_ctx} tokens) and there is "
        f"{_gb(verdict.budget)} for a model here. Ollama would not stop you — "
        f"it loads what fits on the GPU and runs the rest on the CPU."
    )
    if verdict.largest_num_ctx:
        # Both entry points take --num-ctx and both read `num_ctx` from
        # config.toml, so the suggestion names the setting rather than one
        # command's flag.
        line += (
            f" It would fit at a context of {verdict.largest_num_ctx} tokens "
            f"(num_ctx in config.toml, or --num-ctx {verdict.largest_num_ctx})."
        )
    return line
