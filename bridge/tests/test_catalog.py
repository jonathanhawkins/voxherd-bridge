"""Tests for bridge/catalog.py — the command catalog.

Everything here builds its own assistant tree under ``tmp_path`` and points
``HOME`` at it. The developer's real ``~/.claude`` varies machine to machine
(and ``~/.claude/commands`` does not exist on the dev box at all), so no test
may depend on it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from bridge import catalog
from bridge.tests.conftest import auth_headers, register_test_session

# The probes as shipped, taken before the autouse fixture below blanks them.
SHIPPED_PROBES = dict(catalog._PROBES)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point HOME at a scratch dir so ``~/.claude`` etc. are ours, and empty."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    catalog.clear_cache()
    yield home
    catalog.clear_cache()


@pytest.fixture(autouse=True)
def no_real_probes(monkeypatch):
    """No test runs a real CLI: a catalog request for codex would otherwise
    start ``codex mcp list`` in the background. Probe tests bring their own."""
    monkeypatch.setattr(catalog, "_PROBES", {})
    catalog._PROBE_ATTEMPTED.clear()


@pytest.fixture(autouse=True)
async def probes_stopped():
    """End any probe a test left running before its event loop closes.

    Without this, a test that fails with a probe mid-spawn hangs the whole run
    in pytest-asyncio's teardown -- measured: two hours, on a regression this
    suite exists to catch. It is the same hang ``stop_probes`` prevents on the
    bridge's own shutdown.
    """
    yield
    await catalog.stop_probes()


def write(path, text: str) -> None:
    """Create parent dirs and write a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def by_name(entries: list[dict], name: str) -> dict | None:
    for entry in entries:
        if entry["name"] == name:
            return entry
    return None


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_parses_plain_frontmatter():
    fields = catalog.parse_frontmatter(
        "---\nname: deploy\ndescription: Ship it to prod\n---\n\nBody text\n"
    )
    assert fields["name"] == "deploy"
    assert fields["description"] == "Ship it to prod"


def test_parses_folded_block_scalar():
    """``>-`` folded scalars are what real SKILL.md files use."""
    fields = catalog.parse_frontmatter(
        "---\n"
        "name: br\n"
        "description: >-\n"
        "  Beads Rust issue tracker. Use for creating issues,\n"
        "  dependency tracking, and any `br` CLI operations.\n"
        "---\n"
    )
    assert fields["name"] == "br"
    assert fields["description"] == (
        "Beads Rust issue tracker. Use for creating issues, "
        "dependency tracking, and any `br` CLI operations."
    )


def test_parses_literal_block_scalar():
    fields = catalog.parse_frontmatter("---\ndescription: |\n  line one\n  line two\n---\n")
    assert fields["description"] == "line one\nline two"


def test_strips_quotes_from_scalars():
    fields = catalog.parse_frontmatter("---\nname: \"quoted\"\ndescription: 'single'\n---\n")
    assert fields["name"] == "quoted"
    assert fields["description"] == "single"


def test_skips_nested_mappings():
    """``metadata:`` with indented children must not leak into top-level keys."""
    fields = catalog.parse_frontmatter(
        "---\nname: automation\nmetadata:\n  icon: schedule\n  color: red\nmodel: opus\n---\n"
    )
    assert fields["name"] == "automation"
    assert fields["model"] == "opus"
    assert "icon" not in fields
    assert "color" not in fields


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no frontmatter at all\n",
        "---\n",  # unterminated, nothing inside
        "---\nname: unterminated\n",  # no closing fence
        "---\n\x00\x01 garbage \xff\n---\n",
        "--\nname: wrong fence\n--\n",
        "---\n: no key\n---\n",
        "---\n- a list, not a mapping\n---\n",
    ],
)
def test_malformed_frontmatter_never_raises(text):
    """Garbled input yields no fields, never an exception."""
    assert isinstance(catalog.parse_frontmatter(text), dict)


def test_unterminated_frontmatter_still_yields_keys():
    fields = catalog.parse_frontmatter("---\nname: partial\ndescription: cut off")
    assert fields["name"] == "partial"


def test_parses_toml_head():
    fields = catalog.parse_toml_head(
        'name = "code-reviewer"\n'
        'description = "Expert code review specialist."\n'
        'developer_instructions = """\nlong body\n"""\n'
    )
    assert fields["name"] == "code-reviewer"
    assert fields["description"] == "Expert code review specialist."
    assert "developer_instructions" not in fields


def test_toml_head_stops_at_table_header():
    fields = catalog.parse_toml_head('name = "a"\n[tools]\nname = "should-not-win"\n')
    assert fields["name"] == "a"


def test_toml_head_never_raises_on_garbage():
    assert catalog.parse_toml_head("\x00 = = = [[[\n") == {}


# ---------------------------------------------------------------------------
# Missing directories are the normal case
# ---------------------------------------------------------------------------


def test_missing_directories_yield_empty_scan(fake_home):
    """A brand new home with no assistant dirs at all: empty, not an error."""
    for assistant in catalog.CATALOG_ASSISTANTS:
        assert catalog.scan_filesystem(assistant) == []


def test_missing_claude_commands_dir_does_not_block_other_scans(fake_home):
    """Mirrors the dev machine: ~/.claude/commands absent, skills present."""
    write(fake_home / ".claude/skills/br/SKILL.md", "---\nname: br\ndescription: Tracker\n---\n")
    assert not (fake_home / ".claude/commands").exists()

    entries = catalog.scan_filesystem("claude")
    assert [e["name"] for e in entries] == ["br"]
    assert entries[0]["kind"] == "skill"


def test_missing_codex_prompts_dir_is_not_an_error(fake_home):
    write(fake_home / ".codex/agents/rev.toml", 'name = "rev"\ndescription = "Reviewer"\n')
    assert not (fake_home / ".codex/prompts").exists()

    entries = catalog.scan_filesystem("codex")
    assert [(e["kind"], e["name"]) for e in entries] == [("agent", "rev")]


def test_empty_directory_yields_no_entries(fake_home):
    """~/.grok/skills exists but is empty on the dev machine."""
    (fake_home / ".grok/skills").mkdir(parents=True)
    assert catalog.scan_filesystem("grok") == []


# ---------------------------------------------------------------------------
# Security: symlink escape, size caps, head caps
# ---------------------------------------------------------------------------


def test_symlink_escaping_the_allowed_root_is_refused(fake_home, tmp_path):
    """A symlink pointing outside every assistant root must yield no entry."""
    secret = tmp_path / "outside" / "secret.md"
    write(secret, "---\nname: leaked\ndescription: private data\n---\n")

    link_dir = fake_home / ".claude/skills/evil"
    link_dir.mkdir(parents=True)
    os.symlink(secret, link_dir / "SKILL.md")
    assert (link_dir / "SKILL.md").is_file()  # glob will find it

    entries = catalog.scan_filesystem("claude")
    assert entries == []
    assert catalog.read_head(str(link_dir / "SKILL.md"), catalog._allowed_roots(None)) == ""


def test_symlinked_file_inside_the_root_is_also_refused(fake_home):
    """O_NOFOLLOW refuses the final component even when the target is in-root."""
    real = fake_home / ".claude/skills/real/SKILL.md"
    write(real, "---\nname: real\ndescription: ok\n---\n")
    link_dir = fake_home / ".claude/skills/alias"
    link_dir.mkdir(parents=True)
    os.symlink(real, link_dir / "SKILL.md")

    names = [e["name"] for e in catalog.scan_filesystem("claude")]
    assert names == ["real"]


def test_read_head_refuses_paths_outside_allowed_roots(fake_home, tmp_path):
    outsider = tmp_path / "elsewhere.md"
    outsider.write_text("---\nname: nope\n---\n")
    assert catalog.read_head(str(outsider), catalog._allowed_roots(None)) == ""


def test_oversize_file_is_refused(fake_home, monkeypatch):
    monkeypatch.setattr(catalog, "_MAX_FILE_BYTES", 128)
    write(
        fake_home / ".claude/skills/huge/SKILL.md",
        "---\nname: huge\ndescription: " + ("x" * 500) + "\n---\n",
    )
    write(fake_home / ".claude/skills/small/SKILL.md", "---\nname: small\ndescription: ok\n---\n")

    names = [e["name"] for e in catalog.scan_filesystem("claude")]
    assert names == ["small"]


def test_only_the_head_of_a_file_is_read(fake_home):
    """Frontmatter lives in the first couple of KB; the body is never slurped."""
    body = "BODYMARKER\n" * 50_000  # ~550 KB of body, under the hard file cap
    path = fake_home / ".claude/skills/big/SKILL.md"
    write(path, "---\nname: big\ndescription: short header\n---\n" + body)
    assert path.stat().st_size > 10 * catalog._MAX_HEAD_BYTES

    text = catalog.read_head(str(path), catalog._allowed_roots(None))
    assert 0 < len(text) <= catalog._MAX_HEAD_BYTES
    assert catalog.scan_filesystem("claude")[0]["description"] == "short header"


