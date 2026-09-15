"""The cardless licence for structurally-safe Obsidian writes (#379).

Sibling of `tests/test_recipients.py`: the reason the module exists is that
both surfaces must give the SAME answer, because the question is what the
write can DESTROY and not who is watching. Every row is driven through the
real web approver (owner's own session) and the real terminal approver.

No real vault, no real config: the vault is `tmp_path`, the config is a
`config.toml` written beside it, and `AISH_CONFIG_HOME` points at it.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from aish import server as server_module
from aish import vault_writes
from aish.agent import Agent
from tests.test_agent import model_says, tool_call, tool_messages
from tests.test_recipients import _cli_cards, _FakeLog, _web_cards

TAGGED_INLINE = "---\ntags: [aish, pay]\n---\n# Note\n\nbody\n"
TAGGED_BLOCK = "---\ntitle: x\ntags:\n  - pay\n  - aish\n---\n# Note\n"
TAGGED_HASH = '---\ntags: "#aish"\n---\n# Note\n'
UNTAGGED = "---\ntags: [pay]\n---\n# Note\n"
BODY_ONLY_HASH = "# Note\n\nsome text #aish here\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault plus the config that names it, isolated from the developer's
    real `~/.config/aish` — the approvers read `AISH_CONFIG_HOME` at call time."""
    root = tmp_path / "vault"
    root.mkdir()
    config = tmp_path / "config-home"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[obsidian.vaults]\nMain = "{root}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("AISH_CONFIG_HOME", str(config))
    monkeypatch.delenv("AISH_CONFIG", raising=False)
    return root


