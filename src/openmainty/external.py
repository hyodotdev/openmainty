"""External actions: dry-run first, always.

v1 rule: nothing here posts, merges, deploys, or sends channel messages.
`preview()` renders exactly what would be created. `apply()` exists only
behind an explicit approval file and still only *prepares* payloads in v1 —
it reports every action as `prepared (send disabled in v1)`.

The current user request forbids operational sends; this module enforces it.
"""
from __future__ import annotations


def preview(state: dict) -> dict:
    arts = state.get("artifacts", {})
    return {
        "run": state["id"],
        "repo": state["repo"],
        "issue": state["issue"],
        "mode": "dry-run",
        "would_create": {
            "pr": {"target": "base branch of repo profile", "body_file": arts.get("pr")},
            "release_notes": {"body_file": arts.get("release_notes")},
            "medium": {"body_file": arts.get("medium"), "action": "draft only"},
            "discord": {"body_file": arts.get("discord"), "action": "draft only"},
            "slack": {"body_file": arts.get("slack"), "action": "draft only"},
        },
        "sends": "none (dry-run; no network calls made)",
    }


def apply(state: dict, approval_file=None) -> dict:
    result = preview(state)
    result["mode"] = "approved-prepare (send disabled in v1)"
    if not approval_file:
        result["error"] = "refusing: an explicit approval file is required"
        return result
    result["sends"] = "none (v1 prepares payloads only; no live PR/review-bot/message sent)"
    return result