def test_description_is_truncated_for_the_palette(fake_home):
    write(
        fake_home / ".claude/skills/wordy/SKILL.md",
        "---\nname: wordy\ndescription: " + ("word " * 400) + "\n---\n",
    )
    entry = catalog.scan_filesystem("claude")[0]
    assert len(entry["description"]) <= catalog._MAX_DESCRIPTION_CHARS


def test_unreadable_file_contributes_nothing(fake_home):
    write(fake_home / ".claude/skills/ok/SKILL.md", "---\nname: ok\ndescription: fine\n---\n")
    bad = fake_home / ".claude/skills/locked/SKILL.md"
    write(bad, "---\nname: locked\n---\n")
    os.chmod(bad, 0o000)
    try:
        names = [e["name"] for e in catalog.scan_filesystem("claude")]
        assert "ok" in names
        assert "locked" not in names
    finally:
        os.chmod(bad, 0o644)


# ---------------------------------------------------------------------------
# Scope: user vs project vs plugin
# ---------------------------------------------------------------------------


def test_user_and_project_scopes_are_distinguished(fake_home, tmp_path):
    project = tmp_path / "proj"
    write(fake_home / ".claude/commands/deploy.md", "---\ndescription: User deploy\n---\n")
    write(project / ".claude/commands/deploy.md", "---\ndescription: Project deploy\n---\n")

    entries = catalog.scan_filesystem("claude", str(project))
    sources = sorted(e["source"] for e in entries if e["name"] == "/deploy")
    assert sources == ["project", "user"]
    descriptions = {e["source"]: e["description"] for e in entries if e["name"] == "/deploy"}
    assert descriptions == {"user": "User deploy", "project": "Project deploy"}


def test_project_scope_is_absent_without_a_project_dir(fake_home, tmp_path):
    project = tmp_path / "proj"
    write(project / ".claude/commands/only-here.md", "---\ndescription: Project only\n---\n")

    assert catalog.scan_filesystem("claude") == []
    assert len(catalog.scan_filesystem("claude", str(project))) == 1


def test_nested_commands_become_namespaced_names(fake_home):
    write(fake_home / ".claude/commands/git/sync.md", "---\ndescription: Sync\n---\n")
    entry = catalog.scan_filesystem("claude")[0]
    assert entry["name"] == "/git:sync"


def install_plugin(home, key: str, *, scope: str = "user", project: str | None = None,
                   enabled: bool | None = True, version: str = "1.0.0"):
    """Install a Claude plugin the way Claude Code records it; return its root.

    Files go under a *versioned cache directory*, as on a real install, so a
    test that derived the namespace from the path would get the version.
    """
    import json
    name, market = key.split("@")
    root = home / ".claude/plugins/cache" / market / name / version
    root.mkdir(parents=True, exist_ok=True)
    record_path = home / ".claude/plugins/installed_plugins.json"
    data = json.loads(record_path.read_text()) if record_path.exists() else {"version": 2, "plugins": {}}
    record = {"scope": scope, "installPath": str(root), "version": version}
    if project:
        record["projectPath"] = project
    data["plugins"][key] = [record]
    write(record_path, json.dumps(data))
    if enabled is not None:
        settings_path = home / ".claude/settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        settings.setdefault("enabledPlugins", {})[key] = enabled
        write(settings_path, json.dumps(settings))
    return root


def test_plugin_commands_are_namespaced_by_plugin_name(fake_home):
    root = install_plugin(fake_home, "code-review@official", version="76c85b7366c8")
    write(root / "commands/go.md", "---\ndescription: Review the diff\n---\n")
    entry = catalog.scan_filesystem("claude")[0]
    # The plugin's name, not the version directory that owns commands/.
    assert entry["name"] == "/code-review:go"
    assert entry["source"] == "plugin"
    assert entry["kind"] == "command"


def test_plugin_agents_and_skills_are_namespaced_too(fake_home):
    # Claude Code lists them as codex:rescue, code-simplifier:code-simplifier.
    root = install_plugin(fake_home, "secteam@official")
    write(root / "agents/auditor.md", "---\nname: auditor\ndescription: Audits\n---\n")
    write(root / "skills/threat/SKILL.md", "---\nname: threat\ndescription: Models\n---\n")
    entries = catalog.scan_filesystem("claude")
    assert {(e["kind"], e["name"], e["source"]) for e in entries} == {
        ("agent", "secteam:auditor", "plugin"),
        ("skill", "secteam:threat", "plugin"),
    }


def test_a_marketplace_plugin_that_is_not_installed_is_not_offered(fake_home):
    """The catalog used to glob every marketplace clone.

    Measured on the dev machine: 39 plugins in the official marketplace, 5
    enabled for the project -- so most "plugin" commands were ones the session
    did not have, and a palette would have offered them.
    """
    write(
        fake_home / ".claude/plugins/marketplaces/official/plugins/example/commands/hi.md",
        "---\ndescription: Not installed\n---\n",
    )
    write(
        fake_home / ".claude/plugins/marketplaces/solo/plugin/commands/one.md",
        "---\ndescription: Not installed either\n---\n",
    )
    assert catalog.scan_filesystem("claude") == []


def test_an_installed_plugin_that_is_disabled_is_not_offered(fake_home):
    root = install_plugin(fake_home, "noisy@official", enabled=False)
    write(root / "commands/x.md", "---\ndescription: Disabled\n---\n")
    assert catalog.scan_filesystem("claude") == []
    # Installed but never enabled anywhere: also off.
    root = install_plugin(fake_home, "quiet@official", enabled=None)
    write(root / "commands/y.md", "---\ndescription: Never enabled\n---\n")
    assert catalog.scan_filesystem("claude") == []


def test_a_project_scoped_plugin_applies_only_in_its_project(fake_home, tmp_path):
    project = tmp_path / "patina"
    project.mkdir()
    root = install_plugin(fake_home, "code-review@official", scope="project",
                          project=str(project), enabled=None)
    write(root / "commands/go.md", "---\ndescription: Review\n---\n")
    # Enabled by the project's own settings, as Claude Code records it.
    write(project / ".claude/settings.json",
          '{"enabledPlugins": {"code-review@official": true}}')

    assert by_name(catalog.scan_filesystem("claude", str(project)), "/code-review:go")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert catalog.scan_filesystem("claude", str(elsewhere)) == []
    assert catalog.scan_filesystem("claude") == []


def test_scope_holds_even_when_the_plugin_is_enabled_everywhere(fake_home, tmp_path):
    # Enablement and scope are separate gates: a user-level "enabled" does
    # not carry a project install into other projects.
    project = tmp_path / "patina"
    project.mkdir()
    root = install_plugin(fake_home, "code-review@official", scope="local",
                          project=str(project), enabled=True)
    write(root / "commands/go.md", "---\ndescription: Review\n---\n")
    assert by_name(catalog.scan_filesystem("claude", str(project)), "/code-review:go")
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert catalog.scan_filesystem("claude", str(elsewhere)) == []


