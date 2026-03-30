"""
Shared error logging utility for all monitoring scripts.

Appends structured JSON lines to logs/errors.jsonl.
Each entry: {timestamp, script, severity, message, context}.

Assumes CWD is the repo root (true for GitHub Actions and local runs via
`python3 scripts/<name>.py`). Never raises — logging failures are printed
to stderr and silently ignored so they never crash the calling script.
"""
import json
import os
import sys
from datetime import datetime, timezone


def log_error(
    script: str,
    severity: str,
    message: str,
    context: dict | None = None,
    log_path: str = "logs/errors.jsonl",
    repo: str = "framerlabs-automations",
) -> None:
    """Append one structured error entry to the log file.

    Args:
        script:   Name of the calling script, e.g. "framer_templates".
        severity: "warning" or "error".
        message:  Human-readable description of what went wrong.
        context:  Optional dict with extra debug info (URLs, counts, etc.).
        log_path: Path to the JSONL file (relative to CWD). Defaults to
                  logs/errors.jsonl.
        repo:     Repository name for the log entry. Defaults to
                  "framerlabs-automations".
    """
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "script": script,
            "severity": severity,
            "repo": repo,
            "message": message,
            "context": context or {},
        }
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[error_log] Failed to write log entry: {e}", file=sys.stderr)
