"""Command catalog — everything a focused session can be told to do.

Backs the headset's command palette. There is no
universal "list my slash commands" call on any installed CLI, so the catalog is
a hybrid of three tiers with very different costs:

* **Tier 1 — static manifest.** Built-in slash commands live inside the CLI
  binary, not on disk, and no CLI lists its interactive ones. Each assistant's
  table here (``_BUILTIN_COMMANDS``) is every command its terminal offers,
  measured from one installed build (``BUILTIN_MEASURED``). Honest
  limitation; **must be refreshed on CLI version bumps**.
* **Tier 2 — filesystem scan (primary).** User / project / plugin commands,
  agents and skills are all files with YAML (or TOML) frontmatter carrying
  exactly the ``name`` + ``description`` a palette needs. No subprocess, no
  network — a few hundred ``stat`` calls and capped head-reads.
* **Tier 3 — CLI probe.** Up to seconds per call, so it never runs inside a
  request: a request that finds it stale starts a background refresh and
  answers from cache (``refresh_probe_if_stale``). Only machine-readable,
  read-only listings run -- Codex's MCP list, the one inventory no config file
  holds, and Claude Code's initialize answer, which keeps its bundled skills
  and agents current between Tier 1 refreshes.

Design rules, all load-bearing:

* **Every path is optional.** Measured on the dev machine: ``~/.claude/commands``
  and ``~/.codex/prompts`` do not exist, ``~/.grok/skills`` is empty. A missing
  directory is the normal case, never an error.
* **Never raises.** A garbled frontmatter file yields no entry, not a 500. The
  palette must always render.
* **Security posture is ported from ``bridge/transcript_render.py``**: realpath,
  allowed-root validation, ``O_NOFOLLOW``, hard size cap, capped head-read. A
  symlink pointing out of an assistant config root is refused at read time even
  though ``glob`` happily walked into it.
* **Cache on an mtime fingerprint.** A stale entry beats a spinner in a headset.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

_IS_WINDOWS = sys.platform == "win32"

# --- limits -----------------------------------------------------------------
# Frontmatter lives in the first couple of KB; never slurp a whole file.
_MAX_HEAD_BYTES = 4096
# Hard reject before reading: a 2 MB "SKILL.md" is not a skill header.
_MAX_FILE_BYTES = 1 * 1024 * 1024
# MCP configs are small JSON; read them whole but keep the ceiling low.
_MAX_JSON_BYTES = 256 * 1024
# ~/.claude.json is Claude Code's whole state file -- every project it has
# opened -- and it holds the user- and local-scope MCP servers. 187 KB on the
# dev machine; heavy users run to megabytes. It is parsed whole or not at all.
_MAX_CLAUDE_JSON_BYTES = 16 * 1024 * 1024
# Bound the blast radius of a pathological tree (a marketplace checkout with
# thousands of markdown files) so one glob can't stall the palette.
_MAX_FILES_PER_PATTERN = 500
_MAX_ENTRIES = 1500
# Palette rows are short; a 2 KB skill description is dead weight on the wire.
_MAX_DESCRIPTION_CHARS = 300
_MAX_NAME_CHARS = 120
_MAX_HINT_CHARS = 80

_WS_RE = re.compile(r"\s+")

# Entry kinds and sources, as documented in docs/api.md.
KINDS = ("command", "agent", "skill", "mcp", "plugin")
SOURCES = ("builtin", "user", "project", "plugin")

# Assistants the catalog knows how to scan. Deliberately a superset of
# ``bridge.assistant.SUPPORTED_ASSISTANTS``: "antigravity" has on-disk content
# (``agy`` writes under ``~/.gemini/antigravity-cli/``) but is not yet a
# dispatchable assistant, and the headset client wants to browse it either way.
CATALOG_ASSISTANTS = ("claude", "codex", "gemini", "grok", "composer", "antigravity")

_ASSISTANT_ALIASES = {
    "claude-code": "claude",
    "claudecode": "claude",
    "grok-build": "grok",
    "agy": "antigravity",
    "gemini-cli": "gemini",
}


def normalize_catalog_assistant(value: str | None, *, default: str = "claude") -> str:
    """Normalize an assistant id for catalog lookups, falling back to *default*."""
    raw = (value or "").strip().lower()
    raw = _ASSISTANT_ALIASES.get(raw, raw)
    if raw in CATALOG_ASSISTANTS:
        return raw
    fallback = (default or "claude").strip().lower()
    fallback = _ASSISTANT_ALIASES.get(fallback, fallback)
    return fallback if fallback in CATALOG_ASSISTANTS else "claude"


# ---------------------------------------------------------------------------
# Tier 1 — static manifest
# ---------------------------------------------------------------------------
#
# !!! REFRESH ON CLI VERSION BUMPS !!!
#
# Built-in slash commands are compiled into each CLI, and none of them lists
# its interactive ones from the outside. (Claude comes closest: Tier 3 asks it
# for its bundled skills and agents, which do change between releases.) So
# every table here is measured from one installed build -- the version is in
# ``BUILTIN_MEASURED`` and in each catalog response -- and left alone until
# someone measures again. Measured 2026-09-27:
#
#   claude 2.1.283   the command objects in the binary: the interactive
#                    variant of each (``isEnabled: () => Ae()`` is headless
#                    only), and the bundled skills its initialize answer lists.
#   codex 0.154.0    codex-rs/tui/src/slash_command.rs at tag rust-v0.154.0,
#                    as built for macOS; hints from chatwidget/slash_dispatch.rs.
#   gemini 0.61.0    BuiltinCommandLoader in the npm bundle, with the default
#                    settings (folder trust on, checkpointing and voice off).
#   grok 1.0.30      the TUI's command builders and the shell's static table
#                    in the binary; composer is the same binary.
#   agy 1.2.12       the Go command registry in the binary (its docs page is
#                    older: it still lists /fast and /planning, both removed).
#                    1.2.12 added /plugin to 1.2.10's set; agy updates itself.
#
# Left out on purpose: hidden, debug, easter-egg, other-platform and
# default-off commands, and every alias. A command that is missing costs the
# user a keystroke; one that is not there puts "Unknown command" in their
# terminal.
#
# Each row is (name, argument hint, description), in the CLI's own words where
# they fit a palette row. A hint is shown while the palette listens for words.

BUILTIN_MEASURED: dict[str, str] = {
    "claude": "2.1.283",
    "codex": "0.154.0",
    "gemini": "0.61.0",
    "grok": "1.0.30",
    "composer": "1.0.30",
    "antigravity": "1.2.12",
}

# A model picker that also rewrites the user's default is a hazard in a
# palette: VoxHerd runs many sessions, and every one started later inherits
# the choice. Measured: Claude Code saves /model to ~/.claude/settings.json
# (a throwaway probe's /model once replaced the user's default); Codex
# persists "default model and reasoning effort" (PersistModelSelection) and
# its personality the same way; agy documents its /model as persisting across
# sessions. Gemini's /model is session-only unless asked to --persist, and
# Grok restores a model per session, so theirs are not marked.
_SAVES_DEFAULT = " — also becomes the default for new sessions"

_CLAUDE_BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("/add-dir", "<path>", "Add a new working directory"),
    ("/artifacts", "", "Browse your published and shared artifacts"),
    ("/autocompact", "[auto|<tokens>]", "Set how full the context gets before auto-summarizing"),
    ("/autofix-pr", "", "Monitor and autofix any issues with the current PR"),
    ("/background", "[prompt]", "Send this session to the background and free the terminal"),
    ("/batch", "<instruction>", "Plan a large change; background agents each open a PR"),
    ("/branch", "[name]", "Create a branch of the current conversation at this point"),
    ("/btw", "[question]", "Ask a quick side question without interrupting the main conversation"),
    ("/bug", "[report]", "Report a bug or share your conversation"),
    ("/cd", "<path>", "Move this session to a new working directory"),
    ("/claude-api", "", "Build and debug apps that use the Claude API"),
    ("/clear", "[name]", "Start a new session with empty context; previous session stays on disk (resumable with /resume)"),
    ("/code-review", "[low|medium|high|xhigh|max] [--fix] [--comment] [<pr#>|<branch>|<path>]",
     "Review the current diff, or a PR, branch or path, for correctness bugs"),
    ("/color", "[red|blue|green|yellow|purple|orange|pink|cyan|default]",
     "Set the prompt bar color for this session"),
    ("/compact", "<optional custom summarization instructions>",
     "Free up context by summarizing the conversation so far"),
    ("/config", "[key=value]", "Open settings"),
    ("/context", "[all]", "Visualize current context usage as a colored grid"),
    ("/copy", "", "Copy Claude's last response to clipboard (or /copy N for the Nth-latest)"),
    ("/daemon", "", "Manage background services and routines"),
    ("/dataviz", "", "Design charts, graphs, dashboards and other data visualizations"),
    ("/debug", "[issue description]", "Turn on debug logging and investigate problems"),
    ("/deep-research", "", "Fan out web searches, verify the claims and write a cited report"),
    ("/diff", "", "View uncommitted changes and per-turn diffs"),
    ("/doctor", "[prompt-audit [<path>]]", "Health-check your setup and fix issues: installation, unused extensions, duplicated or bloated memory files, slow hooks, updates, permissions"),
    ("/effort", "", "Set effort level for model usage"),
    ("/exit", "", "Exit the CLI"),
    ("/export", "[filename]", "Export the current conversation to a file or clipboard"),
    ("/fast", "[on|off]", "Toggle fast mode"),
    ("/feedback", "[report]", "Send feedback to Anthropic or report a bug"),
    ("/fewer-permission-prompts", "", "Pre-approve safe read-only commands based on your usage"),
    ("/fork", "<directive>", "Spawn a background agent that inherits the full conversation"),
    ("/goal", "[<condition> | clear]", "Set a goal Claude checks before stopping"),
    ("/help", "", "Show help and available commands"),
    ("/hooks", "", "View hook configurations for tool events"),
    ("/ide", "[open]", "Manage IDE integrations and show status"),
    ("/init", "", "Initialize a new CLAUDE.md file with codebase documentation"),
    ("/insights", "", "Generate a report analyzing your Claude Code sessions"),
    ("/install-github-app", "", "Set up Claude GitHub Actions for a repository"),
    ("/install-slack-app", "", "Install the Claude Slack app"),
    ("/keybindings", "", "Open your keyboard shortcuts file"),
    ("/list-agents", "", "List subagents, teammates, and other Claude sessions you can message"),
    ("/login", "", "Sign in with your Anthropic account, or switch accounts"),
    ("/logout", "", "Sign out from your Anthropic account"),
    ("/loop", "[interval] [prompt]", "Run a prompt or slash command on a recurring interval"),
    ("/mcp", "[reconnect <server>|enable|disable [<server>|all]]", "Manage MCP servers"),
    ("/memory", "", "Edit CLAUDE.md files and memory settings"),
    ("/mobile", "", "Show QR code to download the Claude mobile app"),
    ("/model", "[model]", "Set the AI model for Claude Code" + _SAVES_DEFAULT),
    ("/output-style", "[style]", "List output styles or switch to one"),
    ("/permissions", "", "Manage allow and deny tool permission rules"),
    ("/plan", "[open|<description>]", "Enable plan mode or view the current session plan"),
    ("/plugin", "", "Manage Claude Code plugins"),
    ("/plugin-types", "[dir]", "Write TypeScript declarations for the plugin API and this session's MCP tools"),
    ("/powerup", "", "Discover Claude Code features through quick interactive lessons"),
    ("/privacy-settings", "", "View and update your privacy settings"),
    ("/radio", "", "Listen to Claude FM lo-fi radio"),
    ("/recap", "", "Generate a one-line session recap now"),
    ("/release-notes", "", "View release notes"),
    ("/reload-plugins", "[--force]", "Activate pending plugin changes in the current session"),
    ("/reload-skills", "", "Pick up skills added or changed on disk during this session"),
    ("/remote-control", "[name]", "Control this session from your phone or claude.ai/code"),
    ("/remote-env", "", "Choose the default environment for cloud agents"),
    ("/rename", "[name]", "Rename the current conversation"),
    ("/resume", "[conversation id or search term]", "Resume a previous conversation"),
    ("/rewind", "", "Restore the code and/or conversation to a previous point"),
    ("/run", "", "Launch this project's app to see your change working"),
    ("/run-skill-generator", "", "Create a skill that knows how to run this project's app"),
    ("/schedule", "", "Create and manage scheduled remote Claude Code agents"),
    ("/security-review", "", "Complete a security review of the pending changes on the current branch"),
    ("/simplify", "[<target>]", "Review the changed code for reuse, simplification and efficiency, then fix it"),
    ("/skill-doctor", "", "Show which loaded skills are unused and costing context"),
    ("/skills", "", "List available skills"),
    ("/status", "", "Show Claude Code status including version, model, account, API connectivity, and tool statuses"),
    ("/statusline", "", "Set up Claude Code's status line UI"),
    ("/stickers", "", "Order Claude Code stickers"),
    ("/subtask", "<task>", "Send a subagent off with your full context; its result comes back here"),
    ("/tasks", "", "View and manage everything running in the background"),
    ("/team-onboarding", "", "Help teammates ramp on Claude Code with a guide from your usage"),
    ("/teleport", "", "Send this session to the cloud, or resume one from claude.ai"),
    ("/terminal-setup", "", "Install Shift+Enter key binding for newlines"),
    ("/theme", "", "Change the theme"),
    ("/tui", "[default|fullscreen]", "Set the terminal UI renderer (default | fullscreen)"),
    ("/ultrareview", "", "Find and verify bugs in your branch using a cloud session"),
    ("/update-config", "", "Change settings: hooks, permissions, environment variables"),
    ("/upgrade", "", "Upgrade to Max for higher rate limits and more Opus"),
    ("/usage", "", "Show session cost, plan usage, and activity stats"),
    ("/usage-credits", "", "Configure usage credits or request them from your admin when you hit a limit"),
    ("/verify", "", "Verify a code change works by exercising it end to end"),
    ("/voice", "[hold|tap|off]", "Toggle voice mode"),
    ("/web-setup", "", "Set up cloud sessions with your GitHub account"),
    ("/workflow-authoring", "", "Reference for writing a Workflow tool script"),
    ("/workflows", "", "Browse running and completed workflows"),
)

_CODEX_BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("/agents", "", "View and switch between all active agent sessions"),
    ("/app", "", "Continue this session in the Desktop app"),
    ("/approve", "", "Approve one retry of a recent auto-review denial"),
    ("/apps", "", "Manage apps"),
    ("/archive", "", "Archive this session and exit"),
    ("/btw", "[question]", "Start a side conversation in an ephemeral fork"),
    ("/cd", "[path]", "Change the current working directory"),
    ("/clear", "[prompt]", "Clear the terminal and start a new chat"),
    ("/compact", "", "Summarize conversation to prevent hitting the context limit"),
    ("/copy", "", "Copy the last response or part of it"),
    ("/debug-config", "", "Show config layers and requirement sources for debugging"),
    ("/delete", "", "Permanently delete this session and exit"),
    ("/diff", "", "Show git diff (including untracked files)"),
    ("/exit", "", "Exit Codex"),
    ("/experimental", "", "Toggle experimental features"),
    ("/export", "[path]", "Export the conversation as markdown"),
    ("/feedback", "", "Send logs to maintainers"),
    ("/fork", "[prompt]", "Fork the current chat"),
    ("/goal", "[objective]", "Set or view the goal for a long-running task"),
    ("/hooks", "", "View and manage lifecycle hooks"),
    ("/ide", "", "Include current selection, open files, and other context from your IDE"),
    ("/import", "", "Import setup, this project, and recent chats from Claude Code"),
    ("/init", "", "Create an AGENTS.md file with instructions for Codex"),
    ("/keymap", "[debug]", "Remap TUI shortcuts"),
    ("/logout", "", "Log out of Codex"),
    ("/mcp", "[verbose]", "List configured MCP tools; use /mcp verbose for details"),
    ("/memories", "", "Configure memory use and generation"),
    ("/mention", "", "Mention a file"),
    ("/model", "", "Choose what model and reasoning effort to use" + _SAVES_DEFAULT),
    ("/new", "[prompt]", "Start a new chat during a conversation"),
    ("/permissions", "", "Choose what Codex is allowed to do"),
    ("/personality", "", "Choose a communication style for Codex" + _SAVES_DEFAULT),
    ("/pets", "[pet]", "Choose or hide the terminal pet"),
    ("/plan", "[prompt]", "Switch to Plan mode"),
    ("/plugins", "", "Browse plugins"),
    ("/ps", "", "List background terminals"),
    ("/pwd", "", "Show the current working directory"),
    ("/quit", "", "Exit Codex"),
    ("/raw", "[on|off]", "Toggle raw scrollback mode for copy-friendly terminal selection"),
    ("/recap", "", "Summarize the current conversation now"),
    ("/rename", "[title]", "Rename the current thread"),
    ("/resume", "[session]", "Resume a saved chat"),
    ("/review", "[instructions]", "Review my current changes and find issues"),
    ("/side", "[question]", "Start a side conversation in an ephemeral fork"),
    ("/skills", "", "Use skills to improve how Codex performs specific tasks"),
    ("/status", "", "Show current session configuration and token usage"),
    ("/statusline", "", "Configure which items appear in the status line"),
    ("/stop", "", "Stop all background terminals"),
    ("/subagents", "", "Switch between this session's subagents"),
    ("/theme", "", "Choose a syntax highlighting theme"),
    ("/title", "", "Configure which items appear in the terminal title"),
    ("/usage", "[daily|weekly|cumulative]", "View account usage or use a usage limit reset"),
    ("/vim", "", "Toggle Vim mode for the composer"),
    ("/worktree", "", "Start or continue a conversation in a new worktree"),
)

_GEMINI_BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("/about", "", "Show version info"),
    ("/agents", "", "Manage agents"),
    ("/auth", "", "Manage authentication"),
    ("/bug", "", "Submit a bug report"),
    ("/bug-memory", "", "Capture a V8 heap snapshot to disk to attach to a bug report"),
    ("/chat", "", "Browse auto-saved conversations and manage chat checkpoints"),
    ("/clear", "", "Clear the screen and start a new session"),
    ("/commands", "[list|reload]", "Manage custom slash commands"),
    ("/compress", "", "Compresses the context by replacing it with a summary"),
    ("/copy", "", "Copy the last result or code snippet to clipboard"),
    ("/directory", "", "Manage workspace directories"),
    ("/docs", "", "Open full Gemini CLI documentation in your browser"),
    ("/editor", "", "Set external editor preference"),
    ("/export-session", "", "Export the current session to a JSON file"),
    ("/extensions", "", "Manage extensions"),
    ("/footer", "", "Configure which items appear in the footer (statusline)"),
    ("/gemma", "", "Check local Gemma model routing status"),
    ("/help", "", "For help on gemini-cli"),
    ("/hooks", "", "Manage hooks"),
    ("/ide", "", "Manage IDE integration"),
    ("/init", "", "Analyzes the project and creates a tailored GEMINI.md file"),
    ("/mcp", "", "Manage configured Model Context Protocol (MCP) servers"),
    ("/memory", "", "Commands for interacting with memory"),
    ("/model", "", "Manage model configuration"),
    ("/permissions", "", "Manage folder trust settings and other permissions"),
    ("/plan", "", "Switch to Plan Mode and view current plan"),
    ("/policies", "", "Manage policies"),
    ("/privacy", "", "Display the privacy notice"),
    ("/quit", "", "Exit the cli"),
    ("/resume", "", "Browse auto-saved conversations and manage chat checkpoints"),
    ("/rewind", "", "Jump back to a specific message and restart the conversation"),
    ("/settings", "", "View and edit Gemini CLI settings"),
    ("/setup-github", "", "Set up GitHub Actions"),
    ("/shortcuts", "", "Toggle the shortcuts panel above the input"),
    ("/skills", "[list | disable <name> | enable <name> | reload]",
     "List, enable, disable, or reload Gemini CLI agent skills"),
    ("/stats", "[session|model|tools]", "Check session stats"),
    ("/tasks", "", "Toggle background tasks view"),
    ("/terminal-setup", "", "Configure terminal keybindings for multiline input (VS Code, Cursor, Windsurf)"),
    ("/theme", "", "Change the theme"),
    ("/tools", "[desc]", "List available Gemini CLI tools"),
    ("/vim", "", "Toggle vim mode on/off"),
)

# The TUI's own commands and the ones its shell adds (/goal, /flush, the
# hooks-* family, /deep-research, ...): the menu shows both.
_GROK_BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("/always-approve", "on|off", "Toggle always-approve mode (skip all permission prompts)"),
    ("/announcements", "hide | show", "Show or hide announcements"),
    ("/auto", "", "Toggle auto mode (classifier approves safe tools)"),
    ("/btw", "<question>", "Ask a side question without interrupting"),
    ("/compact", "compaction instructions", "Compact conversation history"),
    ("/compact-mode", "", "Toggle compact UI (less padding, more content)"),
    ("/config-agents", "", "Manage agent definitions"),
    ("/context", "", "View context usage"),
    ("/copy", "[N] [file]", "Copy last response to clipboard or file"),
    ("/dashboard", "", "Open the Agent Dashboard"),
    ("/deep-research", "<query>",
     "Research with bounded parallel agents, cross-check evidence, and write a cited report"),
    ("/delete", "", "Delete this session"),
    ("/docs", "[web|title]", "Open How-to Guides or online Build docs"),
    ("/doctor", "[fix [FIX]]", "Check this session and show available fixes"),
    ("/dream", "", "Run memory consolidation (merge session logs into organized topics)"),
    ("/edit-prompt", "", "Open an external editor for an empty prompt"),
    ("/effort", "<level>", "Set reasoning effort for the current model"),
    ("/expand", "", "Re-print the last collapsed block, fully expanded (minimal mode)"),
    ("/export", "[filename]", "Export the current conversation to a file or clipboard"),
    ("/feedback", "[text]", "Send feedback about the current session"),
    ("/find", "[text]", "Search the conversation scrollback"),
    ("/flush", "", "Flush conversation memory to disk now"),
    ("/fork", "[--worktree|--no-worktree] [directive]", "Branch the current session into a peer agent"),
    ("/fullscreen", "", "Switch this session to fullscreen mode, back with /minimal"),
    ("/goal", "<objective> [--budget <tokens>] | status | pause | resume | clear",
     "Set, manage, or check an autonomous goal"),
    ("/help", "", "Browse commands and keyboard shortcuts"),
    ("/history", "", "Search prompt history"),
    ("/home", "", "Return to the welcome screen"),
    ("/hooks", "", "View hooks"),
    ("/hooks-add", "path to hook file or directory", "Add a custom hook file or directory"),
    ("/hooks-list", "", "Show hooks loaded in this session"),
    ("/hooks-remove", "path to hook file or directory", "Remove a custom hook file or directory path"),
    ("/hooks-trust", "", "Trust this project for hook execution"),
    ("/hooks-untrust", "", "Remove trust for the current project"),
    ("/imagine", "<description>", "Generate an image from a text description"),
    ("/imagine-video", "<description>", "Generate a video from a text description"),
    ("/import-claude", "", "Open the Claude settings import modal"),
    ("/jump", "", "Jump to a turn in the conversation"),
    ("/login", "", "Log in or re-authenticate with your account"),
    ("/logout", "", "Log out and return to the login screen"),
    ("/loop", "[interval] <prompt>", "Run a prompt on a recurring interval"),
    ("/marketplace", "", "View marketplace"),
    ("/mcps", "", "Show MCP server status"),
    ("/memory", "on|off", "Browse, view, and manage your memories"),
    ("/minimal", "", "Switch this session to minimal (scrollback-native) mode, back with /fullscreen"),
    ("/model", "<name> [effort]", "Switch the active model"),
    ("/multiline", "", "Toggle multiline input mode (swap Enter and Shift+Enter)"),
    ("/new", "", "Start a new session"),
    ("/personas", "", "Manage personas (create, edit, delete)"),
    ("/plan", "[description]", "Enter plan mode"),
    ("/plugins", "", "View plugins"),
    ("/privacy", "", "Open coding data, retention, and training settings"),
    ("/queue", "", "List the prompts queued behind the running turn"),
    ("/quit", "", "Quit the application"),
    ("/recap", "", "Summarize the session so far"),
    ("/release-notes", "", "View release notes for the current version"),
    ("/reload-plugins", "", "Reload plugins from disk (alias for /plugins reload)"),
    ("/remember", "[text]", "Save a memory note"),
    ("/rename", "<title> | --auto", "Rename the current session"),
    ("/resume", "", "Resume a previous session"),
    ("/rewind", "", "Rewind to a previous turn"),
    ("/session-info", "", "Show session info"),
    ("/settings", "", "Open the settings modal"),
    ("/share", "", "Share this session via URL"),
    ("/skills", "", "View skills"),
    ("/tasks", "", "List background tasks, subagents, and scheduled tasks"),
    ("/theme", "<name>", "Switch the color theme"),
    ("/timeline", "", "Toggle the timeline sidebar"),
    ("/timestamps", "", "Toggle message timestamps on/off"),
    ("/toggle-mouse-reporting", "", "Toggle terminal mouse reporting (native click-drag copy/paste)"),
    ("/transcript", "", "View the conversation transcript in your pager ($PAGER)"),
    ("/tutorial", "", "Quick tips to get the most out of Grok Build"),
    ("/usage", "[show|manage]", "View usage"),
    ("/view-plan", "", "View the current plan"),
    ("/vim-mode", "", "Toggle vim-style scrollback keybindings (j/k, h/l, g/G, y/Y, …)"),
    ("/voice", "", "Toggle dictation (Ctrl+Space/F8; Esc/Enter to stop)"),
    ("/workflow", "<name> [args] | runs | pause|resume|stop|save [name]",
     "Launch a saved workflow, list runs, or manage a run (pause, resume, stop, save)"),
    ("/workflows", "", "Browse installed workflows"),
)

_ANTIGRAVITY_BUILTINS: tuple[tuple[str, str, str], ...] = (
    ("/add-dir", "<path>", "Add a directory to the workspace"),
    ("/agents", "", "List available custom agents"),
    ("/artifact", "", "View and review artifacts"),
    ("/btw", "<question>", "Ask a side question without interrupting the current task"),
    ("/changelog", "", "Show release notes and changes"),
    ("/clear", "", "Clear conversation and start a new one"),
    ("/codesearch", "<query>", "Search code in the workspace"),
    ("/config", "", "Open settings panel"),
    ("/context", "", "Visualize current context usage"),
    ("/copy", "[n|btw]", "Copy the last planner response, the n-th one, or the /btw answer to the clipboard"),
    ("/credits", "", "Show remaining G1 credits and purchase link"),
    ("/diff", "", "View uncommitted changes and per-turn diffs"),
    ("/effort", "", "Set the reasoning effort"),
    ("/exit", "", "Exit the CLI"),
    ("/feedback", "", "Submit qualitative feedback to improve the agent"),
    ("/fork", "", "Create a branch of the current conversation at this point, optionally specifying a project"),
    ("/help", "", "Show available commands and keybindings"),
    ("/hooks", "", "Manage hook configurations for tool events"),
    ("/keybindings", "", "Set custom keybindings"),
    ("/logout", "", "Log out"),
    ("/mcp", "", "Manage MCP servers"),
    ("/model", "[model]", "Set a model, or run a single prompt on another model" + _SAVES_DEFAULT),
    ("/open", "", "Open a file or view opened/edited files"),
    ("/permissions", "", "Manage tool permissions"),
    ("/plugin", "", "Manage, discover, install, uninstall, enable, and disable plugins"),
    # Not in the command registry: a mode prefix ("/plan /grill-me <prompt>"),
    # per the changelog agy carries.
    ("/plan", "[prompt]", "Run the prompt in plan mode"),
    ("/remote-control", "[off]", "Turn on remote control for this session (off to disable)"),
    ("/rename", "<name>", "Rename the current conversation"),
    ("/resume", "", "Browse and resume past conversations"),
    ("/rewind", "", "Rewind conversation to a previous message"),
    ("/skills", "", "List available skills"),
    ("/statusline", "", "Toggle the statusline"),
    ("/tasks", "", "View background tasks"),
    ("/title", "[on|off]", "Toggle custom terminal window title"),
    ("/usage", "", "View model quota usage"),
    ("/voice", "", "Dictate a prompt using your microphone"),
)

_BUILTIN_COMMANDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "claude": _CLAUDE_BUILTINS,
    "codex": _CODEX_BUILTINS,
    "gemini": _GEMINI_BUILTINS,
    "grok": _GROK_BUILTINS,
    "composer": _GROK_BUILTINS,
    "antigravity": _ANTIGRAVITY_BUILTINS,
}

# Commands that do nothing without words -- the CLI answers a bare one with
# its usage. A palette listens for the words instead of sending it bare. Only
# where that was seen: Grok's and agy's usage errors, and hints that make the
# words the whole point (/imagine, /deep-research). Everything else may be
# sent bare, which at worst prints the command's usage.
_BUILTIN_NEEDS_ARGS: dict[str, frozenset[str]] = {
    "grok": frozenset({
        "/btw", "/deep-research", "/hooks-add", "/hooks-remove", "/imagine", "/imagine-video", "/loop",
    }),
    "antigravity": frozenset({"/add-dir", "/btw", "/codesearch", "/rename"}),
}
_BUILTIN_NEEDS_ARGS["composer"] = _BUILTIN_NEEDS_ARGS["grok"]

# Claude Code's own subagents, from its initialize answer. Tier 3 keeps them
# current; these are here so the palette has them before the first probe.
_BUILTIN_AGENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "claude": (
        ("claude", "Catch-all for any task that doesn't fit a more specific agent"),
        ("Explore", "Fast read-only search agent for locating code"),
        ("general-purpose", "Researches complex questions, searches for code and runs multi-step tasks"),
        ("Plan", "Software architect agent for designing implementation plans"),
        ("statusline-setup", "Configures the Claude Code status line setting"),
    ),
}


def builtin_entries(assistant: str) -> list[dict]:
    """Tier 1: the static per-assistant manifest. Pure; never raises."""
    needs_args = _BUILTIN_NEEDS_ARGS.get(assistant, frozenset())
    entries: list[dict] = []
    for name, hint, description in _BUILTIN_COMMANDS.get(assistant, ()):
        entry = {
            "kind": "command",
            "name": name,
            "description": description,
            "source": "builtin",
            "assistant": assistant,
            "path": None,
        }
        if hint:
            entry["argument_hint"] = hint
        if name in needs_args:
            entry["needs_args"] = True
        entries.append(entry)
    for name, description in _BUILTIN_AGENTS.get(assistant, ()):
        entries.append({
            "kind": "agent",
            "name": name,
            "description": description,
            "source": "builtin",
            "assistant": assistant,
            "path": None,
        })
    return entries


# ---------------------------------------------------------------------------
# Frontmatter / TOML head parsing
# ---------------------------------------------------------------------------

_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract top-level scalars from a YAML frontmatter block.

    A deliberately minimal YAML subset — enough for ``name:``/``description:``
    including folded (``>``, ``>-``) and literal (``|``, ``|-``) block scalars,
    which real skill files use. Nested mappings (``metadata:\\n  icon: x``) are
    skipped rather than flattened. Returns ``{}`` when there is no frontmatter
    or the block is garbled. Never raises.
    """
    try:
        if not text:
            return {}
        stripped = text.lstrip("﻿")
        lines = stripped.split("\n")
        # The opening fence must be the first non-empty line.
        start = 0
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start >= len(lines) or lines[start].strip() != "---":
            return {}

        out: dict[str, str] = {}
        i = start + 1
        while i < len(lines):
            raw = lines[i]
            bare = raw.strip()
            if bare in ("---", "..."):
                break
            if not bare or bare.startswith("#"):
                i += 1
                continue
            # Only top-level keys; indented lines belong to a block we handle
            # below or to a nested mapping we intentionally skip.
            if raw[:1].isspace():
                i += 1
                continue
            if ":" not in raw:
                i += 1
                continue

            key, _, rest = raw.partition(":")
            key = key.strip()
            value = rest.strip()
            if not key:
                i += 1
                continue

            if _BLOCK_SCALAR_RE.match(value):
                folded = value[0] == ">"
                block: list[str] = []
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if nxt.strip() and not nxt[:1].isspace():
                        break
                    if nxt.strip() in ("---", "..."):
                        break
                    block.append(nxt.strip())
                    i += 1
                joined = " ".join(p for p in block if p) if folded else "\n".join(block)
                out[key] = joined.strip()
                continue

            if not value:
                # Either an empty scalar or the head of a nested mapping; skip
                # any indented continuation so it can't be read as top-level.
                i += 1
                while i < len(lines) and lines[i][:1].isspace() and lines[i].strip():
                    i += 1
                continue

            out[key] = _unquote(value)
            i += 1
        return out
    except Exception:
        return {}


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes from a scalar."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_toml_head(text: str) -> dict[str, str]:
    """Extract leading top-level ``key = "value"`` pairs from a TOML file.

    Used for ``~/.codex/agents/*.toml``, where ``name`` and ``description`` are
    the first two lines. Line-based on purpose: the head-read truncates the file
    mid-``\"\"\"`` block, which would make a real TOML parse fail outright.
    Stops at the first table header or multi-line string. Never raises.
    """
    try:
        out: dict[str, str] = {}
        for raw in (text or "").split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                break
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not key or value.startswith(('"""', "'''")):
                break
            if value.startswith(('"', "'")):
                out[key] = _unquote(value)
        return out
    except Exception:
        return {}