def test_project_settings_can_disable_a_user_plugin(fake_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    root = install_plugin(fake_home, "codex@openai")
    write(root / "commands/rescue.md", "---\ndescription: Rescue\n---\n")
    write(project / ".claude/settings.local.json",
          '{"enabledPlugins": {"codex@openai": false}}')
    assert catalog.scan_filesystem("claude", str(project)) == []
    assert by_name(catalog.scan_filesystem("claude"), "/codex:rescue")


@pytest.mark.parametrize("record", [
    "not json",
    '{"plugins": []}',
    '{"plugins": {"x@y": "nope"}}',
    '{"plugins": {"x@y": [{"scope": "user"}]}}',
    '{"plugins": {"x@y": [{"installPath": 42}]}}',
])
def test_a_garbled_install_record_yields_no_plugins_and_never_raises(fake_home, record):
    write(fake_home / ".claude/plugins/installed_plugins.json", record)
    write(fake_home / ".claude/settings.json", '{"enabledPlugins": {"x@y": true}}')
    assert catalog.claude_plugins(None) == []
    assert catalog.scan_filesystem("claude") == []


def test_an_install_path_outside_the_config_root_is_refused(fake_home, tmp_path):
    # installed_plugins.json is data, and a path in it is not a licence to
    # read anywhere on disk.
    import json
    outside = tmp_path / "elsewhere"
    write(outside / "commands/x.md", "---\ndescription: Escaped\n---\n")
    write(fake_home / ".claude/plugins/installed_plugins.json", json.dumps(
        {"plugins": {"evil@m": [{"scope": "user", "installPath": str(outside)}]}}
    ))
    write(fake_home / ".claude/settings.json", '{"enabledPlugins": {"evil@m": true}}')
    # Refused at the source, so nothing outside the config root is even
    # globbed -- read_head's own check is the second line, not the first.
    assert catalog.claude_plugins(None) == []
    assert catalog.scan_filesystem("claude") == []


def test_skill_name_falls_back_to_its_directory(fake_home):
    write(fake_home / ".claude/skills/no-name/SKILL.md", "---\ndescription: Nameless\n---\n")
    entry = catalog.scan_filesystem("claude")[0]
    assert entry["name"] == "no-name"


def test_project_mcp_servers_are_catalogued(fake_home, tmp_path):
    project = tmp_path / "proj"
    write(
        project / ".mcp.json",
        '{"mcpServers": {"supabase": {"command": "npx supabase-mcp"}, "stripe": {}}}',
    )
    # Approved, as Claude Code requires before it loads a project's servers.
    write(fake_home / ".claude/settings.json", '{"enableAllProjectMcpServers": true}')
    entries = catalog.scan_filesystem("claude", str(project))
    assert {e["name"] for e in entries} == {"supabase", "stripe"}
    assert all(e["kind"] == "mcp" and e["source"] == "project" for e in entries)
    # The executable only: the rest of a one-string command can be a key.
    assert by_name(entries, "supabase")["description"] == "npx"


def test_malformed_mcp_json_yields_no_entries(fake_home, tmp_path):
    project = tmp_path / "proj"
    write(project / ".mcp.json", "{not json at all")
    assert catalog.scan_filesystem("claude", str(project)) == []


# ---------------------------------------------------------------------------
# MCP servers: exactly the ones each assistant loads (Phase 3's exit)
# ---------------------------------------------------------------------------
#
# Every rule below was measured on 2026-09-27 by having each CLI write its own
# config in a throwaway HOME and asking it (`<cli> mcp list`) what it loads.
# Before this, every assistant's palette was offered the project's .mcp.json
# servers -- which only Claude reads -- and nothing from where they keep theirs.


def mcp_names(assistant: str, project=None) -> set[str]:
    entries = catalog.scan_filesystem(assistant, str(project) if project else None)
    return {e["name"] for e in entries if e["kind"] == "mcp"}


def claude_json(home, data: dict) -> None:
    import json
    write(home / ".claude.json", json.dumps(data))


def test_claude_user_and_local_scope_servers_come_from_claude_json(fake_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    claude_json(fake_home, {
        "mcpServers": {"stripe": {"command": "npx"}},
        "projects": {
            os.path.realpath(project): {"mcpServers": {"postgres": {"command": "uvx"}}},
            "/somewhere/else": {"mcpServers": {"elsewhere": {}}},
        },
    })
    entries = catalog.scan_filesystem("claude", str(project))
    sources = {e["name"]: e["source"] for e in entries if e["kind"] == "mcp"}
    assert sources == {"stripe": "user", "postgres": "project"}


def test_local_scope_is_keyed_by_the_realpath(fake_home, tmp_path):
    # Measured: a project under /tmp is filed under /private/tmp.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    claude_json(fake_home, {"projects": {os.path.realpath(real): {"mcpServers": {"db": {}}}}})
    assert mcp_names("claude", link) == {"db"}


def test_mcp_servers_in_claude_settings_json_are_not_claude_codes(fake_home):
    # Measured: Claude Code ignores this key; it is Claude Desktop's shape.
    write(fake_home / ".claude/settings.json", '{"mcpServers": {"desktop-only": {}}}')
    assert mcp_names("claude") == set()


def test_claude_ai_connectors_are_claude_codes_servers(fake_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    claude_json(fake_home, {
        "claudeAiMcpEverConnected": ["claude.ai Gmail", "claude.ai Google Drive", 7, "", None],
        "projects": {os.path.realpath(project): {"disabledMcpServers": ["claude.ai Google Drive"]}},
    })
    everywhere = {e["name"]: e for e in catalog.scan_filesystem("claude") if e["kind"] == "mcp"}
    assert set(everywhere) == {"claude.ai Gmail", "claude.ai Google Drive"}
    assert everywhere["claude.ai Gmail"]["description"] == "claude.ai connector"
    assert catalog.invocation(everywhere["claude.ai Gmail"])[0] == "Use the claude.ai Gmail MCP server to"
    # Switched off in one project, and only there.
    assert mcp_names("claude", project) == {"claude.ai Gmail"}
    # They are the claude.ai account's, which only Claude Code signs in with.
    assert mcp_names("codex") == set()


def test_a_server_switched_off_for_the_project_is_not_offered(fake_home, tmp_path):
    """``/mcp`` → disable writes the project's ``disabledMcpServers``, and it
    holds for a server of any scope. A plugin's goes by its qualified name."""
    project = tmp_path / "proj"
    project.mkdir()
    root = install_plugin(fake_home, "supabase@official")
    write(root / ".mcp.json", '{"mcpServers": {"supabase": {}, "other": {}}}')
    write(root / ".claude-plugin/plugin.json", '{"mcpServers": {"inline": {}}}')
    # A plugin.json that points at its servers' file, as supabase's does.
    pointed = install_plugin(fake_home, "pointer@official")
    write(pointed / ".claude-plugin/plugin.json", '{"mcpServers": "./agents/.mcp.json"}')
    write(pointed / "agents/.mcp.json", '{"mcpServers": {"viaref": {}}}')
    write(project / ".mcp.json", '{"mcpServers": {"shared": {}, "kept": {}}}')
    claude_json(fake_home, {
        "mcpServers": {"stripe": {}, "blender": {}},
        "projects": {os.path.realpath(project): {
            "hasTrustDialogAccepted": True,
            "enabledMcpjsonServers": ["shared", "kept"],
            "mcpServers": {"postgres": {}, "redis": {}},
            "disabledMcpServers": [
                "stripe", "redis", "shared", "plugin:supabase:supabase", "plugin:supabase:inline",
                "plugin:pointer:viaref",
                "other",  # unqualified: not the plugin's server
            ],
        }},
    })
    assert mcp_names("claude", project) == {"blender", "postgres", "kept", "other"}
    # Nothing is switched off outside that project.
    assert mcp_names("claude") == {"stripe", "blender", "supabase", "other", "inline", "viaref"}


def _approval_case(fake_home, project, *, user=None, project_settings=None, local=None,
                   record=None, trusted=False):
    import json
    project.mkdir(exist_ok=True)
    write(project / ".mcp.json", '{"mcpServers": {"remotion": {"command": "npx"}}}')
    if user is not None:
        write(fake_home / ".claude/settings.json", json.dumps(user))
    if project_settings is not None:
        write(project / ".claude/settings.json", json.dumps(project_settings))
    if local is not None:
        write(project / ".claude/settings.local.json", json.dumps(local))
    claude_json(fake_home, {"projects": {os.path.realpath(project): {
        **(record or {}), "hasTrustDialogAccepted": trusted,
    }}})


APPROVE = {"enabledMcpjsonServers": ["remotion"]}
REJECT = {"disabledMcpjsonServers": ["remotion"]}


@pytest.mark.parametrize("case,loaded", [
    ({}, False),                                                  # "Pending approval"
    ({"user": APPROVE}, True),
    ({"user": {"enableAllProjectMcpServers": True}}, True),
    ({"local": APPROVE}, False),                                     # untrusted folder
    ({"local": APPROVE, "trusted": True}, True),
    ({"local": {"enableAllProjectMcpServers": True}}, False),
    ({"local": {"enableAllProjectMcpServers": True}, "trusted": True}, True),
    ({"project_settings": APPROVE, "trusted": True}, True),
    ({"project_settings": APPROVE}, False),
    ({"record": APPROVE, "trusted": True}, True),
    ({"record": APPROVE}, False),
    ({"user": APPROVE, "local": REJECT}, False),                        # a rejection anywhere wins
    ({"user": APPROVE, "record": REJECT, "trusted": True}, False),
    ({"user": REJECT, "local": APPROVE, "trusted": True}, False),
])
def test_a_project_mcp_json_server_loads_as_claude_code_decides(fake_home, tmp_path, case, loaded):
    project = tmp_path / "proj"
    _approval_case(fake_home, project, **case)
    assert ("remotion" in mcp_names("claude", project)) is loaded


def test_a_changed_approval_is_not_served_from_the_cache(fake_home, tmp_path):
    project = tmp_path / "proj"
    _approval_case(fake_home, project, trusted=True)
    names = lambda: {e["name"] for e in catalog.scan_filesystem_cached("claude", str(project))[0]}
    assert "remotion" not in names()
    write(project / ".claude/settings.local.json", '{"enabledMcpjsonServers": ["remotion"]}')
    assert "remotion" in names()


def test_an_approval_in_user_settings_is_not_served_from_the_cache(fake_home, tmp_path):
    project = tmp_path / "proj"
    _approval_case(fake_home, project, user={})
    names = lambda: {e["name"] for e in catalog.scan_filesystem_cached("claude", str(project))[0]}
    assert "remotion" not in names()
    write(fake_home / ".claude/settings.json", '{"enabledMcpjsonServers": ["remotion"]}')
    assert "remotion" in names()


def test_the_dev_machines_rejected_server_is_not_offered(fake_home, tmp_path):
    # voxherd itself: remotion-documentation rejected in settings.local.json,
    # and until now the only MCP server any assistant's palette offered here.
    project = tmp_path / "voxherd"
    _approval_case(fake_home, project, local=REJECT, trusted=True)
    assert mcp_names("claude", project) == set()


def test_a_plugins_servers_come_with_the_plugin(fake_home):
    root = install_plugin(fake_home, "supabase@official")
    write(root / ".claude-plugin/plugin.json", '{"name": "supabase", "mcpServers": "./agents/claude/.mcp.json"}')
    write(root / "agents/claude/.mcp.json",
          '{"mcpServers": {"supabase": {"type": "http", "url": "https://mcp.example/x", "headers": {}}}}')
    entry = by_name(catalog.scan_filesystem("claude"), "supabase")
    assert entry["kind"] == "mcp" and entry["source"] == "plugin"
    assert entry["description"] == ""


def test_a_plugins_servers_inline_or_at_its_root(fake_home):
    root = install_plugin(fake_home, "tools@official")
    write(root / ".claude-plugin/plugin.json", '{"mcpServers": {"inline": {"command": "node"}}}')
    write(root / ".mcp.json", '{"mcpServers": {"rooted": {}}}')
    assert mcp_names("claude") == {"inline", "rooted"}


def test_a_plugin_cannot_point_its_servers_outside_itself(fake_home, tmp_path):
    root = install_plugin(fake_home, "sly@official")
    write(fake_home / ".claude/elsewhere.json", '{"mcpServers": {"escaped": {}}}')
    write(root / ".claude-plugin/plugin.json", '{"mcpServers": "../../../../../elsewhere.json"}')
    assert mcp_names("claude") == set()


def test_a_disabled_plugins_servers_are_not_offered(fake_home):
    root = install_plugin(fake_home, "off@official", enabled=False)
    write(root / ".mcp.json", '{"mcpServers": {"off": {}}}')
    assert mcp_names("claude") == set()


def test_no_secret_in_a_servers_config_reaches_an_entry(fake_home, tmp_path):
    # The shapes measured on the dev machine: a live key in args, bearer
    # tokens in headers -- plus env, a key in a URL, and a whole command line
    # in "command".
    import json
    secret = "sk_live_DO_NOT_LEAK"
    claude_json(fake_home, {"mcpServers": {
        "args-key": {"command": "npx", "args": ["-y", "@stripe/mcp", f"--api-key={secret}"]},
        "header-key": {"type": "http", "url": f"https://api.example/mcp?key={secret}",
                       "headers": {"Authorization": f"Bearer {secret}"}},
        "env-key": {"command": "/usr/local/bin/uvx", "env": {"TOKEN": secret}},
        "one-line": {"command": f"npx server --token {secret}"},
    }})
    entries = catalog.scan_filesystem("claude")
    assert secret not in json.dumps(entries)
    assert {e["name"]: e["description"] for e in entries} == {
        "args-key": "npx", "header-key": "", "env-key": "uvx", "one-line": "npx",
    }


def test_codex_servers_come_from_its_config_toml(fake_home, tmp_path):
    write(fake_home / ".codex/config.toml",
          '[mcp_servers.github]\nurl = "https://x"\n\n[mcp_servers.computer-use]\ncommand = "x"\nenabled = false\n')
    project = tmp_path / "proj"
    write(project / ".mcp.json", '{"mcpServers": {"claude-only": {}}}')
    assert mcp_names("codex", project) == {"github"}


def test_grok_servers_come_from_its_config_toml_in_both_scopes(fake_home, tmp_path):
    write(fake_home / ".grok/config.toml",
          'disabled_mcp_servers = ["listed-off"]\n\n'
          '[mcp_servers.mine]\ncommand = "echo"\nenabled = true\n\n'
          '[mcp_servers.flag-off]\ncommand = "echo"\nenabled = false\n\n'
          '[mcp_servers.listed-off]\ncommand = "echo"\n')
    project = tmp_path / "proj"
    write(project / ".grok/config.toml", '[mcp_servers.shared]\ncommand = "echo"\n')
    write(project / ".mcp.json", '{"mcpServers": {"claude-only": {}}}')
    assert mcp_names("grok", project) == {"mine", "shared"}
    assert mcp_names("composer", project) == {"mine", "shared"}


def test_antigravity_servers_come_from_its_own_file(fake_home, tmp_path):
    write(fake_home / ".gemini/config/mcp_config.json",
          '{"mcpServers": {"on": {"command": "echo"}, "off": {"command": "echo", "disabled": true}}}')
    project = tmp_path / "proj"
    write(project / ".mcp.json", '{"mcpServers": {"claude-only": {}}}')
    assert mcp_names("antigravity", project) == {"on"}


def test_antigravitys_empty_config_is_no_servers(fake_home):
    # What `agy` leaves on the dev machine: a zero-byte mcp_config.json.
    write(fake_home / ".gemini/config/mcp_config.json", "")
    assert mcp_names("antigravity") == set()


def _gemini(fake_home, project, trust=None):
    import json
    write(fake_home / ".gemini/settings.json", '{"mcpServers": {"user-srv": {"command": "echo"}}}')
    write(project / ".gemini/settings.json", '{"mcpServers": {"proj-srv": {"command": "echo"}}}')
    write(fake_home / ".gemini/extensions/ext/gemini-extension.json", '{"mcpServers": {"ext-srv": {}}}')
    # Antigravity's file, in the same tree: Gemini does not read it.
    write(fake_home / ".gemini/config/mcp_config.json", '{"mcpServers": {"agy-srv": {}}}')
    write(project / ".mcp.json", '{"mcpServers": {"claude-only": {}}}')
    if trust is not None:
        write(fake_home / ".gemini/trustedFolders.json", json.dumps(trust))


def test_gemini_loads_no_server_in_a_folder_it_does_not_trust(fake_home, tmp_path):
    # Measured: "MCP servers are configured but disabled because this folder
    # is untrusted. User-level servers are also suppressed".
    project = tmp_path / "proj"
    _gemini(fake_home, project)
    assert mcp_names("gemini", project) == set()


def test_gemini_in_a_trusted_folder(fake_home, tmp_path):
    project = tmp_path / "proj"
    _gemini(fake_home, project, trust={os.path.realpath(project): "TRUST_FOLDER"})
    entries = catalog.scan_filesystem("gemini", str(project))
    assert {e["name"]: e["source"] for e in entries if e["kind"] == "mcp"} == {
        "user-srv": "user", "proj-srv": "project", "ext-srv": "plugin",
    }


@pytest.mark.parametrize("trust,trusted", [
    ("parent-of-sibling", True),     # TRUST_PARENT on a sibling trusts the shared parent
    ("parent-folder", True),         # TRUST_FOLDER on a parent covers the project
    ("do-not-trust", False),
    ("trust-disabled", True),        # security.folderTrust.enabled = false
])
def test_gemini_trust_levels(fake_home, tmp_path, trust, trusted):
    import json
    project = tmp_path / "work" / "proj"
    sibling = tmp_path / "work" / "other"
    sibling.mkdir(parents=True)
    rules = {
        "parent-of-sibling": {os.path.realpath(sibling): "TRUST_PARENT"},
        "parent-folder": {os.path.realpath(tmp_path / "work"): "TRUST_FOLDER"},
        "do-not-trust": {os.path.realpath(tmp_path / "work"): "TRUST_FOLDER",
                         os.path.realpath(project): "DO_NOT_TRUST"},
        "trust-disabled": None,
    }[trust]
    _gemini(fake_home, project, trust=rules)
    if trust == "trust-disabled":
        write(fake_home / ".gemini/settings.json", json.dumps({
            "security": {"folderTrust": {"enabled": False}},
            "mcpServers": {"user-srv": {"command": "echo"}},
        }))
    assert ("user-srv" in mcp_names("gemini", project)) is trusted


def test_the_same_server_in_two_scopes_is_one_row(fake_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    claude_json(fake_home, {
        "mcpServers": {"stripe": {}},
        "projects": {os.path.realpath(project): {"mcpServers": {"stripe": {}}}},
    })
    rows = [e for e in catalog.scan_filesystem("claude", str(project)) if e["kind"] == "mcp"]
    assert [e["name"] for e in rows] == ["stripe"]


def test_claude_json_is_the_only_file_outside_the_roots_a_read_may_open(fake_home):
    write(fake_home / ".claude.json", "{}")
    write(fake_home / ".bashrc", "export SECRET=1")
    allowed = catalog._allowed_roots(None)
    assert catalog.read_head(str(fake_home / ".claude.json"), allowed) == "{}"
    assert catalog.read_head(str(fake_home / ".bashrc"), allowed) == ""


# ---------------------------------------------------------------------------
# Per-assistant dispatch
# ---------------------------------------------------------------------------


def test_each_assistant_scans_its_own_tree(fake_home):
    write(fake_home / ".claude/skills/csk/SKILL.md", "---\nname: csk\ndescription: c\n---\n")
    write(fake_home / ".codex/skills/xsk/SKILL.md", "---\nname: xsk\ndescription: x\n---\n")
    write(fake_home / ".grok/bundled/agents/explore.md", "---\nname: explore\n---\n")
    write(
        fake_home / ".gemini/antigravity-cli/builtin/skills/auto/SKILL.md",
        "---\nname: auto\ndescription: a\n---\n",
    )

    assert [e["name"] for e in catalog.scan_filesystem("claude")] == ["csk"]
    assert [e["name"] for e in catalog.scan_filesystem("codex")] == ["xsk"]
    assert [e["name"] for e in catalog.scan_filesystem("grok")] == ["explore"]
    assert [e["name"] for e in catalog.scan_filesystem("antigravity")] == ["auto"]
    # Gemini's own tree is genuinely sparse — antigravity-cli is not its own.
    assert catalog.scan_filesystem("gemini") == []


def test_antigravity_reads_skills_where_agy_does(fake_home, tmp_path):
    """agy's bundled agy-customizations skill documents it: the user's under
    ~/.gemini/config/, a workspace's under .agents/ (or .agent/, _agents/,
    _agent/). Never Gemini CLI's .gemini/commands."""
    project = tmp_path / "proj"
    write(fake_home / ".gemini/config/skills/mine/SKILL.md", "---\nname: mine\ndescription: m\n---\n")
    for root, name in ((".agents", "one"), (".agent", "two"), ("_agents", "three"), ("_agent", "four")):
        write(project / root / "skills" / name / "SKILL.md", f"---\nname: {name}\n---\n")
    write(project / ".gemini/commands/deploy.toml", 'description = "Deploy"\n')
    write(fake_home / ".gemini/antigravity-cli/skills/guess/SKILL.md", "---\nname: guess\n---\n")
    sources = {e["name"]: e["source"] for e in catalog.scan_filesystem("antigravity", str(project))}
    assert sources == {"mine": "user", "one": "project", "two": "project", "three": "project",
                       "four": "project"}
    assert {e["name"] for e in catalog.scan_filesystem("gemini", str(project))} == {"deploy"}


def test_composer_shares_the_grok_tree(fake_home):
    write(fake_home / ".grok/bundled/skills/design/SKILL.md", "---\nname: design\n---\n")
    grok = catalog.scan_filesystem("grok")
    composer = catalog.scan_filesystem("composer")
    assert [e["name"] for e in grok] == [e["name"] for e in composer] == ["design"]
    assert composer[0]["assistant"] == "composer"


def test_grok_bundled_content_is_marked_builtin(fake_home):
    write(fake_home / ".grok/bundled/skills/design/SKILL.md", "---\nname: design\n---\n")
    write(fake_home / ".grok/skills/mine/SKILL.md", "---\nname: mine\n---\n")
    sources = {e["name"]: e["source"] for e in catalog.scan_filesystem("grok")}
    assert sources == {"design": "builtin", "mine": "user"}


def test_unknown_assistant_has_no_scan_targets(fake_home):
    assert catalog.scan_targets("nonesuch", None) == []
    assert catalog.scan_filesystem("nonesuch") == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude", "claude"),
        ("CLAUDE", "claude"),
        ("claude-code", "claude"),
        ("agy", "antigravity"),
        ("grok-build", "grok"),
        ("", "claude"),
        (None, "claude"),
        ("totally-unknown", "claude"),
    ],
)
def test_assistant_normalization(raw, expected):
    assert catalog.normalize_catalog_assistant(raw) == expected


# ---------------------------------------------------------------------------
# Tier 1 (static manifest) and Tier 3 (probe seam)
# ---------------------------------------------------------------------------


def test_builtin_manifest_is_present_for_every_assistant():
    for assistant in catalog.CATALOG_ASSISTANTS:
        entries = catalog.builtin_entries(assistant)
        commands = [e for e in entries if e["kind"] == "command"]
        assert commands, f"{assistant} has no Tier 1 manifest"
        assert catalog.BUILTIN_MEASURED[assistant], f"{assistant}'s table says no CLI version"
        for entry in entries:
            assert entry["source"] == "builtin"
            assert entry["kind"] in ("command", "agent")
            assert entry["assistant"] == assistant
            assert entry["description"], entry["name"]
            assert len(entry["description"]) <= catalog._MAX_DESCRIPTION_CHARS, entry["name"]
            assert len(entry.get("argument_hint", "")) <= catalog._MAX_HINT_CHARS, entry["name"]
        for entry in commands:
            assert re.fullmatch(r"/[a-z][a-z0-9-]*", entry["name"]), entry["name"]
        names = [(e["kind"], e["name"]) for e in entries]
        assert len(names) == len(set(names)), f"{assistant} lists an entry twice"


# Measured per build (catalog.BUILTIN_MEASURED): everyday commands each CLI
# has -- several missing from the five-row stub this replaced -- and ones it
# has, or once had, that a palette must not offer: hidden, debug, headless-
# only, other-platform, default-off, removed, and aliases.
@pytest.mark.parametrize("assistant, present, absent", [
    ("claude",
     {"/clear", "/compact", "/context", "/model", "/resume", "/rewind", "/plan", "/permissions",
      "/hooks", "/memory", "/mcp", "/btw", "/tasks", "/usage", "/code-review", "/simplify",
      "/loop", "/security-review", "/config", "/export"},
     {"/agents", "/heapdump", "/focus", "/version", "/stop", "/__remote-workflow", "/cost",
      "/ultraplan", "/wellbeing", "/loops", "/pause-memory", "/review", "/checkup"}),
    ("codex",
     {"/model", "/review", "/approve", "/side", "/btw", "/goal", "/plan", "/compact", "/new",
      "/resume", "/fork", "/mcp", "/status", "/diff", "/init", "/quit", "/personality"},
     {"/setup-default-sandbox", "/sandbox-add-read-dir", "/rollout", "/test-approval",
      "/debug-m-drop", "/debug-m-update", "/cwd", "/pet", "/clean"}),
    ("gemini",
     {"/chat", "/compress", "/memory", "/stats", "/tools", "/mcp", "/permissions", "/plan",
      "/skills", "/resume", "/rewind", "/model", "/quit"},
     {"/corgi", "/oncall", "/profile", "/restore", "/voice", "/upgrade", "/summarize",
      "/compact", "/exit", "/usage"}),
    ("grok",
     {"/model", "/compact", "/goal", "/loop", "/resume", "/rename", "/privacy", "/settings",
      "/skills", "/usage", "/voice", "/auto", "/new", "/deep-research", "/imagine",
      "/hooks-trust", "/btw"},
     {"/gboom", "/debug", "/scroll-debug", "/yolo", "/mem", "/clear", "/status", "/info"}),
    ("antigravity",
     {"/model", "/btw", "/codesearch", "/resume", "/rewind", "/fork", "/effort", "/plan",
      "/credits", "/remote-control", "/help", "/plugin"},
     # /fast and /planning: gone in 1.2.10 though its docs page lists them.
     # /automation: a skill, catalogued from disk as one.
     {"/fast", "/planning", "/bug", "/automation", "/boost", "/teamwork-preview", "/quit",
      "/undo", "/branch", "/cs", "/search", "/settings", "/switch"}),
])
def test_each_builtin_table_is_the_measured_one(assistant, present, absent):
    names = {e["name"] for e in catalog.builtin_entries(assistant) if e["kind"] == "command"}
    assert present <= names, present - names
    assert not absent & names, absent & names


def test_composer_is_grok_with_another_model():
    grok = catalog.builtin_entries("grok")
    composer = catalog.builtin_entries("composer")
    assert [{**e, "assistant": "-"} for e in grok] == [{**e, "assistant": "-"} for e in composer]


@pytest.mark.parametrize("assistant, marked", [
    ("claude", True), ("codex", True), ("antigravity", True),
    ("gemini", False), ("grok", False), ("composer", False),
])
def test_a_model_picker_that_rewrites_the_default_says_so(assistant, marked):
    model = by_name(catalog.builtin_entries(assistant), "/model")
    assert ("default for new sessions" in model["description"]) is marked


def test_codex_personality_is_saved_as_the_default_too():
    personality = by_name(catalog.builtin_entries("codex"), "/personality")
    assert "default for new sessions" in personality["description"]


def test_a_builtin_carries_its_hint_and_whether_it_needs_words():
    grok = catalog.builtin_entries("grok")
    btw = by_name(grok, "/btw")
    assert btw["argument_hint"] == "<question>" and btw["needs_args"] is True
    assert catalog.invocation(btw) == ("/btw", True)
    resume = by_name(grok, "/resume")
    assert "argument_hint" not in resume and "needs_args" not in resume
    assert catalog.invocation(resume) == ("/resume", False)
    # A hint alone does not make words necessary: /compact's are optional.
    compact = by_name(catalog.builtin_entries("claude"), "/compact")
    assert compact["argument_hint"] and catalog.invocation(compact) == ("/compact", False)
    codesearch = by_name(catalog.builtin_entries("antigravity"), "/codesearch")
    assert catalog.invocation(codesearch) == ("/codesearch", True)


def test_claude_code_s_own_agents_are_built_in():
    agents = {e["name"]: e for e in catalog.builtin_entries("claude") if e["kind"] == "agent"}
    assert {"Explore", "Plan", "general-purpose"} <= set(agents)
    assert catalog.invocation(agents["Explore"]) == ("Use the Explore agent to", True)
    assert not [e for e in catalog.builtin_entries("codex") if e["kind"] == "agent"]


def test_probe_cache_starts_empty_and_is_stale():
    catalog.clear_cache()
    assert catalog.probe_entries("claude") == []
    assert catalog.probe_is_stale("claude") is True
    assert catalog.probe_age_seconds("claude") is None


def test_the_probe_seam_keeps_a_commands_hint_and_nothing_elses():
    catalog.clear_cache()
    catalog.store_probe_entries("claude", [
        {"kind": "command", "name": "/x", "argument_hint": "  [ y ]  "},
        {"kind": "agent", "name": "a", "argument_hint": "[z]"},
    ])
    entries = catalog.probe_entries("claude")
    assert by_name(entries, "/x")["argument_hint"] == "[ y ]"
    assert "argument_hint" not in by_name(entries, "a")
    catalog.clear_cache()


def test_probe_seam_stores_and_normalizes_entries():
    catalog.clear_cache()
    catalog.store_probe_entries(
        "claude",
        [
            {"kind": "plugin", "name": "code-review", "description": "  a   plugin  "},
            {"kind": "bogus-kind", "name": "fallback", "source": "bogus-source"},
            {"name": ""},  # dropped: no name
            "not a dict",  # dropped: wrong type
        ],
    )
    entries = catalog.probe_entries("claude")
    assert len(entries) == 2
    assert by_name(entries, "code-review")["description"] == "a plugin"
    fallback = by_name(entries, "fallback")
    assert fallback["kind"] == "command"
    assert fallback["source"] == "plugin"
    assert catalog.probe_is_stale("claude") is False
    catalog.clear_cache()


# --- Tier 3, running: codex mcp list ------------------------------------------------

# One record of `codex mcp list --json` as it came back on the dev machine, the
# key replaced: the transport carries Authorization headers and command lines.
CODEX_LIST = """[
  {"name": "github", "enabled": true, "transport": {"type": "streamable_http",
   "url": "https://api.example/mcp", "http_headers": {"Authorization": "Bearer ghp_DO_NOT_LEAK"}}},
  {"name": "cua_repl", "enabled": true, "transport": {"type": "stdio", "command": "node",
   "args": ["--key=sk_live_DO_NOT_LEAK"], "env": {"TOKEN": "DO_NOT_LEAK"}}},
  {"name": "codex_app", "enabled": false, "transport": {"type": "stdio", "command": "x"}}
]"""


def test_the_codex_probe_keeps_names_and_nothing_else():
    entries = catalog._parse_codex_mcp_list(CODEX_LIST)
    assert [e["name"] for e in entries] == ["github", "cua_repl"]
    assert "DO_NOT_LEAK" not in repr(entries)
    assert {k for e in entries for k in e} == {"kind", "name", "description", "source"}


def fake_probe(monkeypatch, code: str, assistant: str = "codex"):
    """Make *assistant*'s probe a Python one-liner, so no real CLI runs."""
    import sys
    monkeypatch.setattr(catalog, "_PROBES", {
        assistant: (((sys.executable, "-c", code), catalog._parse_codex_mcp_list),),
    })


async def test_a_probe_run_is_cached_as_tier_3(fake_home, monkeypatch):
    fake_probe(monkeypatch, f"print({CODEX_LIST!r})")
    assert await catalog.refresh_probe("codex") is True
    assert {e["name"] for e in catalog.probe_entries("codex")} == {"github", "cua_repl"}
    assert catalog.probe_is_stale("codex") is False


@pytest.mark.parametrize("code", [
    "import sys; sys.exit(2)",                # the CLI failed
    "print('not json')",                      # it said something else
    "print('[]'); import sys; sys.exit(1)",   # failed, but its output parses: still not the truth
])
async def test_a_failed_probe_keeps_what_was_cached(fake_home, monkeypatch, code):
    catalog.store_probe_entries("codex", [{"kind": "mcp", "name": "kept"}])
    fake_probe(monkeypatch, code)
    assert await catalog.refresh_probe("codex") is False
    assert [e["name"] for e in catalog.probe_entries("codex")] == ["kept"]


async def test_a_missing_cli_is_a_failed_probe_not_an_error(fake_home, monkeypatch):
    monkeypatch.setattr(catalog, "_PROBES", {
        "codex": ((("no-such-cli-anywhere", "mcp", "list"), catalog._parse_codex_mcp_list),),
    })
    assert await catalog.refresh_probe("codex") is False


async def test_a_slow_probe_is_killed(fake_home, monkeypatch):
    monkeypatch.setattr(catalog, "_PROBE_TIMEOUT_SECONDS", 0.2)
    fake_probe(monkeypatch, "import time; time.sleep(10)")
    started = asyncio.get_running_loop().time()
    assert await catalog.refresh_probe("codex") is False
    assert asyncio.get_running_loop().time() - started < 5


async def test_a_timed_out_probe_is_not_left_running(fake_home, monkeypatch, tmp_path):
    # A hung CLI, every five minutes, would pile up otherwise.
    marker = tmp_path / "still-running"
    monkeypatch.setattr(catalog, "_PROBE_TIMEOUT_SECONDS", 0.2)
    fake_probe(monkeypatch, f"import time; time.sleep(1); open({str(marker)!r}, 'w').close()")
    assert await catalog.refresh_probe("codex") is False
    await asyncio.sleep(1.5)
    assert not marker.exists()


async def test_a_probe_stopped_mid_run_leaves_no_cli_behind(fake_home, monkeypatch, tmp_path):
    marker = tmp_path / "still-running"
    fake_probe(monkeypatch, f"import time; time.sleep(1); open({str(marker)!r}, 'w').close()")
    catalog.refresh_probe_if_stale("codex")
    await asyncio.sleep(0.4)           # running: its output is being awaited
    await catalog.stop_probes()
    await asyncio.sleep(1.5)
    assert not marker.exists()


async def test_stop_probes_returns_only_once_they_have_ended(fake_home, monkeypatch):
    # What the shutdown relies on: nothing of ours left for the loop's teardown.
    fake_probe(monkeypatch, "import time; time.sleep(5)")
    catalog.refresh_probe_if_stale("codex")
    await asyncio.sleep(0.3)
    assert catalog._PROBE_TASKS
    await catalog.stop_probes()
    assert not catalog._PROBE_TASKS


def test_a_probe_caught_mid_spawn_does_not_hang_shutdown(tmp_path):
    """Measured on Python 3.11: asyncio.run() cancelling every task at once,
    with a probe inside create_subprocess_exec, never returns. stop_probes()
    ends the probe first; this runs a real asyncio.run() to prove it."""
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2])
    script = tmp_path / "shutdown.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, sys
        sys.path.insert(0, {root!r})
        from bridge import catalog
        catalog._PROBES = {{"codex": (((sys.executable, "-c", "import time; time.sleep(30)"),
                                        catalog._parse_codex_mcp_list),)}}

        async def main():
            catalog.refresh_probe_if_stale("codex")
            await asyncio.sleep(0)          # the probe is now inside create_subprocess_exec
            await catalog.stop_probes()

        asyncio.run(main())
        print("closed")
    """))
    try:
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("the event loop hung closing with a probe mid-spawn")
    assert "closed" in result.stdout, result.stderr[-500:]


async def test_the_bridge_stops_its_probes_before_its_loop_closes(monkeypatch):
    from unittest.mock import AsyncMock, patch

    from bridge import bridge_server
    from bridge import server_state as state

    # The lifespan builds these afresh; put the originals back afterwards.
    for name in ("narration", "_prune_task", "_activity_poll_task", "_bonjour_task"):
        monkeypatch.setattr(state, name, getattr(state, name))
    stop = AsyncMock()
    monkeypatch.setattr(catalog, "stop_probes", stop)
    with (
        patch.object(bridge_server.mac_tts, "start", return_value=None),
        patch.object(bridge_server.mac_tts, "stop", return_value=None),
        patch.object(bridge_server.sessions, "prune_dead", new_callable=AsyncMock, return_value=[]),
        patch.object(bridge_server.sessions, "_sessions", {}),
        patch("bridge.bridge_server._init_voice_loop", return_value=None),
        patch("bridge.tmux_manager.cleanup_stale_sessions", return_value=[]),
        patch("bridge.bridge_server._discover_tmux_sessions", new_callable=AsyncMock, return_value=0),
        patch("bridge.bridge_server._periodic_prune", new_callable=AsyncMock),
        patch("bridge.bridge_server._activity_poll_loop", new_callable=AsyncMock),
        patch("bridge.bridge_server.bonjour") as bonjour,
    ):
        bonjour.register = lambda **kwargs: None
        bonjour.unregister = lambda: None
        async with bridge_server._lifespan(bridge_server.app):
            stop.assert_not_awaited()
    stop.assert_awaited_once()


async def test_a_catalog_request_refreshes_tier_3_behind_itself(fake_home, monkeypatch):
    fake_probe(monkeypatch, f"print({CODEX_LIST!r})")
    first = await catalog.build_catalog("codex")
    # Answered from cache: the probe had not run when this came back.
    assert first["probe_stale"] is True
    assert "cua_repl" not in {e["name"] for e in first["entries"]}
    await asyncio.wait_for(asyncio.gather(*catalog._PROBE_TASKS), timeout=10)
    second = await catalog.build_catalog("codex")
    assert "cua_repl" in {e["name"] for e in second["entries"]}


async def test_a_failed_probe_is_not_retried_on_every_open(fake_home, monkeypatch):
    fake_probe(monkeypatch, "import sys; sys.exit(1)")
    await catalog.build_catalog("codex")
    await asyncio.wait_for(asyncio.gather(*catalog._PROBE_TASKS), timeout=10)
    await asyncio.sleep(0)
    await catalog.build_catalog("codex")
    assert not catalog._PROBE_TASKS, "a second probe started straight after a failure"


async def test_a_server_both_tiers_found_is_one_row(fake_home, monkeypatch):
    write(fake_home / ".codex/config.toml", '[mcp_servers.github]\nurl = "https://x"\n')
    catalog.store_probe_entries("codex", catalog._parse_codex_mcp_list(CODEX_LIST))
    payload = await catalog.build_catalog("codex")
    mcp = [e["name"] for e in payload["entries"] if e["kind"] == "mcp"]
    assert sorted(mcp) == ["cua_repl", "github"]


# --- Tier 3, running: Claude Code's initialize answer --------------------------------

# The shape of 2.1.283's answer (trimmed), plus what the parser must refuse.
CLAUDE_ANSWER = json.dumps({"type": "control_response", "response": {
    "subtype": "success", "request_id": "voxherd-catalog", "response": {
        "commands": [
            {"name": "simplify", "description": "Review the changed code", "argumentHint": "[<target>]",
             "aliases": None, "builtin": True},
            {"name": "brand-new", "description": "Shipped after the table was measured"},
            {"name": "focus", "description": "Toggle focus view", "argumentHint": "[on|off]"},
            {"name": "__remote-workflow", "description": "Server-launched sessions only"},
            {"name": "agents", "description": "(removed) Ask Claude to create/manage subagents"},
            "not a dict",
            {"description": "no name"},
        ],
        "agents": [{"name": "Explore", "description": "Fast read-only search agent"}, {"name": ""}, None],
        "account": {"email": "someone@example.com", "organization": "DO_NOT_LEAK"},
        "models": [{"value": "opus"}],
    }}})


def test_the_claude_probe_keeps_built_ins_a_terminal_has():
    entries = catalog._parse_claude_initialize("some noise\n" + CLAUDE_ANSWER + "\n")
    assert [(e["kind"], e["name"]) for e in entries] == [
        ("command", "/simplify"), ("command", "/brand-new"), ("agent", "Explore"),
    ]
    assert entries[0]["argument_hint"] == "[<target>]" and "argument_hint" not in entries[1]
    assert "someone@example.com" not in repr(entries) and "DO_NOT_LEAK" not in repr(entries)


@pytest.mark.parametrize("stdout", ["", "not json\n", '{"type": "system"}\n',
                                    '{"type": "control_response", "response": "nope"}\n'])
def test_a_claude_answer_without_its_commands_is_a_failed_probe(stdout):
    with pytest.raises(ValueError):
        catalog._parse_claude_initialize(stdout)


def test_the_shipped_claude_probe_cannot_reach_the_users_setup():
    """What makes it safe to run behind every palette open: signed out, in an
    empty HOME, no settings, hooks or MCP servers, and no prompt -- so no
    model call, and nothing of the user's is read or written."""
    (probe,) = SHIPPED_PROBES["claude"]
    assert probe.isolated is True
    assert probe.stdin == catalog._CLAUDE_INITIALIZE
    assert json.loads(probe.stdin)["request"] == {"subtype": "initialize"}
    argv = list(probe.argv)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert probe.parse is catalog._parse_claude_initialize


