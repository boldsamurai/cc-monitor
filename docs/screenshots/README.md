# Screenshots

Screenshots referenced from the project README live here. Filenames the
README expects:

- `main-sessions.png` — main view, Sessions tab visible, filter bar on top
- `session-detail.png` — drilled into a single session, Usage tab
- `project-detail.png` — drilled into a single project
- `settings.png` — Settings screen open

## How to capture

Textual ships a screenshot shortcut. While the app is running:

1. Navigate to the screen you want to capture.
2. Press `ctrl+p`, type `screenshot`, hit Enter — Textual writes an SVG
   to the current working directory.
3. Convert the SVG to PNG (any rasterizer works; `librsvg` example below):

```bash
rsvg-convert -w 1600 -o main-sessions.png screenshot.svg
```

Drop the PNG in this directory under the matching filename and commit.

## Tips

- Resize the terminal to ~140 cols × 40 rows before screenshotting — the
  app looks tighter and the Sessions table shows enough rows to be
  illustrative without dominating the README.
- Pick a moment when the BlockPanel is showing real data, not the
  "Waiting for first API response…" placeholder.
- For Settings, scroll partway down so multiple sections are visible
  rather than just the Appearance heading.