def _clean(text: str, limit: int) -> str:
    """Collapse whitespace and truncate for a palette row."""
    try:
        collapsed = _WS_RE.sub(" ", str(text or "")).strip()
        if len(collapsed) > limit:
            return collapsed[: limit - 1].rstrip() + "…"
        return collapsed
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Safe reads — realpath → allowed-root → O_NOFOLLOW → size cap → head-read
# ---------------------------------------------------------------------------


def _realpath_or_empty(path: str) -> str:
    """``os.path.realpath`` that yields ``""`` instead of raising."""
    try:
        return os.path.realpath(os.path.expanduser(path))
    except Exception:
        return ""


def _allowed_roots(project_dir: str | None) -> list[str]:
    """Realpath'd roots a catalog read is allowed to touch.

    Every assistant config root plus the validated project directory. ``glob``
    will happily walk a symlinked directory out of these trees, so this list is
    the actual boundary: it is checked against the *realpath* of each file at
    open time.
    """
    candidates = ["~/.claude", "~/.codex", "~/.gemini", "~/.grok"]
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates += [os.path.join(appdata, n) for n in ("claude", "codex", "gemini", "grok")]
    if project_dir:
        candidates.append(project_dir)

    roots: list[str] = []
    for candidate in candidates:
        real = _realpath_or_empty(candidate)
        if real and real not in roots:
            roots.append(real)
    return roots


