"""Whether a local model can run on this machine (#324).

No Ollama, no network: every call into the daemon is a seam, and the machine's
own memory is a parameter.
"""

from types import SimpleNamespace

import pytest

from aish import backends, model_fit

GB = 1_000_000_000

# Real qwen3:14b metadata, as `ollama show` returns it. Kept verbatim because
# one test below checks this arithmetic against what Ollama's own runner
# logged for the same model — that is the only reason to believe the number.
QWEN3_14B = {
    "general.architecture": "qwen3",
    "qwen3.block_count": 40,
    "qwen3.attention.head_count": 40,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
}
QWEN3_8B = {
    "general.architecture": "qwen3",
    "qwen3.block_count": 36,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
}

MAC_16GB = 17_179_869_184


def entry(model, size, digest=None):
    return SimpleNamespace(model=model, size=size, digest=digest or f"digest-{model}")


def fake_ollama(installed, info, loaded=()):
    """(show, listed, running) seams over a made-up Ollama."""
    return {
        "show": lambda name: SimpleNamespace(modelinfo=info[name]),
        "listed": lambda: list(installed),
        "running": lambda: list(loaded),
    }


@pytest.fixture(autouse=True)
def unified_mac(monkeypatch):
    """These are claims about Apple Silicon; assert them on any host."""
    monkeypatch.setattr(model_fit, "unified_memory", lambda: True)
    model_fit._INFO_CACHE.clear()
    model_fit.configure(None)
    yield
    model_fit._INFO_CACHE.clear()
    model_fit.configure(None)


class TestFootprintArithmetic:
    def test_kv_matches_what_ollama_itself_computed(self):
        """The calibration. On 2026-08-27 Ollama's runner logged, for
        qwen3:14b at num_ctx 32768, `kv cache device=Metal size="5.0 GiB"`.
        If this ever stops reproducing that, the estimate has drifted from
        the thing it claims to predict and the whole rule is guesswork."""
        kv = model_fit.kv_bytes_per_token(QWEN3_14B) * 32768
        assert round(kv / (1024**3), 1) == 5.0

    def test_kv_scales_with_context(self):
        per_token = model_fit.kv_bytes_per_token(QWEN3_14B)
        assert per_token * 16384 == (per_token * 32768) // 2

    def test_metadata_without_the_attention_shape_yields_nothing(self):
        assert model_fit.kv_bytes_per_token({"general.architecture": "qwen3"}) == 0
        assert model_fit.kv_bytes_per_token({}) == 0

    def test_head_dimension_is_derived_when_key_length_is_absent(self):
        older = dict(QWEN3_8B)
        del older["qwen3.attention.key_length"]
        del older["qwen3.attention.value_length"]
        older["qwen3.embedding_length"] = 4096
        assert model_fit.kv_bytes_per_token(older) > 0


class TestVerdicts:
    def test_the_model_that_took_the_machine_down_does_not_fit(self):
        """qwen3:14b on the 16 GB M4: 9.3 GB of weights plus 5.4 GB of KV
        cache at aish's default context, against a ~12.4 GB budget."""
        found = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama([entry("qwen3:14b", 9_276_198_565)], {"qwen3:14b": QWEN3_14B}),
        )
        verdict = found["qwen3:14b"]
        assert not verdict.fits
        assert verdict.needed > verdict.budget

    def test_the_model_actually_in_use_still_fits(self):
        found = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama([entry("qwen3:8b", 5_200_000_000)], {"qwen3:8b": QWEN3_8B}),
        )
        assert found["qwen3:8b"].fits

    def test_a_model_that_overruns_only_at_a_large_context_fits_at_a_small_one(self):
        seams = fake_ollama([entry("qwen3:14b", 9_276_198_565)], {"qwen3:14b": QWEN3_14B})
        assert not model_fit.verdicts(32768, ram=MAC_16GB, **seams)["qwen3:14b"].fits
        assert model_fit.verdicts(8192, ram=MAC_16GB, **seams)["qwen3:14b"].fits

    def test_refusal_names_the_context_that_would_fit(self):
        verdict = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama([entry("qwen3:14b", 9_276_198_565)], {"qwen3:14b": QWEN3_14B}),
        )["qwen3:14b"]
        # 12.4 GB budget less 9.3 GB of weights leaves 3.1 GB of KV cache,
        # which at 160 KB/token buys 18.8k tokens — the power of two under it.
        assert verdict.largest_num_ctx == 16384
        text = model_fit.refusal(verdict)
        assert "does not fit" in text and "--num-ctx 16384" in text
        # The numbers that decided it are in the sentence, not just the verdict.
        assert "14.6 GB" in text and "9.3 GB" in text

    def test_no_context_fits_when_the_weights_alone_overrun(self):
        found = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama([entry("huge:70b", 40 * GB)], {"huge:70b": QWEN3_14B}),
        )
        verdict = found["huge:70b"]
        assert not verdict.fits and verdict.largest_num_ctx == 0
        assert "--num-ctx" not in model_fit.refusal(verdict)


