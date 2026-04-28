from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"
EVENT_LOG = CLAUDE_HOME / "usagemonitor-events.jsonl"

PRICING_FILE = Path(__file__).parent / "pricing.json"