async def test_an_isolated_probe_runs_in_an_empty_home_and_is_sent_its_request(
    fake_home, monkeypatch, tmp_path,
):
    import sys
    import warnings
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-DO_NOT_PASS")
    monkeypatch.setenv("VOXHERD_QUIET", "1")
    # Claude Code sets this for its own children; a probe must not rely on it.
    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    report = tmp_path / "seen.json"
    code = f"""
import json, os, sys
request = json.loads(sys.stdin.readline())
json.dump({{
    "home": os.environ.get("HOME"), "cwd": os.getcwd(), "home_was_empty": os.listdir(os.environ["HOME"]) == [],
    "config": os.environ.get("CLAUDE_CONFIG_DIR"), "key": os.environ.get("ANTHROPIC_API_KEY"),
    "quiet": os.environ.get("VOXHERD_QUIET"), "autoupdate_off": os.environ.get("DISABLE_AUTOUPDATER"),
    "quiet_network": os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
    "subtype": request["request"]["subtype"],
}}, open({str(report)!r}, "w"))
print(json.dumps({{"type": "control_response", "response": {{"response": {{
    "commands": [{{"name": "probe", "description": "d"}}]}}}}}}))
"""
    monkeypatch.setattr(catalog, "_PROBES", {"claude": (catalog._Probe(
        (sys.executable, "-c", code), catalog._parse_claude_initialize,
        stdin=catalog._CLAUDE_INITIALIZE, isolated=True,
    ),)})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert await catalog.refresh_probe("claude") is True
    assert by_name(catalog.probe_entries("claude"), "/probe")
    # Cleaned up by the probe, not left to the garbage collector.
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)], caught
    seen = json.loads(report.read_text())
    assert seen["subtype"] == "initialize"
    assert os.path.realpath(seen["home"]) == os.path.realpath(seen["cwd"])
    assert os.path.realpath(seen["home"]) != os.path.realpath(fake_home)
    assert seen["home_was_empty"] is True
    assert seen["config"] is None and seen["key"] is None and seen["quiet"] is None
    assert seen["autoupdate_off"] == "1" and seen["quiet_network"] == "1"
    # And the scratch HOME is gone again.
    assert not os.path.exists(seen["home"])