def _note(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed(vault: Path) -> None:
    """One vault layout every table row reads."""
    _note(vault, "Existing.md", UNTAGGED)
    _note(vault, "Tagged.md", TAGGED_INLINE)
    _note(vault, "Block.md", TAGGED_BLOCK)
    _note(vault, "Hash.md", TAGGED_HASH)
    _note(vault, "Untagged.md", UNTAGGED)
    _note(vault, "BodyOnly.md", BODY_ONLY_HASH)
    _note(vault, "Payments/Twin.md", TAGGED_INLINE)
    _note(vault, "Archive/Twin.md", TAGGED_INLINE)
    _note(vault, "Payments/Eon.md", TAGGED_INLINE)


# Each row is (label, args) for obsidian_write, with the answer BOTH surfaces owe.
_WRITES = [
    ("create in vault root", {"action": "create", "title": "New", "content": "x"}, False),
    ("create into a subfolder",
     {"action": "create", "title": "New", "folder": "Payments", "content": "x"}, False),
    ("create into a folder that does not exist yet",
     {"action": "create", "title": "New", "folder": "Fresh/Deep"}, False),
    ("create where the file exists", {"action": "create", "title": "Existing"}, True),
    ("create where the file exists, if_exists=unique",
     {"action": "create", "title": "Existing", "if_exists": "unique"}, True),
    ("create with folder=../outside",
     {"action": "create", "title": "New", "folder": "../outside"}, True),
    ("create with an absolute folder",
     {"action": "create", "title": "New", "folder": "/tmp"}, True),
    ("create into a dot-directory",
     {"action": "create", "title": "New", "folder": ".obsidian"}, True),
    ("create with attachments",
     {"action": "create", "title": "New", "attachments": "/etc/hosts"}, True),
    ("create in an unknown vault",
     {"action": "create", "title": "New", "vault": "Nope"}, True),
    ("create with a title the wrapper would rewrite",
     {"action": "create", "title": "Foo/Bar"}, True),
    ("create with a title ending in punctuation",
     {"action": "create", "title": "Note."}, True),
    ("create with no title", {"action": "create", "content": "x"}, True),
    ("append to an inline-list tagged note",
     {"action": "append", "note": "Tagged", "content": "more"}, False),
    ("append to a block-list tagged note",
     {"action": "append", "note": "Block.md", "content": "more"}, False),
    ("append to a #aish tagged note",
     {"action": "append", "note": "Hash", "content": "more"}, False),
    ("append by vault-relative path",
     {"action": "append", "note": "Payments/Eon.md", "content": "more"}, False),
    ("append by unique bare name",
     {"action": "append", "note": "Eon", "content": "more"}, False),
    ("append to an un-tagged note",
     {"action": "append", "note": "Untagged", "content": "more"}, True),
    ("append where only the body says #aish",
     {"action": "append", "note": "BodyOnly", "content": "more"}, True),
    ("append to an ambiguous bare name",
     {"action": "append", "note": "Twin", "content": "more"}, True),
    ("append to a missing note",
     {"action": "append", "note": "Missing", "content": "more"}, True),
    ("append with attachments",
     {"action": "append", "note": "Tagged", "attachments": "/etc/hosts"}, True),
    ("set_frontmatter paid=true on a tagged note",
     {"action": "set_frontmatter", "note": "Tagged", "frontmatter": "paid=true"}, False),
    ("set_frontmatter two keys, kv form",
     {"action": "set_frontmatter", "note": "Tagged", "frontmatter": "paid=true, due=2026"},
     False),
    ("set_frontmatter touching tags",
     {"action": "set_frontmatter", "note": "Tagged", "frontmatter": "tags=aish, pay"}, True),
    ("set_frontmatter paid=true, JSON form",
     {"action": "set_frontmatter", "note": "Tagged", "frontmatter": '{"paid": true}'},
     False),
    ("set_frontmatter touching tags, JSON form",
     {"action": "set_frontmatter", "note": "Tagged", "frontmatter": '{"tags": ["aish"]}'},
     True),
    ("set_frontmatter on an un-tagged note",
     {"action": "set_frontmatter", "note": "Untagged", "frontmatter": "paid=true"}, True),
    ("set_frontmatter with nothing to set",
     {"action": "set_frontmatter", "note": "Tagged"}, True),
    ("replace on a tagged note",
     {"action": "replace", "note": "Tagged", "content": "new body"}, True),
    ("replace_section on a tagged note",
     {"action": "replace_section", "note": "Tagged", "section": "Log", "content": "x"},
     True),
    ("an unknown action", {"action": "purge", "note": "Tagged"}, True),
]


@pytest.mark.parametrize("label,args,cards", _WRITES, ids=[r[0] for r in _WRITES])
def test_both_surfaces_answer_a_write_identically(vault, label, args, cards, monkeypatch):
    _seed(vault)
    assert _web_cards("obsidian_write", args) is cards, f"web disagrees on: {label}"
    assert _cli_cards("obsidian_write", args, monkeypatch) is cards, f"cli: {label}"


def test_obsidian_delete_is_never_licensed(vault, monkeypatch):
    """The licence is one tool's. Deleting is the consequence the whole
    opt-in exists to keep behind a card, however the note is tagged."""
    _seed(vault)
    for args in ({"note": "Tagged"}, {"note": "Existing"}, {"note": "Tagged", "vault": "Main"}):
        assert vault_writes.owner_opted_write("obsidian_delete", args) is None
        assert _web_cards("obsidian_delete", args)
        assert _cli_cards("obsidian_delete", args, monkeypatch)


def test_the_licence_is_origin_independent(vault):
    """#377's property, restated for writes: an opted-in write is safe because
    of what it can destroy, so every origin gets one answer."""
    _seed(vault)
    for origin in ("user", "email", "schedule", "webhook"):
        assert server_module._auto_safe(
            "obsidian_write", {"action": "create", "title": "New"}, origin
        ) == vault_writes.OWNER_OPTED
        assert server_module._auto_safe(
            "obsidian_write", {"action": "replace", "note": "Tagged"}, origin
        ) is None


def test_the_audit_line_names_the_policy_never_the_origin(vault, monkeypatch):
    _seed(vault)
    args = {"action": "append", "note": "Tagged", "content": "x"}
    for origin in ("user", "email"):
        log = _FakeLog()
        approvers = server_module.make_web_approvers(
            _Bridge(), log, Path("/x/allow"), Path("/x/deny"),
            ask_all=False, get_scope=lambda: (".", []),
            trust_dir=lambda p: "", get_origin=lambda o=origin: o,
        )
        assert approvers[3]("obsidian_write", args) is True
        assert log.records[0][1] == "auto (owner-opted vault write)"
        assert origin not in log.records[0][1]
    log = _FakeLog()
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("a card was drawn"))
    from aish.cli import make_tool_approver

    assert make_tool_approver(log)("obsidian_write", args) is True
    assert log.records[0][1] == "auto (owner-opted vault write)"