def _under_allowed_root(real_path: str, allowed_roots: list[str]) -> bool:
    """True when *real_path* sits inside one of the allowed roots."""
    return any(real_path.startswith(root + os.sep) for root in allowed_roots)


# Single files a catalog read may open outside the roots. Claude Code keeps its
# user- and local-scope MCP servers in ~/.claude.json, beside ~/.claude/ rather
# than in it. Exact files, never a root: the home directory stays closed.
_ALLOWED_FILES = ("~/.claude.json",)


def _is_allowed_file(real_path: str) -> bool:
    return any(real_path == _realpath_or_empty(f) for f in _ALLOWED_FILES)


def read_head(
    path: str,
    allowed_roots: list[str],
    *,
    max_bytes: int = _MAX_HEAD_BYTES,
    max_file_bytes: int | None = None,
) -> str:
    """Read at most *max_bytes* from *path*, refusing anything unsafe.

    TOCTOU-safe, mirroring ``transcript_render._open_transcript_fd``: resolve to
    a realpath, require it under an allowed root (or to be one of
    ``_ALLOWED_FILES``), open with ``O_NOFOLLOW`` so the final component cannot
    be a symlink, reject files over *max_file_bytes* (``_MAX_FILE_BYTES`` by
    default, read at call time) via ``fstat`` on the already-open fd, then read
    only the head. Returns ``""`` on any failure.
    """
    fd = None
    try:
        real_path = os.path.realpath(path)
        if not _under_allowed_root(real_path, allowed_roots) and not _is_allowed_file(real_path):
            return ""
        flags = os.O_RDONLY
        if not _IS_WINDOWS:
            flags |= os.O_NOFOLLOW
        fd = os.open(real_path, flags)
        if os.fstat(fd).st_size > (_MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes):
            return ""
        raw = os.read(fd, max_bytes)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Tier 2 — scan targets