async def test_a_probe_without_isolation_keeps_the_real_home(fake_home, monkeypatch, tmp_path):
    report = tmp_path / "home.txt"
    code = f"import os; open({str(report)!r}, 'w').write(os.environ['HOME']); print('[]')"
    fake_probe(monkeypatch, code)
    assert await catalog.refresh_probe("codex") is True
    assert os.path.realpath(report.read_text()) == os.path.realpath(fake_home)


async def test_tier_3_adds_only_what_tiers_1_and_2_do_not_have(fake_home):
    catalog.store_probe_entries("claude", catalog._parse_claude_initialize(CLAUDE_ANSWER))
    payload = await catalog.build_catalog("claude")
    rows = [(e["kind"], e["name"]) for e in payload["entries"]]
    assert rows.count(("command", "/simplify")) == 1
    # Tier 1's is the terminal's wording; the probe's may be a headless variant's.
    assert by_name(payload["entries"], "/simplify")["description"] != "Review the changed code"
    new = by_name(payload["entries"], "/brand-new")
    assert new["source"] == "builtin" and new["invoke"] == "/brand-new"
    assert rows.count(("agent", "Explore")) == 1


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_cache_hit_on_second_scan(fake_home):
    write(fake_home / ".claude/skills/a/SKILL.md", "---\nname: a\ndescription: first\n---\n")

    first, cached_first = catalog.scan_filesystem_cached("claude")
    second, cached_second = catalog.scan_filesystem_cached("claude")
    assert cached_first is False
    assert cached_second is True
    assert first == second