class TestFailsClosed:
    """Every uncertainty is a card. #356 is the scar for the other direction."""

    def test_an_unreadable_note_is_a_card(self, vault):
        (vault / "Dir.md").mkdir()  # a directory named like a note
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Dir", "content": "x"}
        ) is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads through chmod 000")
    def test_a_note_aish_cannot_read_is_a_card(self, vault):
        path = _note(vault, "Locked.md", TAGGED_INLINE)
        path.chmod(0)
        try:
            assert vault_writes.owner_opted_write(
                "obsidian_write", {"action": "append", "note": "Locked", "content": "x"}
            ) is None
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_a_symlink_inside_the_vault_pointing_outside_is_a_card(self, vault, tmp_path):
        outside = tmp_path / "elsewhere" / "Real.md"
        outside.parent.mkdir()
        outside.write_text(TAGGED_INLINE, encoding="utf-8")
        (vault / "Link.md").symlink_to(outside)
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Link", "content": "x"}
        ) is None
        # And a create THROUGH a symlinked folder lands outside: card.
        (vault / "Out").symlink_to(outside.parent, target_is_directory=True)
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "create", "title": "New", "folder": "Out"}
        ) is None

    def test_a_tagged_note_inside_trash_or_obsidian_is_a_card(self, vault):
        _note(vault, ".trash/Old.md", TAGGED_INLINE)
        _note(vault, ".obsidian/Conf.md", TAGGED_INLINE)
        for ref in ("Old", ".trash/Old.md", "Conf", ".obsidian/Conf"):
            assert vault_writes.owner_opted_write(
                "obsidian_write", {"action": "append", "note": ref, "content": "x"}
            ) is None

    def test_a_tags_key_stated_twice_is_a_card(self, vault):
        """#326: nothing here picks one of two readings."""
        _note(vault, "Twice.md", "---\ntags: [aish]\ntags: [pay]\n---\n# x\n")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Twice", "content": "x"}
        ) is None

    def test_an_unclosed_frontmatter_block_is_a_card(self, vault):
        _note(vault, "Open.md", "---\ntags: [aish]\n# no closing fence\n")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Open", "content": "x"}
        ) is None

    def test_a_note_that_is_not_utf8_is_a_card(self, vault):
        (vault / "Bin.md").write_bytes(b"---\ntags: [aish]\n---\n\xff\xfe")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Bin", "content": "x"}
        ) is None

    def test_no_config_or_a_broken_config_is_a_card(self, tmp_path):
        args = {"action": "create", "title": "New"}
        assert vault_writes.owner_opted_write(
            "obsidian_write", args, config_home=tmp_path / "absent"
        ) is None
        (tmp_path / "config.toml").write_text("[obsidian\nbroken", encoding="utf-8")
        assert vault_writes.owner_opted_write(
            "obsidian_write", args, config_home=tmp_path
        ) is None

    def test_a_vault_that_is_not_a_directory_is_a_card(self, tmp_path):
        (tmp_path / "config.toml").write_text(
            f'[obsidian.vaults]\nMain = "{tmp_path / "missing"}"\n', encoding="utf-8"
        )
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "create", "title": "New"}, config_home=tmp_path
        ) is None

    def test_several_vaults_need_the_vault_named(self, tmp_path):
        """The wrapper falls back to one called `Main`; the licence does not
        guess which vault a write lands in."""
        for name in ("Main", "Work"):
            (tmp_path / name).mkdir()
        (tmp_path / "config.toml").write_text(
            f'[obsidian.vaults]\nMain = "{tmp_path / "Main"}"\nWork = "{tmp_path / "Work"}"\n',
            encoding="utf-8",
        )
        args = {"action": "create", "title": "New"}
        assert vault_writes.owner_opted_write(
            "obsidian_write", args, config_home=tmp_path
        ) is None
        assert vault_writes.owner_opted_write(
            "obsidian_write", {**args, "vault": "work"}, config_home=tmp_path
        ) == vault_writes.OWNER_OPTED

    def test_a_relative_vault_path_is_a_card(self, tmp_path):
        (tmp_path / "config.toml").write_text(
            '[obsidian.vaults]\nMain = "vault"\n', encoding="utf-8"
        )
        (tmp_path / "vault").mkdir()
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "create", "title": "New"}, config_home=tmp_path
        ) is None

    def test_a_dangling_symlink_at_the_create_target_is_a_card(self, vault):
        (vault / "Ghost.md").symlink_to(vault / "nowhere.md")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "create", "title": "Ghost"}
        ) is None