# ---------------------------------------------------------------------------

# A target is (glob_pattern, kind, source, style, namespace). ``style`` selects
# the parser:
#   "md"        — YAML frontmatter markdown; name derived from the path
#   "md_named"  — YAML frontmatter markdown; name from frontmatter, path fallback
#   "skill"     — SKILL.md; name from frontmatter, parent directory fallback
#   "toml"      — top-level TOML scalars
#   "mcp_json"  — {"mcpServers": {name: {...}}}
# ``namespace`` is the owning Claude plugin's name, or "" for none.
_Target = tuple[str, str, str, str, str]


def _targets(rows: list[tuple[str, str, str, str]], namespace: str = "") -> list[_Target]:
    """Attach a namespace to (pattern, kind, source, style) rows."""
    return [(*row, namespace) for row in rows]


# Claude Code plugins: what is *installed and enabled here*, not what a
# marketplace offers.
#
# This used to glob ``~/.claude/plugins/marketplaces/*/plugins/*`` -- every
# plugin in every marketplace clone. Measured 2026-09-26: the official
# marketplace holds 39 plugins, 9 were installed and 5 enabled for this
# project, so most of the "plugin" catalog was commands the session did not
# have (``/example-plugin:example-command``), and a palette built on it
# would have offered them. Claude Code's own record of what loads is:
#
#   ~/.claude/plugins/installed_plugins.json
#       {"plugins": {"<name>@<marketplace>": [{"scope", "projectPath"?,
#                                              "installPath", ...}]}}
#   enabledPlugins in ~/.claude/settings.json, then <project>/.claude/
#       settings.json, then settings.local.json -- later wins
#
# A "project" or "local" install applies only in its projectPath. The files
# live under installPath (a versioned cache directory), so the namespace
# cannot come from the path: it is the plugin's name, the part before "@".
# That is also what Claude Code prefixes: ``/code-review:go``,
# ``codex:rescue``, ``code-simplifier:code-simplifier``.
_CLAUDE_INSTALLED_PLUGINS = "~/.claude/plugins/installed_plugins.json"


def _read_json(path: str, allowed_roots: list[str]) -> object:
    """Parse a small JSON file through the sandboxed reader; None on any failure."""
    try:
        text = read_head(os.path.expanduser(path), allowed_roots, max_bytes=_MAX_JSON_BYTES)
        return json.loads(text) if text else None
    except Exception:
        return None


def _enabled_claude_plugins(project_dir: str | None, allowed_roots: list[str]) -> dict[str, bool]:
    """Merged ``enabledPlugins``: user settings, then the project's, then local."""
    paths = ["~/.claude/settings.json"]
    if project_dir:
        base = project_dir.rstrip(os.sep)
        paths += [f"{base}/.claude/settings.json", f"{base}/.claude/settings.local.json"]
    merged: dict[str, bool] = {}
    for path in paths:
        data = _read_json(path, allowed_roots)
        enabled = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(enabled, dict):
            merged.update({k: v for k, v in enabled.items() if isinstance(v, bool)})
    return merged


def claude_plugins(project_dir: str | None) -> list[tuple[str, str]]:
    """``(install_path, plugin_name)`` for each plugin Claude Code loads here.

    Installed, enabled, and in scope for *project_dir*. Never raises: a
    missing or garbled record means no plugin entries, not a broken palette.
    """
    try:
        allowed = _allowed_roots(project_dir)
        data = _read_json(_CLAUDE_INSTALLED_PLUGINS, allowed)
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, dict):
            return []
        enabled = _enabled_claude_plugins(project_dir, allowed)
        project_real = _realpath_or_empty(project_dir) if project_dir else ""
        found: list[tuple[str, str]] = []
        for key, records in plugins.items():
            if not isinstance(key, str) or enabled.get(key) is not True:
                continue
            name = key.split("@", 1)[0].strip()
            if not name or not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                if record.get("scope", "user") in ("project", "local"):
                    where = record.get("projectPath")
                    if not project_real or not isinstance(where, str) \
                            or _realpath_or_empty(where) != project_real:
                        continue
                install = record.get("installPath")
                real = _realpath_or_empty(install) if isinstance(install, str) else ""
                if real and _under_allowed_root(real, allowed) and os.path.isdir(real):
                    found.append((real, name))
                    break
        return found
    except Exception:
        return []


def _claude_plugin_targets(project_dir: str | None) -> list[_Target]:
    targets: list[_Target] = []
    for install, name in claude_plugins(project_dir):
        base = glob.escape(install.rstrip(os.sep))
        targets += _targets([
            (f"{base}/commands/**/*.md", "command", "plugin", "md"),
            (f"{base}/agents/**/*.md", "agent", "plugin", "md_named"),
            (f"{base}/skills/*/SKILL.md", "skill", "plugin", "skill"),
            # A plugin's MCP servers: an .mcp.json at its root, or plugin.json's
            # "mcpServers" -- inline, or a path to such a file (supabase ships
            # "./agents/claude/.mcp.json").
            (f"{base}/.mcp.json", "mcp", "plugin", "mcp_json"),
            (f"{base}/.claude-plugin/plugin.json", "mcp", "plugin", "plugin_mcp"),
        ], namespace=name)
    return targets