def test_cache_invalidates_when_a_file_changes(fake_home):
    path = fake_home / ".claude/skills/a/SKILL.md"
    write(path, "---\nname: a\ndescription: first\n---\n")
    entries, _ = catalog.scan_filesystem_cached("claude")
    assert entries[0]["description"] == "first"

    path.write_text("---\nname: a\ndescription: second\n---\n")
    os.utime(path, (1_000_000, 1_000_000))  # force a distinct mtime

    entries, cached = catalog.scan_filesystem_cached("claude")
    assert cached is False
    assert entries[0]["description"] == "second"


def test_cache_invalidates_when_a_file_is_added(fake_home):
    write(fake_home / ".claude/skills/a/SKILL.md", "---\nname: a\n---\n")
    catalog.scan_filesystem_cached("claude")

    write(fake_home / ".claude/skills/b/SKILL.md", "---\nname: b\n---\n")
    entries, cached = catalog.scan_filesystem_cached("claude")
    assert cached is False
    assert sorted(e["name"] for e in entries) == ["a", "b"]


def test_cache_invalidates_when_a_file_is_deleted(fake_home):
    path = fake_home / ".claude/skills/a/SKILL.md"
    write(path, "---\nname: a\n---\n")
    write(fake_home / ".claude/skills/b/SKILL.md", "---\nname: b\n---\n")
    catalog.scan_filesystem_cached("claude")

    path.unlink()
    entries, cached = catalog.scan_filesystem_cached("claude")
    assert cached is False
    assert [e["name"] for e in entries] == ["b"]