class TestOngoingProcesses:
    """What Ollama already holds is contended, and is measured rather than
    assumed — but a resident model does not compete with itself."""

    def test_another_resident_model_shrinks_the_budget(self):
        installed = [entry("qwen3:8b", 5_200_000_000)]
        info = {"qwen3:8b": QWEN3_8B}
        alone = model_fit.verdicts(32768, ram=MAC_16GB, **fake_ollama(installed, info))
        crowded = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama(
                installed,
                info,
                loaded=[SimpleNamespace(model="embeddinggemma", size_vram=2 * GB)],
            ),
        )
        assert crowded["qwen3:8b"].budget == alone["qwen3:8b"].budget - 2 * GB

    def test_a_model_is_not_charged_for_its_own_residency(self):
        installed = [entry("qwen3:8b", 5_200_000_000)]
        info = {"qwen3:8b": QWEN3_8B}
        alone = model_fit.verdicts(32768, ram=MAC_16GB, **fake_ollama(installed, info))
        itself = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama(
                installed,
                info,
                loaded=[SimpleNamespace(model="qwen3:8b", size_vram=6 * GB)],
            ),
        )
        assert itself["qwen3:8b"].budget == alone["qwen3:8b"].budget


class TestNoOpinion:
    """None is not "yes". Every one of these must leave the model offered,
    because none of them measured anything."""

    def test_hardware_this_rule_has_no_evidence_for(self, monkeypatch):
        monkeypatch.setattr(model_fit, "unified_memory", lambda: False)
        assert model_fit.verdicts(32768, ram=MAC_16GB, **fake_ollama(
            [entry("qwen3:14b", 9_276_198_565)], {"qwen3:14b": QWEN3_14B})) == {}

    def test_ollama_not_running(self):
        def boom():
            raise ConnectionError("connection refused")

        assert model_fit.verdicts(32768, ram=MAC_16GB, show=lambda n: None,
                                  listed=boom, running=list) == {}

    def test_model_without_readable_metadata_is_absent_not_refused(self):
        found = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            **fake_ollama([entry("mystery:1b", 3 * GB)], {"mystery:1b": {}}),
        )
        assert found == {}

    def test_one_unreadable_model_does_not_blank_the_others(self):
        def show(name):
            if name == "broken:1b":
                raise ValueError("no manifest")
            return SimpleNamespace(modelinfo=QWEN3_8B)

        found = model_fit.verdicts(
            32768,
            ram=MAC_16GB,
            show=show,
            listed=lambda: [entry("broken:1b", 3 * GB), entry("qwen3:8b", 5_200_000_000)],
            running=list,
        )
        assert set(found) == {"qwen3:8b"}

    def test_a_model_that_is_not_installed_gets_no_verdict(self):
        assert model_fit.check(
            "never-pulled:8b",
            32768,
            ram=MAC_16GB,
            **fake_ollama([entry("qwen3:8b", 5_200_000_000)], {"qwen3:8b": QWEN3_8B}),
        ) is None


class TestConfiguredCeiling:
    def test_an_explicit_memory_cap_overrides_the_share(self):
        seams = fake_ollama([entry("qwen3:8b", 5_200_000_000)], {"qwen3:8b": QWEN3_8B})
        assert model_fit.verdicts(32768, ram=MAC_16GB, **seams)["qwen3:8b"].fits
        model_fit.configure(6)  # 6 GB — below what qwen3:8b needs
        model_fit._INFO_CACHE.clear()
        assert not model_fit.verdicts(32768, ram=MAC_16GB, **seams)["qwen3:8b"].fits

    def test_configure_none_restores_the_default(self):
        model_fit.configure(6)
        model_fit.configure(None)
        seams = fake_ollama([entry("qwen3:8b", 5_200_000_000)], {"qwen3:8b": QWEN3_8B})
        assert model_fit.verdicts(32768, ram=MAC_16GB, **seams)["qwen3:8b"].fits

    def test_a_nonsense_cap_is_ignored_rather_than_hiding_everything(self):
        model_fit.configure(0)
        assert model_fit._MEMORY_CAP is None
        model_fit.configure(-5)
        assert model_fit._MEMORY_CAP is None