# Where each assistant keeps its MCP servers -- measured 2026-09-27 by having
# each CLI add a server in a throwaway HOME and reading what it wrote, and by
# each one's own `mcp list` inside a project with an .mcp.json:
#
#   claude       ~/.claude.json: "mcpServers" (user scope) and
#                "projects"[<realpath>]["mcpServers"] (local scope); the
#                project's .mcp.json, only once approved (see
#                _claude_mcpjson_approved); enabled plugins. Not "mcpServers"
#                in settings.json, which it ignores.
#   codex        ~/.codex/config.toml [mcp_servers.*], `enabled = false` off;
#                plus servers its bundled plugins add, which only
#                `codex mcp list --json` reports (Tier 3).
#   grok         ~/.grok/config.toml and ./.grok/config.toml, [mcp_servers.*],
#                `enabled = false` and `disabled_mcp_servers` off.
#   antigravity  ~/.gemini/config/mcp_config.json, "disabled": true off.
#   gemini       ~/.gemini/settings.json and ./.gemini/settings.json, and
#                extensions -- and none of them in a folder it does not trust.
#
# Only Claude reads .mcp.json. Offering its servers to the other four put
# servers in their palettes that they do not have.
_CLAUDE_JSON = "~/.claude.json"


def _user_targets(assistant: str) -> list[_Target]:
    """Home-directory scan targets for *assistant*. Every path is optional."""
    if assistant == "claude":
        # Plugins are scoped to a project, so they come from
        # ``_claude_plugin_targets`` in ``scan_targets``, not from here.
        return _targets([
            ("~/.claude/commands/**/*.md", "command", "user", "md"),
            ("~/.claude/agents/**/*.md", "agent", "user", "md_named"),
            ("~/.claude/skills/*/SKILL.md", "skill", "user", "skill"),
            (_CLAUDE_JSON, "mcp", "user", "claude_json"),
            # Holds approvals for the project's .mcp.json: watched, so a
            # changed approval is not served from the cache.
            ("~/.claude/settings.json", "", "user", "watch"),
        ])
    if assistant == "codex":
        return _targets([
            # ~/.codex/prompts does not exist on this machine — kept because it
            # is the documented location and costs one empty glob.
            ("~/.codex/prompts/*.md", "command", "user", "md"),
            ("~/.codex/agents/*.toml", "agent", "user", "toml"),
            ("~/.codex/skills/*/SKILL.md", "skill", "user", "skill"),
            ("~/.codex/plugins/*/skills/*/SKILL.md", "skill", "plugin", "skill"),
            ("~/.codex/plugins/*/commands/*.md", "command", "plugin", "md"),
            ("~/.codex/config.toml", "mcp", "user", "toml_mcp"),
        ])
    if assistant in ("grok", "composer"):
        return _targets([
            # ~/.grok/skills exists but is empty here; the shipped content lives
            # under ~/.grok/bundled, which is why it is marked source=builtin.
            ("~/.grok/skills/*/SKILL.md", "skill", "user", "skill"),
            ("~/.grok/agents/*.md", "agent", "user", "md_named"),
            ("~/.grok/bundled/skills/*/SKILL.md", "skill", "builtin", "skill"),
            ("~/.grok/bundled/agents/*.md", "agent", "builtin", "md_named"),
            ("~/.grok/installed-plugins/*/skills/*/SKILL.md", "skill", "plugin", "skill"),
            ("~/.grok/installed-plugins/*/commands/*.md", "command", "plugin", "md"),
            ("~/.grok/config.toml", "mcp", "user", "toml_mcp"),
        ])
    if assistant == "gemini":
        # Audited: ~/.gemini holds only config/, history/, projects.json and the
        # antigravity-cli tree — no commands/agents/skills/extensions dirs. The
        # patterns below are the documented Gemini CLI locations, all optional.
        # MCP servers come from ``_gemini_mcp_targets``: they depend on whether
        # the folder is trusted. (~/.gemini/config/mcp_config.json is
        # Antigravity's; Gemini does not read it.)
        return _targets([
            ("~/.gemini/commands/**/*.toml", "command", "user", "toml"),
            ("~/.gemini/agents/*.md", "agent", "user", "md_named"),
            ("~/.gemini/skills/*/SKILL.md", "skill", "user", "skill"),
        ])
    if assistant == "antigravity":
        # `agy` keeps its own state under ~/.gemini/antigravity-cli/ -- its
        # bundled skills are unpacked to builtin/ there -- and reads the user's
        # customizations from ~/.gemini/config/, as its bundled
        # agy-customizations skill documents. Plugins
        # (~/.gemini/config/plugins/) are not read: none exist here to measure
        # how agy records which are switched off.
        return _targets([
            ("~/.gemini/antigravity-cli/builtin/skills/*/SKILL.md", "skill", "builtin", "skill"),
            ("~/.gemini/config/skills/*/SKILL.md", "skill", "user", "skill"),
            ("~/.gemini/config/mcp_config.json", "mcp", "user", "mcp_json"),
        ])
    return []


def _project_targets(assistant: str, project_dir: str) -> list[_Target]:
    """Project-scoped scan targets. Empty when there is no project directory."""
    if not project_dir:
        return []
    base = project_dir.rstrip(os.sep)
    if assistant == "claude":
        return _targets([
            (f"{base}/.claude/commands/**/*.md", "command", "project", "md"),
            (f"{base}/.claude/agents/**/*.md", "agent", "project", "md_named"),
            (f"{base}/.claude/skills/*/SKILL.md", "skill", "project", "skill"),
            (f"{base}/.mcp.json", "mcp", "project", "claude_mcp_json"),
            (f"{base}/.claude/settings.json", "", "project", "watch"),
            (f"{base}/.claude/settings.local.json", "", "project", "watch"),
        ])
    if assistant == "codex":
        return _targets([
            (f"{base}/.codex/prompts/*.md", "command", "project", "md"),
            (f"{base}/.codex/agents/*.toml", "agent", "project", "toml"),
        ])
    if assistant in ("grok", "composer"):
        return _targets([
            (f"{base}/.grok/skills/*/SKILL.md", "skill", "project", "skill"),
            (f"{base}/.grok/agents/*.md", "agent", "project", "md_named"),
            (f"{base}/.grok/config.toml", "mcp", "project", "toml_mcp"),
        ])
    if assistant == "gemini":
        return _targets([
            (f"{base}/.gemini/commands/**/*.toml", "command", "project", "toml"),
        ])
    if assistant == "antigravity":
        # A workspace's customization root is .agents/ -- or .agent/, _agents/
        # or _agent/. (agy never reads .gemini/commands: that is Gemini CLI's.)
        return _targets([
            (f"{base}/{root}/skills/*/SKILL.md", "skill", "project", "skill")
            for root in (".agents", ".agent", "_agents", "_agent")
        ])
    return []


def _gemini_trusted(project_dir: str | None) -> bool:
    """Whether Gemini CLI trusts *project_dir* -- and so loads any MCP server there.

    Measured: in an untrusted folder `gemini mcp list` says every server,
    user-level ones included, is "disabled because this folder is untrusted".
    Trust lives in ~/.gemini/trustedFolders.json as {realpath: level};
    TRUST_FOLDER covers the folder and below, TRUST_PARENT its parent's tree,
    DO_NOT_TRUST refuses. Folder trust can be switched off entirely in
    settings (security.folderTrust.enabled = false), which trusts everything.
    """
    if not project_dir:
        return False
    try:
        allowed = _allowed_roots(project_dir)
        settings = _read_json("~/.gemini/settings.json", allowed)
        folder_trust = settings.get("security", {}).get("folderTrust", {}) if isinstance(settings, dict) else {}
        if isinstance(folder_trust, dict) and folder_trust.get("enabled") is False:
            return True
        levels = _read_json("~/.gemini/trustedFolders.json", allowed)
        if not isinstance(levels, dict):
            return False
        here = _realpath_or_empty(project_dir)
        if not here:
            return False
        trusted = False
        for path, level in levels.items():
            real = _realpath_or_empty(path) if isinstance(path, str) else ""
            if not real:
                continue
            if level == "DO_NOT_TRUST" and here == real:
                return False
            scope = os.path.dirname(real) if level == "TRUST_PARENT" else real
            if level in ("TRUST_FOLDER", "TRUST_PARENT") and (here == scope or here.startswith(scope + os.sep)):
                trusted = True
        return trusted
    except Exception:
        return False


def _gemini_mcp_targets(project_dir: str | None) -> list[_Target]:
    if not _gemini_trusted(project_dir):
        return []
    base = (project_dir or "").rstrip(os.sep)
    return _targets([
        ("~/.gemini/settings.json", "mcp", "user", "mcp_json"),
        ("~/.gemini/extensions/*/gemini-extension.json", "mcp", "plugin", "mcp_json"),
        (f"{glob.escape(base)}/.gemini/settings.json", "mcp", "project", "mcp_json"),
    ])


def scan_targets(assistant: str, project_dir: str | None) -> list[_Target]:
    """All Tier 2 scan targets for an assistant + optional project directory."""
    targets = _user_targets(assistant) + _project_targets(assistant, project_dir or "")
    if assistant == "claude":
        targets += _claude_plugin_targets(project_dir)
    if assistant == "gemini":
        targets += _gemini_mcp_targets(project_dir)
    return targets


# ---------------------------------------------------------------------------
# Tier 2 — the scan itself
# ---------------------------------------------------------------------------


def _expand(pattern: str) -> list[str]:
    """Glob one pattern, sorted and capped. Missing directories yield ``[]``."""
    try:
        expanded = os.path.expanduser(pattern)
        hits = glob.glob(expanded, recursive="**" in expanded)
        hits = [h for h in hits if os.path.isfile(h)]
        hits.sort()
        return hits[:_MAX_FILES_PER_PATTERN]
    except Exception:
        return []


def collect_files(targets: list[_Target]) -> list[tuple[str, _Target]]:
    """Expand every target to concrete files. Never raises."""
    found: list[tuple[str, _Target]] = []
    for target in targets:
        for path in _expand(target[0]):
            found.append((path, target))
            if len(found) >= _MAX_ENTRIES:
                return found
    return found


def fingerprint(files: list[tuple[str, _Target]]) -> tuple:
    """Cheap change detector: (path, mtime_ns, size) for every matched file.

    Stat-only — the expensive part of a scan is the head-reads and the parsing,
    and this skips both. Catches edits, additions and deletions; a file that
    vanishes between the stat and the read just yields no entry.
    """
    out: list[tuple[str, int, int]] = []
    for path, _target in files:
        try:
            st = os.stat(path)
            out.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((path, -1, -1))
    return tuple(out)