def test_cache_is_keyed_by_assistant_and_project(fake_home, tmp_path):
    project = tmp_path / "proj"
    write(fake_home / ".claude/skills/a/SKILL.md", "---\nname: a\n---\n")
    write(project / ".claude/skills/p/SKILL.md", "---\nname: p\n---\n")

    user_only, _ = catalog.scan_filesystem_cached("claude")
    with_project, cached = catalog.scan_filesystem_cached("claude", str(project))
    assert cached is False  # different key, not a hit on the user-only entry
    assert [e["name"] for e in user_only] == ["a"]
    assert sorted(e["name"] for e in with_project) == ["a", "p"]


def test_clear_cache_forces_a_rescan(fake_home):
    write(fake_home / ".claude/skills/a/SKILL.md", "---\nname: a\n---\n")
    catalog.scan_filesystem_cached("claude")
    catalog.clear_cache()
    _, cached = catalog.scan_filesystem_cached("claude")
    assert cached is False


# ---------------------------------------------------------------------------
# build_catalog — the three tiers merged
# ---------------------------------------------------------------------------


async def test_build_catalog_merges_all_three_tiers(fake_home):
    write(fake_home / ".claude/skills/fs/SKILL.md", "---\nname: fs\ndescription: tier two\n---\n")
    catalog.store_probe_entries("claude", [{"kind": "plugin", "name": "probed"}])
    try:
        payload = await catalog.build_catalog("claude")
        names = [e["name"] for e in payload["entries"]]
        assert "/clear" in names  # tier 1
        assert "fs" in names  # tier 2
        assert "probed" in names  # tier 3
        assert payload["assistant"] == "claude"
        assert payload["total"] == len(payload["entries"])
        assert payload["counts"]["skill"] == 1
        assert payload["probe_stale"] is False
    finally:
        catalog.clear_cache()


