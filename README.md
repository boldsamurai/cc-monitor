# cc-usagemonitor

Real-time token usage and cost monitor for Claude Code sessions, in your terminal.

[![PyPI version](https://img.shields.io/pypi/v/cc-usagemonitor.svg)](https://pypi.org/project/cc-usagemonitor/)
[![Python](https://img.shields.io/pypi/pyversions/cc-usagemonitor.svg)](https://pypi.org/project/cc-usagemonitor/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Downloads](https://static.pepy.tech/badge/cc-usagemonitor)](https://pepy.tech/project/cc-usagemonitor)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-5a4fcf.svg)](https://textual.textualize.io/)

A Textual TUI that tails Claude Code's session logs, correlates them with the
hook event stream, and gives you a live view of every active project, every
session, and every model — with live 5h-block tracking so you know how close
you are to your plan limit before the bill (or the rate limit) catches you off
guard.

---

## Table of contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Installation](#installation)
  - [uv (recommended)](#uv-recommended)
  - [pipx](#pipx)
  - [pip](#pip)
  - [From source](#from-source)
- [Quick start](#quick-start)
- [CLI flags](#cli-flags)
- [Configuration](#configuration)
- [Development](#development)
- [Acknowledgements](#acknowledgements)
- [License](#license)

---

## Features

- **Live 5h-block tracking** — progress bar against your Anthropic plan limit
  (auto-derived P90 in offline mode; authoritative `/api/oauth/usage` numbers
  when OAuth is available)
- **Toast notifications** at 80% and 100% of cost ceiling so you don't have
  to babysit the screen
- **Sessions / Projects / Models tabs** with click-to-sort columns,
  date / cost / model filters, and full keyboard navigation
- **Per-session detail screen** — usage charts, tool counts, file-read /
  file-write hot lists, full per-turn context window utilization
- **Project detail screen** — same metrics rolled up across every session
  in the project, plus 7-day cumulative cost / token charts
- **CSV / JSON export** of sessions, projects, and models for pandas / Excel
  analysis
- **Cross-run state snapshot** — quit and relaunch in <1s even with
  hundreds of sessions and an 8-day archive
- **Cache-aware detail screens** — first open parses the JSONL once;
  re-opens are instant
- **Mouse and keyboard fully wired** — clickable filter chips, back button,
  sort headers, drill-in rows; or use `1`/`2`/`3`/`/`/`,`/`?` for everything

## Screenshots

| Main view (Sessions tab) | Session detail |
| :---: | :---: |
| ![Sessions tab](https://raw.githubusercontent.com/boldsamurai/cc-monitor/main/docs/screenshots/main-sessions.png) | ![Session detail](https://raw.githubusercontent.com/boldsamurai/cc-monitor/main/docs/screenshots/session-detail.png) |

| Project detail | Settings |
| :---: | :---: |
| ![Project detail](https://raw.githubusercontent.com/boldsamurai/cc-monitor/main/docs/screenshots/project-detail.png) | ![Settings](https://raw.githubusercontent.com/boldsamurai/cc-monitor/main/docs/screenshots/settings.png) |

## Installation

### uv (recommended)

```bash
uv tool install cc-usagemonitor
```

`uv tool` creates an isolated environment automatically, sidesteps PEP 668
("externally-managed-environment") errors on modern Linux, and adds the
entry-point shim to `~/.local/bin` without touching system Python.

### pipx

```bash
pipx install cc-usagemonitor
```

### pip

```bash
pip install --user cc-usagemonitor
```

On Debian/Ubuntu/Fedora-based systems with system Python you'll need
`--user` to bypass PEP 668. Prefer `uv` or `pipx` instead.

### From source

```bash
git clone https://github.com/boldsamurai/cc-monitor.git
cd cc-monitor
uv tool install .
```

If `cc-usagemonitor` isn't on PATH after install, add `~/.local/bin` to
your shell's PATH.

## Quick start

Just run:

```bash
cc-usagemonitor
```

On first launch it will:

1. Install a hook into `~/.claude/settings.json` so Claude Code feeds it
   `tool_start` / `tool_end` events (idempotent, safe to re-run).
2. Replay every JSONL in `~/.claude/projects/` to populate the 8-day archive.
3. Auto-detect OAuth credentials. If found, pull authoritative 5h / 7d
   utilization from Anthropic. Otherwise fall back to a P90-derived local
   ceiling.
4. Drop you into the Sessions tab.

Press `?` (or `ctrl+h`) at any time for the keyboard cheatsheet.

## CLI flags

| Flag | Default | Notes |
|---|---|---|
| `--poll N` | `0.5` | Polling interval (seconds) for the JSONL tailer. |
| `--max-5h-cost X` | — | Pin a custom USD ceiling for the 5h-block progress bar. Without it, `--no-api` mode auto-derives a P90 ceiling from your last 8 days. |
| `--no-api` | off | Disable Anthropic `/api/oauth/usage` polling. Stay fully offline; falls back to the local P90 ceiling. |
| `--debug` | off | DEBUG-level logging to `~/.cache/cc-usagemonitor/usagemonitor.log` (rotates at 10 MB). |
| `--reinstall-hook` | — | Re-run the hook installer and exit without launching the TUI. Idempotent — safe in provisioning scripts. |
| `--rescan` | — | Discard the cached state snapshot before launch so the next run replays every JSONL from scratch. CLI equivalent of Settings → Force re-scan. |

Everything else (theme, date format, default tab, refresh interval, filter
persistence, confirms) is editable from the in-app Settings screen — `,`
key from the main view.

## Configuration

| Path | Purpose |
|---|---|
| `~/.config/cc-usagemonitor/config.json` | Settings persisted from the in-app Settings screen. |
| `~/.cache/cc-usagemonitor/state.pickle` | Cross-run state snapshot. Missing / corrupt / version-mismatched → fall back to full replay. |
| `~/.cache/cc-usagemonitor/usagemonitor.log` | Rolling log file (10 MB cap). |
| `~/.cache/cc-usagemonitor/exports/` | Timestamped CSV / JSON dumps from Settings → Export. |

None of the above is committed to your repo.

## Development

```bash
git clone https://github.com/boldsamurai/cc-monitor.git
cd cc-monitor
uv sync                      # install runtime + dev deps
uv run pytest                # 88 tests
uv run cc-usagemonitor       # launch from source
```

## Acknowledgements

Built with [Textual](https://textual.textualize.io/) and
[plotext](https://github.com/piccolomo/plotext) (via
[textual-plotext](https://github.com/Textualize/textual-plotext)).
The 5h-block detection algorithm is informed by
[Maciek-roboblog/Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor).

## License

MIT — see [LICENSE](LICENSE).