def _command_name(path: str, source: str, namespace: str = "") -> str:
    """Derive a slash-command name from a command file's path.

    ``~/.claude/commands/foo/bar.md`` → ``/foo:bar``. A Claude plugin's
    commands take the plugin's *name* as their namespace, passed in, because
    they live under a versioned cache directory whose path says nothing about
    it (``.../cache/official/code-review/76c85b7366c8/commands/go.md`` is
    ``/code-review:go``). Other assistants' plugin commands still take the
    directory that owns ``commands/``.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        parts = path.replace(os.sep, "/").split("/")
        if "commands" not in parts:
            return "/" + stem
        idx = len(parts) - 1 - parts[::-1].index("commands")
        pieces = parts[idx + 1 : -1] + [stem]
        if namespace:
            pieces.insert(0, namespace)
        # Elsewhere a plugin's commands/ sits directly under the plugin's own
        # directory.
        elif source == "plugin" and idx >= 1:
            pieces.insert(0, parts[idx - 1])
        return "/" + ":".join(p for p in pieces if p)
    except Exception:
        return "/" + stem


def _entry_from_file(
    path: str,
    target: _Target,
    assistant: str,
    allowed_roots: list[str],
    project_dir: str | None = None,
) -> list[dict]:
    """Parse one file into zero or more catalog entries. Never raises."""
    try:
        _pattern, kind, source, style, namespace = target

        if style == "watch":
            # Read by another file's rules; here only so its mtime is in the
            # fingerprint and a change to it is not served from the cache.
            return []
        if style == "mcp_json":
            # A Claude plugin's .mcp.json; everywhere else a plain config.
            off = _claude_plugin_off(namespace, project_dir, allowed_roots) if namespace else None
            return _entries_from_mcp_json(path, kind, source, assistant, allowed_roots, skip=off)
        if style == "claude_mcp_json":
            loaded = _claude_mcpjson_approved(project_dir, allowed_roots)
            disabled = _claude_disabled(project_dir, allowed_roots)
            return _entries_from_mcp_json(
                path, kind, source, assistant, allowed_roots,
                skip=lambda name: name in disabled or not loaded(name),
            )
        if style == "claude_json":
            return _entries_from_claude_json(path, assistant, project_dir, allowed_roots)
        if style == "plugin_mcp":
            return _entries_from_plugin_json(
                path, source, assistant, allowed_roots,
                skip=_claude_plugin_off(namespace, project_dir, allowed_roots),
            )
        if style == "toml_mcp":
            return _entries_from_toml_mcp(path, kind, source, assistant, allowed_roots)

        text = read_head(path, allowed_roots)
        if not text:
            return []

        if style == "toml":
            fields = parse_toml_head(text)
        else:
            fields = parse_frontmatter(text)

        description = _clean(fields.get("description", ""), _MAX_DESCRIPTION_CHARS)

        if style == "md":
            # A slash command: the path *is* the invocation.
            name = _command_name(path, source, namespace)
        else:
            name = _clean(fields.get("name", ""), _MAX_NAME_CHARS)
            if not name:
                # A skill is named by its directory; everything else by its file.
                stem = os.path.dirname(path) if style == "skill" else path
                name = os.path.splitext(os.path.basename(stem))[0]
            if name and namespace:
                # Claude Code prefixes a plugin's agents and skills with the
                # plugin: ``codex:rescue``, ``code-simplifier:code-simplifier``.
                name = f"{namespace}:{name}"

        if not name:
            return []
        entry = {
            "kind": kind,
            "name": name,
            "description": description,
            "source": source,
            "assistant": assistant,
            "path": path,
        }
        # What a command expects after its name, e.g. "[message]". Shown by a
        # palette as the prompt for dictated arguments; absent when unstated.
        hint = _clean(fields.get("argument-hint", ""), _MAX_HINT_CHARS)
        if hint and kind == "command":
            entry["argument_hint"] = hint
        return [entry]
    except Exception:
        return []


def _mcp_description(cfg: object) -> str:
    """A server's palette subtitle: its own description, else its executable's name.

    Never its arguments, environment, URL or headers: that is where the keys
    are. Measured on the dev machine -- a Stripe live key in one server's args,
    Authorization headers with a GitHub token and a Butterbase secret in
    others. Entries go to every client, so even a one-string ``command`` is cut
    to the executable, in case the rest of the line is a key.
    """
    if not isinstance(cfg, dict):
        return ""
    text = cfg.get("description")
    if not isinstance(text, str) or not text.strip():
        command = cfg.get("command")
        words = command.split() if isinstance(command, str) else []
        text = os.path.basename(words[0]) if words else ""
    return _clean(text, _MAX_DESCRIPTION_CHARS)


def _mcp_off(cfg: object) -> bool:
    """True when a server's own config turns it off: Antigravity writes
    ``"disabled": true``, Codex and Grok ``enabled = false``."""
    return isinstance(cfg, dict) and (cfg.get("disabled") is True or cfg.get("enabled") is False)


def _names(value: object) -> set[str]:
    """A JSON/TOML list of server names as a set; anything else is empty."""
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def _mcp_entries(
    servers: object,
    kind: str,
    source: str,
    assistant: str,
    path: str,
    skip: Callable[[str], bool] | None = None,
) -> list[dict]:
    """One entry per server of a ``{name: config}`` mapping that is switched on."""
    if not isinstance(servers, dict):
        return []
    out: list[dict] = []
    for name, cfg in servers.items():
        clean_name = _clean(name, _MAX_NAME_CHARS) if isinstance(name, str) else ""
        if not clean_name or _mcp_off(cfg) or (skip is not None and skip(name)):
            continue
        out.append({
            "kind": kind,
            "name": clean_name,
            "description": _mcp_description(cfg),
            "source": source,
            "assistant": assistant,
            "path": path,
        })
    return out


def _entries_from_mcp_json(
    path: str,
    kind: str,
    source: str,
    assistant: str,
    allowed_roots: list[str],
    skip: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Expand an ``{"mcpServers": {...}}`` config into one entry per server."""
    try:
        text = read_head(path, allowed_roots, max_bytes=_MAX_JSON_BYTES)
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, dict):
            return []
        return _mcp_entries(data.get("mcpServers"), kind, source, assistant, path, skip)
    except Exception:
        return []


def _entries_from_toml_mcp(
    path: str, kind: str, source: str, assistant: str, allowed_roots: list[str]
) -> list[dict]:
    """``[mcp_servers.<name>]`` tables, as Codex and Grok write them.

    ``enabled = false`` turns one off; so does Grok's top-level
    ``disabled_mcp_servers``, which ``grok mcp disable`` writes as well.
    """
    try:
        import tomllib

        text = read_head(path, allowed_roots, max_bytes=_MAX_JSON_BYTES)
        if not text:
            return []
        data = tomllib.loads(text)
        off = _names(data.get("disabled_mcp_servers"))
        return _mcp_entries(data.get("mcp_servers"), kind, source, assistant, path, skip=off.__contains__)
    except Exception:
        return []


def _read_claude_json(allowed_roots: list[str]) -> dict:
    """``~/.claude.json``, parsed whole, or ``{}``."""
    try:
        text = read_head(
            os.path.expanduser(_CLAUDE_JSON), allowed_roots,
            max_bytes=_MAX_CLAUDE_JSON_BYTES, max_file_bytes=_MAX_CLAUDE_JSON_BYTES,
        )
        data = json.loads(text) if text else None
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _claude_project_record(data: dict, project_dir: str | None) -> dict:
    """``projects[<project>]`` of ~/.claude.json -- keyed by the realpath, as
    Claude Code writes it (measured: a project under /tmp is filed under
    /private/tmp)."""
    projects = data.get("projects")
    if not project_dir or not isinstance(projects, dict):
        return {}
    for key in (_realpath_or_empty(project_dir), project_dir.rstrip(os.sep)):
        record = projects.get(key)
        if isinstance(record, dict):
            return record
    return {}


def _entries_from_claude_json(
    path: str, assistant: str, project_dir: str | None, allowed_roots: list[str]
) -> list[dict]:
    """Claude Code's user-scope servers, this project's local-scope ones and
    the claude.ai connectors -- less those switched off for this project."""
    data = _read_claude_json(allowed_roots)
    local = _claude_project_record(data, project_dir)
    off = _names(local.get("disabledMcpServers")).__contains__
    entries = _mcp_entries(data.get("mcpServers"), "mcp", "user", assistant, path, skip=off)
    entries += _mcp_entries(local.get("mcpServers"), "mcp", "project", assistant, path, skip=off)
    return entries + _claude_connectors(data, assistant, path, off)


def _claude_connectors(
    data: dict, assistant: str, path: str, off: Callable[[str], bool]
) -> list[dict]:
    """The claude.ai connectors (Gmail, Google Drive, ...) of the signed-in account.

    They are not in any config file: Claude Code fetches them from claude.ai
    at startup. Its only local record is ``claudeAiMcpEverConnected`` in
    ~/.claude.json -- each connector that has ever connected, by display name
    ("claude.ai Gmail"), so one disconnected since is still listed. The list
    holds names and nothing else.
    """
    names = data.get("claudeAiMcpEverConnected")
    entries: list[dict] = []
    for name in names if isinstance(names, list) else []:
        clean = _clean(name, _MAX_NAME_CHARS) if isinstance(name, str) else ""
        if clean and not off(name):
            entries.append({
                "kind": "mcp",
                "name": clean,
                "description": "claude.ai connector",
                "source": "user",
                "assistant": assistant,
                "path": path,
            })
    return entries


def _claude_disabled(project_dir: str | None, allowed_roots: list[str]) -> set[str]:
    """Servers switched off for this project (``/mcp`` → disable): its
    ~/.claude.json record's ``disabledMcpServers``. Measured in 2.1.283's
    source: it applies to a server of any scope; a plugin's is named
    ``plugin:<plugin>:<server>``, a connector by its display name."""
    record = _claude_project_record(_read_claude_json(allowed_roots), project_dir)
    return _names(record.get("disabledMcpServers"))


def _claude_plugin_off(
    plugin: str, project_dir: str | None, allowed_roots: list[str]
) -> Callable[[str], bool]:
    disabled = _claude_disabled(project_dir, allowed_roots)
    return lambda name: f"plugin:{plugin}:{name}" in disabled