async def test_build_catalog_entries_have_the_documented_shape(fake_home):
    write(fake_home / ".claude/skills/fs/SKILL.md", "---\nname: fs\ndescription: d\n---\n")
    payload = await catalog.build_catalog("claude")
    required = {"kind", "name", "description", "source", "assistant", "path"}
    optional = {"invoke", "needs_args", "argument_hint"}
    for entry in payload["entries"]:
        assert required <= set(entry) <= required | optional, set(entry)
        assert entry["kind"] in catalog.KINDS
        assert entry["source"] in catalog.SOURCES
        assert entry["assistant"] == "claude"
        assert isinstance(entry["description"], str)


async def test_build_catalog_survives_a_scanner_failure(fake_home, monkeypatch):
    """A scan blowing up must still return the static manifest, not a 500."""
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(catalog, "scan_filesystem_cached", boom)
    payload = await catalog.build_catalog("claude")
    assert sorted(e["name"] for e in payload["entries"]) == sorted(
        e["name"] for e in catalog.builtin_entries("claude")
    )


async def test_the_payload_says_which_build_tier_1_was_measured_on(fake_home):
    assert (await catalog.build_catalog("grok"))["builtin_version"] == catalog.BUILTIN_MEASURED["grok"]
    assert (await catalog.build_catalog("agy"))["builtin_version"] == catalog.BUILTIN_MEASURED["antigravity"]


async def test_build_catalog_normalizes_the_assistant(fake_home):
    payload = await catalog.build_catalog("agy")
    assert payload["assistant"] == "antigravity"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


async def test_catalog_endpoint_returns_entries(client):
    await register_test_session(client, session_id="cat-1", project="myproject")
    resp = await client.get("/api/catalog/myproject", headers=auth_headers())
    assert resp.status_code == 200

    body = resp.json()
    assert body["project"] == "myproject"
    assert body["assistant"] == "claude"
    assert body["project_matched"] is True
    assert isinstance(body["entries"], list)
    assert body["total"] == len(body["entries"])
    assert "/clear" in [e["name"] for e in body["entries"]]


async def test_catalog_endpoint_honors_the_assistant_query_param(client):
    await register_test_session(client, session_id="cat-2", project="myproject")
    resp = await client.get(
        "/api/catalog/myproject", params={"assistant": "codex"}, headers=auth_headers()
    )
    body = resp.json()
    assert body["assistant"] == "codex"
    assert all(e["assistant"] == "codex" for e in body["entries"])


async def test_catalog_endpoint_uses_the_sessions_assistant_by_default(client):
    await register_test_session(
        client, session_id="cat-3", project="myproject", assistant="codex"
    )
    resp = await client.get("/api/catalog/myproject", headers=auth_headers())
    assert resp.json()["assistant"] == "codex"


async def test_catalog_endpoint_renders_for_an_unknown_project(client):
    """No session and no configured project: still render the user catalog."""
    resp = await client.get("/api/catalog/ghostproject", headers=auth_headers())
    assert resp.status_code == 200

    body = resp.json()
    assert body["project_matched"] is False
    assert body["project_dir"] is None
    assert body["entries"], "the static manifest must still render"


async def test_catalog_endpoint_rejects_an_invalid_project_name(client):
    resp = await client.get("/api/catalog/bad%21name%3B", headers=auth_headers())
    assert resp.status_code == 200
    assert "error" in resp.json()
    assert "entries" not in resp.json()


async def test_catalog_endpoint_requires_auth(client):
    resp = await client.get("/api/catalog/myproject")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Invocation — what a palette sends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry, expected", [
    ({"kind": "command", "name": "/git:sync", "assistant": "claude"}, ("/git:sync", False)),
    # Gemini's TOML commands are named without the slash.
    ({"kind": "command", "name": "deploy", "assistant": "gemini"}, ("/deploy", False)),
    ({"kind": "skill", "name": "br", "assistant": "claude"}, ("/br", False)),
    ({"kind": "skill", "name": "codex:rescue", "assistant": "claude"}, ("/codex:rescue", False)),
    # Grok's and Antigravity's skills are slash commands as well.
    ({"kind": "skill", "name": "code-review", "assistant": "grok"}, ("/code-review", False)),
    ({"kind": "skill", "name": "code-review", "assistant": "composer"}, ("/code-review", False)),
    ({"kind": "skill", "name": "automation", "assistant": "antigravity"}, ("/automation", False)),
    ({"kind": "skill", "name": "triage", "assistant": "gemini"}, ("Use the triage skill to", True)),
    # A built-in the CLI rejects bare says so.
    ({"kind": "command", "name": "/btw", "assistant": "grok", "needs_args": True}, ("/btw", True)),
    # Asked for in words, which every assistant understands -- and which
    # means nothing without the task, so arguments are required.
    ({"kind": "agent", "name": "code-reviewer", "assistant": "claude"},
     ("Use the code-reviewer agent to", True)),
    ({"kind": "skill", "name": "triage", "assistant": "codex"}, ("Use the triage skill to", True)),
    ({"kind": "mcp", "name": "supabase", "assistant": "claude"},
     ("Use the supabase MCP server to", True)),
    # A plugin is its parts, each catalogued on its own.
    ({"kind": "plugin", "name": "code-review", "assistant": "claude"}, (None, False)),
    ({"kind": "command", "name": "  ", "assistant": "claude"}, (None, False)),
])
def test_invocation_per_kind(entry, expected):
    assert catalog.invocation(entry) == expected


async def test_build_catalog_says_how_to_invoke_each_entry(fake_home):
    write(fake_home / ".claude/agents/reviewer.md", "---\nname: reviewer\ndescription: R\n---\n")
    write(fake_home / ".claude/commands/ship.md",
          "---\ndescription: Ship it\nargument-hint: [version]\n---\n")
    payload = await catalog.build_catalog("claude")
    agent = by_name(payload["entries"], "reviewer")
    assert agent["invoke"] == "Use the reviewer agent to" and agent["needs_args"] is True
    ship = by_name(payload["entries"], "/ship")
    assert ship["invoke"] == "/ship" and ship["needs_args"] is False
    assert ship["argument_hint"] == "[version]"
    # Builtins get it too.
    assert by_name(payload["entries"], "/clear")["invoke"] == "/clear"