class TestFrontmatterReading:
    def test_the_three_tag_shapes_and_the_hash(self, tmp_path):
        for text in (
            "---\ntags: [aish]\n---\n",
            "---\ntags: [ '#aish' , pay ]\n---\n",
            "---\ntags:\n- aish\n---\n",
            "---\ntags:\n  - pay\n  - \"#aish\"\n---\n",
            "---\ntags: aish\n---\n",
            '---\ntags: "#aish"\n---\n',
            "---\ntags: pay aish\n---\n",
            "---\ntags: AISH\n---\n",
        ):
            path = tmp_path / "n.md"
            path.write_text(text, encoding="utf-8")
            assert "aish" in vault_writes._frontmatter_tags(path), text

    def test_what_does_not_count(self, tmp_path):
        for text in (
            "# no frontmatter\n#aish\n",
            "---\ntags: [aishx]\n---\n",
            "---\nnested:\n  tags: [aish]\n---\n",
            "---\ntag: aish\n---\n",  # the singular key is the wrapper's leniency, not ours
            "---\ntags:\n---\n#aish\n",
            "---\ntags: []\n---\n",
        ):
            path = tmp_path / "n.md"
            path.write_text(text, encoding="utf-8")
            assert "aish" not in vault_writes._frontmatter_tags(path), text


class TestSetFrontmatterStaysOnItsOwnLine:
    """A value is written as a line of YAML, so the licence reads VALUES and
    not only key names. Found by adversarial review before this shipped: a
    newline inside a value wrote a second `tags:` line, cardless, and the
    note fell out of its own opt-in on both sides."""

    plain = staticmethod(vault_writes._frontmatter_is_plain)

    def test_plain_forms_are_licensed(self):
        for raw in (
            "paid=true",
            "paid=true, due=2026",
            "paid=",
            '{"paid": true, "due": "2026"}',
            '{"paid": ["a", "b"], "n": 3}',
            {"paid": True},
            ' {"paid": "x"}',
        ):
            assert self.plain(raw), raw

    def test_every_route_to_a_second_line_or_to_tags_cards(self):
        for raw in (
            # the review's reproducers
            '{"paid": "x\\ntags:evil"}',
            "tags:x=1",
            '{"paid\\ntags": "x"}',
            '{"paid": ["a\\ntags:evil"]}',
            '{"paid": "x\\n---\\nrest"}',
            '{"paid": "x\\u2028tags: evil"}',
            # tags by any spelling
            "tags=aish, pay",
            "Tags=x",
            '{"tag": "x"}',
            {"tags": ["aish"]},
            # removals
            '{"paid": null}',
            "paid=null",
            "paid=None",
            # nested or unreadable
            '{"paid": {"a": 1}}',
            "paid=true, oops",
            "paid",
            "[1, 2]",
            "",
            None,
            7,
            "key with space=1",
            "a:b=1",
        ):
            assert not self.plain(raw), raw