def _claude_mcpjson_approved(project_dir: str | None, allowed_roots: list[str]) -> Callable[[str], bool]:
    """Which of a project's .mcp.json servers Claude Code actually loads.

    Measured on 2.1.283 against a throwaway HOME, since the answer is not what
    the setting names suggest:

    - rejected anywhere (``disabledMcpjsonServers``) -- never;
    - approved in user settings (``enabledMcpjsonServers``, or
      ``enableAllProjectMcpServers``) -- yes;
    - approved in the project's settings, settings.local.json or its record in
      ~/.claude.json -- yes, but only once the folder is trusted
      (``hasTrustDialogAccepted``): a repository cannot approve its own servers;
    - otherwise "Pending approval": listed by ``claude mcp list``, never loaded.
    """
    base = project_dir.rstrip(os.sep) if project_dir else ""
    user = [d for d in (_read_json("~/.claude/settings.json", allowed_roots),) if isinstance(d, dict)]
    record = _claude_project_record(_read_claude_json(allowed_roots), project_dir)
    project_files = (
        [_read_json(f"{base}/.claude/settings.json", allowed_roots),
         _read_json(f"{base}/.claude/settings.local.json", allowed_roots)]
        if base else []
    )
    project = [d for d in [*project_files, record] if isinstance(d, dict)]
    rejected = set().union(*(_names(d.get("disabledMcpjsonServers")) for d in user + project))
    trusted = record.get("hasTrustDialogAccepted") is True

    def approves(settings: dict, name: str) -> bool:
        return settings.get("enableAllProjectMcpServers") is True or name in _names(settings.get("enabledMcpjsonServers"))

    def loaded(name: str) -> bool:
        if name in rejected:
            return False
        if any(approves(d, name) for d in user):
            return True
        return trusted and any(approves(d, name) for d in project)

    return loaded


def _entries_from_plugin_json(
    path: str,
    source: str,
    assistant: str,
    allowed_roots: list[str],
    skip: Callable[[str], bool] | None = None,
) -> list[dict]:
    """A Claude plugin's ``plugin.json`` ``"mcpServers"``: inline servers, or
    paths to .mcp.json files inside the plugin -- never outside it."""
    data = _read_json(path, allowed_roots)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, dict):
        return _mcp_entries(servers, "mcp", source, assistant, path, skip)
    refs = [servers] if isinstance(servers, str) else servers if isinstance(servers, list) else []
    root = _realpath_or_empty(os.path.dirname(os.path.dirname(path)))
    entries: list[dict] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip() or not root:
            continue
        target = os.path.realpath(os.path.join(root, ref))
        if target.startswith(root + os.sep):
            entries += _entries_from_mcp_json(target, "mcp", source, assistant, allowed_roots, skip)
    return entries


def scan_filesystem(assistant: str, project_dir: str | None = None) -> list[dict]:
    """Tier 2: scan disk for commands, agents, skills and MCP servers.

    Pure, synchronous, stdlib-only and total: a missing directory, an
    unreadable file or garbled frontmatter each contribute nothing rather than
    raising. Entries are de-duplicated on (kind, name, source) and sorted.
    """
    try:
        allowed_roots = _allowed_roots(project_dir)
        files = collect_files(scan_targets(assistant, project_dir))
        return _entries_from_files(files, assistant, allowed_roots, project_dir)
    except Exception:
        return []