class TestTheFence:
    """The picker is not the only way in. #324 chose one fence, in make_chat,
    because --model at launch, /model <name>, the web model card and the saved
    startup default all pass through it."""

    def test_make_chat_refuses_a_model_that_does_not_fit(self, monkeypatch):
        verdict = model_fit.Verdict(
            model="qwen3:14b", fits=False, weights=9 * GB, kv=5 * GB,
            budget=12 * GB, num_ctx=32768, largest_num_ctx=8192,
        )
        monkeypatch.setattr(model_fit, "check", lambda *a, **k: verdict)
        with pytest.raises(backends.BackendError, match="does not fit"):
            backends.make_chat("qwen3:14b", num_ctx=32768)

    def test_make_chat_allows_a_model_that_fits(self, monkeypatch):
        verdict = model_fit.Verdict(
            model="qwen3:8b", fits=True, weights=5 * GB, kv=4 * GB,
            budget=12 * GB, num_ctx=32768, largest_num_ctx=32768,
        )
        monkeypatch.setattr(model_fit, "check", lambda *a, **k: verdict)
        _chat, provider, name = backends.make_chat("qwen3:8b", num_ctx=32768)
        assert (provider, name) == ("ollama", "qwen3:8b")

    def test_no_verdict_means_the_model_is_allowed(self, monkeypatch):
        monkeypatch.setattr(model_fit, "check", lambda *a, **k: None)
        _chat, provider, _name = backends.make_chat("anything:1b")
        assert provider == "ollama"

    def test_a_caller_that_does_not_say_still_gets_checked(self, monkeypatch):
        """The fence must never be silently OFF: a make_chat call with no
        num_ctx is checked at aish's own default, not waved through."""
        seen = {}
        monkeypatch.setattr(
            model_fit, "check",
            lambda model, num_ctx, **k: seen.update(num_ctx=num_ctx) or None,
        )
        backends.make_chat("qwen3:8b")
        assert seen["num_ctx"] == model_fit.DEFAULT_NUM_CTX

    def test_cloud_models_are_never_fit_checked(self, monkeypatch):
        def fail(*a, **k):
            raise AssertionError("a cloud model has no local footprint")

        monkeypatch.setattr(model_fit, "check", fail)
        _chat, provider, name = backends.make_chat(
            "gemini:gemini-3.5-flash", client=object()
        )
        assert (provider, name) == ("gemini", "gemini-3.5-flash")


class TestPicker:
    def test_a_model_that_does_not_fit_is_not_offered(self, monkeypatch):
        import sys

        from aish.cli import available_models

        monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(
            list=lambda: SimpleNamespace(models=[
                entry("qwen3:8b", 5_200_000_000),
                entry("qwen3:14b", 9_276_198_565),
            ])
        ))
        monkeypatch.setattr(model_fit, "verdicts", lambda _ctx: {
            "qwen3:8b": model_fit.Verdict("qwen3:8b", True, 5 * GB, 4 * GB,
                                          12 * GB, 32768, 32768),
            "qwen3:14b": model_fit.Verdict("qwen3:14b", False, 9 * GB, 5 * GB,
                                           12 * GB, 32768, 8192),
        })
        agent = SimpleNamespace(model="qwen3:8b", provider="ollama", num_ctx=32768)
        names = [name for name, _ in available_models(agent)]
        assert "qwen3:8b" in names
        assert "qwen3:14b" not in names

    def test_the_model_in_use_is_listed_even_if_it_does_not_fit(self, monkeypatch):
        """A session can be running one — Ollama may have been down when it
        was chosen. Hiding it would describe a machine the owner is not on."""
        import sys

        from aish.cli import available_models

        monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(
            list=lambda: SimpleNamespace(models=[entry("qwen3:14b", 9_276_198_565)])
        ))
        monkeypatch.setattr(model_fit, "verdicts", lambda _ctx: {
            "qwen3:14b": model_fit.Verdict("qwen3:14b", False, 9 * GB, 5 * GB,
                                           12 * GB, 32768, 8192),
        })
        agent = SimpleNamespace(model="qwen3:14b", provider="ollama", num_ctx=32768)
        listed = dict(available_models(agent))
        assert "current" in listed["qwen3:14b"]

    def test_nothing_is_hidden_when_there_is_no_verdict(self, monkeypatch):
        import sys

        from aish.cli import available_models

        monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(
            list=lambda: SimpleNamespace(models=[entry("qwen3:14b", 9_276_198_565)])
        ))
        monkeypatch.setattr(model_fit, "verdicts", lambda _ctx: {})
        agent = SimpleNamespace(model="gemini-3.5-flash", provider="gemini", num_ctx=32768)
        assert "qwen3:14b" in [name for name, _ in available_models(agent)]