class TestTheLicenceNeverLeansOnTheWrapper:
    """Each of these was refused only by the wrapper's own check before the
    review; now aish's reading cards it itself."""

    def test_a_title_with_non_ascii_whitespace_is_a_card(self, vault):
        _note(vault, "Foo Bar.md", UNTAGGED)
        for title in ("Foo Bar", "Foo Bar", "Foo\tBar", "Foo　Bar"):
            assert vault_writes.owner_opted_write(
                "obsidian_write", {"action": "create", "title": title}
            ) is None, repr(title)

    def test_an_icloud_placeholder_counts_as_present(self, vault):
        (vault / ".Evicted.md.icloud").write_bytes(b"")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "create", "title": "Evicted"}
        ) is None
        # ...and as a note aish cannot read, so an edit through it cards
        # rather than resolving past it to a different note.
        _note(vault, "Sub/Evicted.md", TAGGED_INLINE)
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Evicted", "content": "x"}
        ) is None

    def test_a_md_reference_resolves_in_the_wrappers_order(self, vault):
        """`Foo.md` with only `Foo.md.md` present names THAT file, because
        that is the file the write reaches; the tagged twin elsewhere must
        not be what licenses it."""
        _note(vault, "Foo.md.md", UNTAGGED)
        _note(vault, "Sub/Foo.md", TAGGED_INLINE)
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Foo.md", "content": "x"}
        ) is None
        (vault / "Foo.md.md").write_text(TAGGED_INLINE, encoding="utf-8")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Foo.md", "content": "x"}
        ) == vault_writes.OWNER_OPTED

    def test_a_symlinked_note_inside_the_vault_is_a_card(self, vault):
        _note(vault, "Real.md", TAGGED_INLINE)
        (vault / "Link.md").symlink_to(vault / "Real.md")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "append", "note": "Link", "content": "x"}
        ) is None

    def test_a_config_of_the_wrong_shape_is_a_card_not_a_crash(self, tmp_path):
        (tmp_path / "config.toml").write_text('obsidian = "x"\n', encoding="utf-8")
        assert vault_writes.owner_opted_write(
            "obsidian_write", {"action": "create", "title": "New"}, config_home=tmp_path
        ) is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches through chmod 000")
    def test_an_unsearchable_parent_is_a_card_not_absence(self, vault):
        folder = vault / "Locked"
        folder.mkdir()
        folder.chmod(0)
        try:
            assert vault_writes.owner_opted_write(
                "obsidian_write", {"action": "create", "title": "New", "folder": "Locked"}
            ) is None
        finally:
            folder.chmod(stat.S_IRWXU)


# --------------------------------------------------------------------------
# The acceptance case: a triggered session with no viewer
# --------------------------------------------------------------------------


class _Bridge:
    """A bridge nobody is watching. `ask` records the card and denies it —
    the shape a held card takes in a test, since a real one blocks forever."""

    def __init__(self):
        self.asked: list = []

    def emit(self, event, record=True):
        pass

    def ask(self, request):
        request.setdefault("id", "uid-1")
        self.asked.append(request)
        return {"action": "deny"}


def _install_obsidian_write(vault: Path) -> None:
    """A stand-in `obsidian_write` in the (isolated) global tools dir. Its
    wrapper creates the note the args name — enough to observe that a
    licensed create RAN and a carded one did not. `mutating: yes`, as the real
    manifest declares; nothing about the licence lives in this file."""
    from aish import tool_plugins

    tdir = tool_plugins.GLOBAL_TOOLS_DIR / "obsidian_write"
    tdir.mkdir(parents=True)
    schema = {
        "action": {"type": "string", "required": True},
        "title": {"type": "string"},
        "note": {"type": "string"},
        "folder": {"type": "string"},
        "content": {"type": "string"},
    }
    (tdir / "TOOL.md").write_text(
        "---\nname: obsidian_write\ndescription: write a note\nexec: ./wrapper\n"
        f"mutating: yes\nreturns: text\nschema: {json.dumps(schema)}\n---\nbody\n",
        encoding="utf-8",
    )
    wrapper = tdir / "wrapper"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        "a = json.load(sys.stdin)\n"
        f"root = pathlib.Path({str(vault)!r})\n"
        "if a['action'] == 'create':\n"
        "    p = root / (a.get('folder') or '.') / (a['title'] + '.md')\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    p.write_text('---\\ntags: [aish]\\n---\\n' + a.get('content', ''))\n"
        "    print('created ' + str(p))\n"
        "else:\n"
        "    (root / (a['note'] + '.md')).write_text(a.get('content', ''))\n"
        "    print('wrote')\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)