def _entries_from_files(
    files: list[tuple[str, _Target]],
    assistant: str,
    allowed_roots: list[str],
    project_dir: str | None = None,
) -> list[dict]:
    """Parse collected files into a de-duplicated, sorted entry list.

    An MCP server is one row whatever scopes name it -- Claude Code resolves a
    name defined in several to one server -- so it is de-duplicated on name.
    """
    entries: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for path, target in files:
        for entry in _entry_from_file(path, target, assistant, allowed_roots, project_dir):
            key = (entry["kind"], entry["name"], "" if entry["kind"] == "mcp" else entry["source"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
            if len(entries) >= _MAX_ENTRIES:
                return _sorted(entries)
    return _sorted(entries)


def _sorted(entries: list[dict]) -> list[dict]:
    """Stable palette ordering: kind, then source, then name."""
    kind_rank = {kind: i for i, kind in enumerate(KINDS)}
    source_rank = {source: i for i, source in enumerate(SOURCES)}
    return sorted(
        entries,
        key=lambda e: (
            kind_rank.get(e.get("kind", ""), len(KINDS)),
            source_rank.get(e.get("source", ""), len(SOURCES)),
            e.get("name", ""),
        ),
    )


# ---------------------------------------------------------------------------
# Tier 2 cache — mtime fingerprint
# ---------------------------------------------------------------------------

# key -> (fingerprint, entries, built_at)
_SCAN_CACHE: dict[tuple[str, str], tuple[tuple, tuple[dict, ...], float]] = {}
_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_CACHE_MAX = 32


def scan_filesystem_cached(
    assistant: str, project_dir: str | None = None
) -> tuple[list[dict], bool]:
    """Memoized :func:`scan_filesystem`, invalidated by an mtime fingerprint.

    Returns ``(entries, from_cache)``. The glob + stat pass always runs (it is
    microseconds); only the head-reads and parsing are skipped on a hit, so an
    edited or deleted file is picked up on the next call. On a scan failure the
    previous entries are served rather than an empty palette — a stale entry
    beats a spinner in a headset.
    """
    key = (assistant, project_dir or "")
    try:
        files = collect_files(scan_targets(assistant, project_dir))
        fp = fingerprint(files)
    except Exception:
        with _SCAN_CACHE_LOCK:
            cached = _SCAN_CACHE.get(key)
        return (list(cached[1]), True) if cached else ([], False)

    with _SCAN_CACHE_LOCK:
        cached = _SCAN_CACHE.get(key)
        if cached is not None and cached[0] == fp:
            return list(cached[1]), True

    try:
        entries = _entries_from_files(files, assistant, _allowed_roots(project_dir), project_dir)
    except Exception:
        return (list(cached[1]), True) if cached else ([], False)

    with _SCAN_CACHE_LOCK:
        if key not in _SCAN_CACHE and len(_SCAN_CACHE) >= _SCAN_CACHE_MAX:
            try:
                del _SCAN_CACHE[next(iter(_SCAN_CACHE))]
            except (StopIteration, KeyError):
                pass
        _SCAN_CACHE[key] = (fp, tuple(entries), time.time())
    return entries, False


def clear_cache() -> None:
    """Drop every cached scan. Used by tests and an explicit palette refresh."""
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE.clear()
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()
    _PROBE_ATTEMPTED.clear()


# ---------------------------------------------------------------------------
# Tier 3 — CLI probe: what only the CLI itself can say
# ---------------------------------------------------------------------------
#
# Probing costs seconds, so it never runs inside a request: a catalog request
# that finds Tier 3 stale starts a refresh in the background and answers from
# what is cached (``refresh_probe_if_stale``); the palette's next open has it.
#
# Only listings that are machine-readable and read-only are run. Measured
# 2026-09-27:
#
#     codex mcp list --json  ~9 s. The only way to see the servers Codex's
#                            bundled plugins add (cua_repl, codex_app), and
#                            whether each is enabled. Each record also carries
#                            its transport -- command lines, URLs, and
#                            Authorization headers with live keys in them --
#                            so the parser keeps a name and nothing else.
#     claude -p, sent the    0.2 s. The installed build's bundled skills
#     SDK's initialize       (/simplify, /loop, ...) and built-in agents, with
#     request                descriptions and argument hints -- what changes
#                            most between Claude releases. Answered before any
#                            model call, and none is made: stdin closes after
#                            the request. Run signed out, in an empty
#                            temporary HOME, so it loads none of the user's
#                            settings, hooks, plugins or MCP servers, and
#                            writes nothing of theirs.
#
# Deliberately not run:
#
#     claude mcp list        health-checks every server: it starts every stdio
#                            MCP server on the machine, and calls the rest.
#     gemini mcp list        connects too, in a trusted folder.
#     grok / agy mcp list    human-readable only, and their config files say
#                            the same thing (Tier 2).
#
# Other inventory commands that exist, for whoever extends this: ``agy
# agents``, ``agy plugin list``, ``gemini extensions list``, ``claude plugin
# details <name>``, ``codex plugin list``, ``grok plugin list``, ``grok
# inspect``. Run a probe with ``asyncio.create_subprocess_exec``, an explicit
# timeout and ``get_subprocess_env()`` -- never ``shell=True`` -- and never let
# anything but names and descriptions out of its parser.

_PROBE_TTL_SECONDS = 15 * 60

# A probe that failed (not installed, timed out, non-zero exit) is not retried
# on every palette open: a missing `codex` would otherwise cost a spawn each time.
_PROBE_RETRY_SECONDS = 5 * 60
_PROBE_TIMEOUT_SECONDS = 30.0
_PROBE_OUTPUT_MAX_BYTES = 1024 * 1024


def _parse_codex_mcp_list(stdout: str) -> list[dict]:
    """``codex mcp list --json``: the enabled servers' names. Nothing else is
    read from a record -- its ``transport`` holds keys."""
    data = json.loads(stdout)
    servers = data if isinstance(data, list) else data.get("servers") if isinstance(data, dict) else None
    entries: list[dict] = []
    for server in servers if isinstance(servers, list) else []:
        if not isinstance(server, dict) or server.get("enabled") is False:
            continue
        name = server.get("name")
        if isinstance(name, str) and name.strip():
            entries.append({"kind": "mcp", "name": name, "description": "", "source": "user"})
    return entries


# Built-ins in Claude Code's initialize answer that a terminal session does not
# offer: headless-only variants and internal hand-offs (2.1.283). Names that
# start with "_" and "(removed)" stubs are dropped by rule.
_CLAUDE_NOT_INTERACTIVE = frozenset({
    "auto-mode-setup", "design-consent", "design-revoke", "focus", "heapdump",
    "workflow-launch-exec",
})


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _parse_claude_initialize(stdout: str) -> list[dict]:
    """Claude Code's answer to the SDK's ``initialize`` control request.

    Its ``commands`` are what a headless session offers: most of them a
    terminal session has too (Tier 1 already describes those, in the
    terminal's wording), plus the bundled skills. Its ``agents`` are the
    built-in subagents. Names, descriptions and hints are all that is read.
    """
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("type") != "control_response":
            continue
        answer = message.get("response")
        answer = answer.get("response") if isinstance(answer, dict) else None
        if not isinstance(answer, dict):
            break
        entries: list[dict] = []
        for command in answer.get("commands") or []:
            name = _text(command.get("name")).strip() if isinstance(command, dict) else ""
            description = _text(command.get("description")) if name else ""
            if (not name or name.startswith("_") or name in _CLAUDE_NOT_INTERACTIVE
                    or description.startswith("(removed)")):
                continue
            entry = {"kind": "command", "name": "/" + name, "description": description, "source": "builtin"}
            hint = _text(command.get("argumentHint")).strip()
            if hint:
                entry["argument_hint"] = hint
            entries.append(entry)
        for agent in answer.get("agents") or []:
            name = _text(agent.get("name")).strip() if isinstance(agent, dict) else ""
            if name:
                entries.append({
                    "kind": "agent", "name": name, "description": _text(agent.get("description")),
                    "source": "builtin",
                })
        return entries
    raise ValueError("no initialize answer")


class _Probe(NamedTuple):
    """One read-only CLI listing and the parser of its stdout."""

    argv: tuple[str, ...]
    parse: Callable[[str], list[dict]]
    # Written to the CLI's stdin, which is then closed.
    stdin: str | None = None
    # Run with HOME and the working directory in an empty temporary directory,
    # and none of the environment that would point the CLI back at the user's
    # own configuration (see ``_ISOLATED_DROP``).
    isolated: bool = False


_CLAUDE_INITIALIZE = json.dumps({
    "type": "control_request",
    "request_id": "voxherd-catalog",
    "request": {"subtype": "initialize"},
}) + "\n"

# assistant -> the probes to run.
_PROBES: dict[str, tuple[_Probe, ...]] = {
    "claude": (_Probe(
        ("claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose", "--setting-sources", "project", "--strict-mcp-config",
         "--mcp-config", '{"mcpServers":{}}'),
        _parse_claude_initialize,
        stdin=_CLAUDE_INITIALIZE,
        isolated=True,
    ),),
    "codex": (_Probe(("codex", "mcp", "list", "--json"), _parse_codex_mcp_list),),
}

# An isolated probe runs without these -- CLAUDE_CONFIG_DIR would send Claude
# back to the user's config, and nothing of VoxHerd's belongs in it -- and
# with these: no self-update, no network it does not need.
_ISOLATED_DROP = ("CLAUDE", "ANTHROPIC", "VOXHERD")
_ISOLATED_ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

_PROBE_ATTEMPTED: dict[str, float] = {}
_PROBES_RUNNING: set[str] = set()
_PROBE_TASKS: set[asyncio.Task] = set()


async def _run_probe(probe: _Probe) -> str | None:
    """stdout of *probe*'s CLI, or None if it is missing, slow or fails."""
    from bridge.env_utils import get_subprocess_env

    proc = None
    scratch = None
    try:
        env = get_subprocess_env()
        if probe.isolated:
            scratch = tempfile.TemporaryDirectory(prefix="voxherd-catalog-", ignore_cleanup_errors=True)
            env = {k: v for k, v in env.items() if not k.startswith(_ISOLATED_DROP)}
            env.update(_ISOLATED_ENV, HOME=scratch.name, USERPROFILE=scratch.name)
        proc = await asyncio.create_subprocess_exec(
            *probe.argv,
            stdin=asyncio.subprocess.DEVNULL if probe.stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=scratch.name if scratch else None,
        )
        request = probe.stdin.encode() if probe.stdin is not None else None
        out, _ = await asyncio.wait_for(proc.communicate(request), timeout=_PROBE_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            return None
        return out[:_PROBE_OUTPUT_MAX_BYTES].decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        # Leave no CLI running behind a cancelled probe -- and do not await
        # its exit: this is shutdown, and the caller is waiting on us.
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        raise
    except Exception:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        return None
    finally:
        if scratch is not None:
            scratch.cleanup()


async def refresh_probe(assistant: str) -> bool:
    """Run *assistant*'s probes and cache what they find. Never raises.

    All or nothing: when any probe fails the previous entries stay, since a
    stale entry beats a hole. True when fresh entries were stored.
    """
    probes = _PROBES.get(assistant)
    if not probes:
        return False
    _PROBE_ATTEMPTED[assistant] = time.time()
    entries: list[dict] = []
    for spec in probes:
        probe = _Probe(*spec)
        out = await _run_probe(probe)
        if out is None:
            return False
        try:
            entries += probe.parse(out)
        except Exception:
            return False
    store_probe_entries(assistant, entries)
    return True


def refresh_probe_if_stale(assistant: str) -> None:
    """Refresh Tier 3 in the background when it is stale. The caller never
    waits: the palette renders from cache."""
    if assistant not in _PROBES or assistant in _PROBES_RUNNING or not probe_is_stale(assistant):
        return
    if time.time() - _PROBE_ATTEMPTED.get(assistant, 0.0) < _PROBE_RETRY_SECONDS:
        return
    _PROBES_RUNNING.add(assistant)

    async def run() -> None:
        try:
            await refresh_probe(assistant)
        finally:
            _PROBES_RUNNING.discard(assistant)

    task = asyncio.get_running_loop().create_task(run())
    # The loop keeps only a weak reference to a task: hold it until it is done.
    _PROBE_TASKS.add(task)
    task.add_done_callback(_PROBE_TASKS.discard)


async def stop_probes(timeout: float = 5.0) -> None:
    """Cancel the probes in flight and wait for them, while the loop still runs.

    Call it on shutdown, before the loop's own teardown. Measured on Python
    3.11: when ``asyncio.run()`` cancels every task at once and catches a probe
    inside ``create_subprocess_exec``, the loop hangs for good -- the task
    connecting the child's pipes is cancelled with it, and the child's exit is
    never reported. Cancelled here, with the loop running, a probe ends in
    milliseconds.
    """
    loop = asyncio.get_running_loop()
    tasks = [t for t in _PROBE_TASKS if not t.done() and t.get_loop() is loop]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)

# assistant -> (stored_at, entries)
_PROBE_CACHE: dict[str, tuple[float, tuple[dict, ...]]] = {}
_PROBE_LOCK = threading.Lock()


def store_probe_entries(assistant: str, entries: list[dict]) -> None:
    """Seam: a background CLI-probe refresher deposits Tier 3 entries here.

    Entries are normalized and stamped; malformed rows are dropped rather than
    poisoning the palette.
    """
    normalized: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name", ""), _MAX_NAME_CHARS)
        if not name:
            continue
        kind = entry.get("kind") if entry.get("kind") in KINDS else "command"
        row = {
            "kind": kind,
            "name": name,
            "description": _clean(entry.get("description", ""), _MAX_DESCRIPTION_CHARS),
            "source": entry.get("source") if entry.get("source") in SOURCES else "plugin",
            "assistant": assistant,
            "path": None,
        }
        hint = _clean(entry.get("argument_hint", ""), _MAX_HINT_CHARS)
        if hint and kind == "command":
            row["argument_hint"] = hint
        normalized.append(row)
    with _PROBE_LOCK:
        _PROBE_CACHE[assistant] = (time.time(), tuple(normalized))


def probe_entries(assistant: str) -> list[dict]:
    """Tier 3 entries from cache. Empty until a refresher has run.

    Past the TTL the entries are still returned — the palette prefers stale data
    to a hole — but ``probe_age_seconds`` lets a caller flag them as old.
    """
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(assistant)
    return list(cached[1]) if cached else []


def probe_age_seconds(assistant: str) -> float | None:
    """Seconds since Tier 3 was last refreshed, or ``None`` if never."""
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(assistant)
    return (time.time() - cached[0]) if cached else None


def probe_is_stale(assistant: str) -> bool:
    """True when Tier 3 has never run or has aged past its TTL."""
    age = probe_age_seconds(assistant)
    return age is None or age > _PROBE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Invocation — what to send to use an entry
# ---------------------------------------------------------------------------

# Assistants whose skills are slash commands. Claude Code's are; Grok's slash
# history holds its bundled skills (``/code-review``); Antigravity documents
# "first-class slash command support (typing /<name>)" for every skill. Codex
# and Gemini CLI reach a skill by being asked to use it.
_SLASH_SKILLS = frozenset({"claude", "grok", "composer", "antigravity"})


def invocation(entry: dict) -> tuple[str | None, bool]:
    """``(text, needs_args)``: what to type to use *entry*, or ``(None, False)``.

    The bridge owns this so no client has to know an assistant's idiom — the
    same reasoning as pointing in the Reader. A client appends
    whatever the user dictated after a space and sends it as a
    ``voice_command``; the dispatch path handles busy sessions and dead panes.

    - A command's name already is its invocation: ``/git:sync``. It needs
      words only when the entry says so (``needs_args``: a built-in the CLI
      rejects bare, like Grok's ``/btw <question>``).
    - Where skills are slash commands they are invoked as one: Claude's
      ``/br``, ``/codex:rescue``; Grok's and Antigravity's too (see
      ``_SLASH_SKILLS``).
    - Agents, other assistants' skills and MCP servers are reached by asking
      for them, which every assistant understands and no syntax change
      breaks. The phrase is a sentence opener, so it **needs** arguments —
      "Use the code-reviewer agent to" alone asks for nothing.
    - A plugin is not invocable itself; its commands, agents and skills are
      catalogued as entries of their own.
    """
    kind = entry.get("kind")
    name = str(entry.get("name") or "").strip()
    if not name:
        return None, False
    if kind == "command":
        return (name if name.startswith("/") else "/" + name), entry.get("needs_args") is True
    if kind == "skill" and entry.get("assistant") in _SLASH_SKILLS:
        return "/" + name.lstrip("/"), False
    if kind == "agent":
        return f"Use the {name} agent to", True
    if kind == "skill":
        return f"Use the {name} skill to", True
    if kind == "mcp":
        return f"Use the {name} MCP server to", True
    return None, False


def _with_invocation(entry: dict) -> dict:
    text, needs_args = invocation(entry)
    if text is None:
        return entry
    return {**entry, "invoke": text, "needs_args": needs_args}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def build_catalog(assistant: str, project_dir: str | None = None) -> dict:
    """Merge all three tiers into the palette payload. Never raises.

    The filesystem scan runs in a worker thread so the event loop is not blocked
    by a few hundred ``stat``/``read`` syscalls.
    """
    assistant = normalize_catalog_assistant(assistant)
    try:
        scanned, from_cache = await asyncio.to_thread(
            scan_filesystem_cached, assistant, project_dir
        )
    except Exception:
        scanned, from_cache = [], False

    # Tier 3 adds only what the tiers before it do not list: a server Tier 2
    # found is the same server when the CLI lists it, and a built-in Tier 1
    # has is already in the terminal's own words (a probe may only have the
    # headless ones).
    builtin = builtin_entries(assistant)
    listed = {(e.get("kind"), e.get("name")) for e in builtin + scanned}
    probed = [e for e in probe_entries(assistant) if (e.get("kind"), e.get("name")) not in listed]
    entries = [_with_invocation(e) for e in _sorted(builtin + scanned + probed)]
    refresh_probe_if_stale(assistant)

    counts: dict[str, int] = {}
    for entry in entries:
        kind = entry.get("kind", "")
        counts[kind] = counts.get(kind, 0) + 1

    return {
        "assistant": assistant,
        "project_dir": project_dir or None,
        "entries": entries,
        "counts": counts,
        "total": len(entries),
        "cached": from_cache,
        "probe_stale": probe_is_stale(assistant),
        # The CLI version Tier 1 was measured against (see BUILTIN_MEASURED).
        "builtin_version": BUILTIN_MEASURED.get(assistant),
    }