def _triggered_agent(vault: Path, cwd: Path, responses, origin="email"):
    """An Agent the way `server.open_session` builds one for `/trigger`: a
    non-user origin and the REAL web approver, whose bridge has no viewer."""
    from tests.test_agent import FakeChat

    bridge, log = _Bridge(), _FakeLog()
    approvers = server_module.make_web_approvers(
        bridge, log, Path("/x/allow"), Path("/x/deny"),
        ask_all=False, get_scope=lambda: (str(cwd), []),
        trust_dir=lambda p: "", get_origin=lambda: origin,
    )
    chat = FakeChat(responses)
    agent = Agent(
        model="fake", approve=lambda _c: None, client_chat=chat,
        approve_tool=approvers[3], origin=origin, cwd=str(cwd),
    )
    return agent, chat, bridge, log


def test_a_triggered_session_creates_a_note_and_holds_a_replace(vault, tmp_path):
    """The issue's acceptance case. A triggered session is offered
    `obsidian_write` (it carries the web approver, as every server session
    does — `server.open_session`), a licensed create RUNS with no card, and
    an unlicensed replace reaches the card, where with nobody watching it is
    refused — with the denial sentence, never silently dropped."""
    _seed(vault)
    _install_obsidian_write(vault)
    agent, chat, bridge, log = _triggered_agent(vault, tmp_path, [
        model_says(tool_calls=[tool_call("obsidian_write", action="create",
                                         title="Captured", content="from mail")]),
        model_says(tool_calls=[tool_call("obsidian_write", action="replace",
                                         note="Tagged", content="wiped")]),
        model_says("done"),
    ])
    assert agent.run_task("capture this") == "done"
    offered = {t["function"]["name"] for t in chat.calls[0]["tools"]}
    assert "obsidian_write" in offered
    assert (vault / "Captured.md").read_text(encoding="utf-8").endswith("from mail")
    assert (vault / "Tagged.md").read_text(encoding="utf-8") == TAGGED_INLINE  # untouched
    assert [c["tool"] for c in bridge.asked] == ["obsidian_write"]
    assert bridge.asked[0]["args"]["action"] == "replace"
    create_result, replace_result = tool_messages(agent.messages)
    assert "created" in create_result["content"]
    assert "USER DENIED" in replace_result["content"]
    assert log.records[0][1] == "auto (owner-opted vault write)"
    assert log.records[1][1] == "denied"


def test_without_any_tool_approver_the_tool_is_hidden_exactly_as_gmail_send_is(
    vault, tmp_path
):
    """The visibility rule is unchanged (`Agent._refresh_plugin_tools`): a
    mutating tool is offered only when a tool approver is wired, and the
    licence lives INSIDE that approver. No production entry point builds a
    session without one; a stale call in such a session is refused with the
    same sentence `gmail_send` gets, never run."""
    from tests.test_agent import FakeChat

    _seed(vault)
    _install_obsidian_write(vault)
    chat = FakeChat([
        model_says(tool_calls=[tool_call("obsidian_write", action="create", title="Stale")]),
        model_says("done"),
    ])
    agent = Agent(model="fake", approve=lambda _c: None, client_chat=chat,
                  approve_tool=None, origin="email", cwd=str(tmp_path))
    agent.run_task("go")
    assert "obsidian_write" not in {t["function"]["name"] for t in chat.calls[0]["tools"]}
    assert not (vault / "Stale.md").exists()
    assert "no tool approver" in tool_messages(agent.messages)[0]["content"]
