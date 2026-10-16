"""Dependency drift watch (public registries only, no keys, no model APIs).

A repo profile may declare `dependencies`::

    {"name": "expo wrapper", "kind": "npm", "id": "expo-ondevice-ai",
     "pin_file": "locanara-versions.json", "pin_key": "expo"}

`check()` reads the live pin from the checkout file and the latest version
from the public registry, and reports drift. `watch` (cli) turns drifted
entries into OpenMainty runs so the normal fix/review/verify loop handles
the update. Scheduling is left to the OS (cron/launchd one-liner in docs);
there is no daemon and no paid service involved.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMEOUT = 15


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "openmainty/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError(f"not published at {url}") from exc
        raise


def latest(kind: str, ident: str) -> str:
    """Latest published version for kind+id. Raises on network/format errors."""
    if kind == "npm":
        data = _get_json(f"https://registry.npmjs.org/{ident}/latest")
        return data["version"]
    if kind == "pub":
        data = _get_json(f"https://pub.dev/api/packages/{ident}")
        return data["latest"]["version"]
    if kind == "maven":
        group, _, artifact = ident.partition(":")
        path = f"{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
        try:
            req = urllib.request.Request(
                f"https://repo1.maven.org/maven2/{path}",
                headers={"User-Agent": "openmainty/0.1"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                xml_text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(f"maven: {ident} not published") from exc
            raise
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        latest_el = root.find("./versioning/latest")
        if latest_el is not None and latest_el.text:
            return latest_el.text.strip()
        versions = [v.text.strip() for v in root.findall("./versioning/versions/version") if v.text]
        if not versions:
            raise ValueError(f"maven: no versions in metadata for {ident}")
        return versions[-1]
    if kind == "github":
        data = _get_json(f"https://api.github.com/repos/{ident}/releases/latest")
        tag = data.get("tag_name", "")
        return tag.lstrip("v")
    raise ValueError(f"unknown dependency kind: {kind}")


def read_pin(checkout, entry: dict) -> str:
    import re
    path = checkout / entry["pin_file"]
    text = path.read_text(encoding="utf-8")
    key = entry.get("pin_key")
    if path.suffix == ".json":
        node = json.loads(text)
        for part in (key or "").split("."):
            if part:
                node = node[part]
    else:
        # minimal `key: value` scan for pubspec.yaml-style pin files
        node = None
        for line in text.splitlines():
            m = re.match(rf"^{re.escape(key or '')}\s*:\s*([^\s#]+)", line.strip())
            if m:
                node = m.group(1).strip("'\"")
                break
        if node is None:
            raise ValueError(f"pin key '{key}' not found in {entry.get('pin_file')}")
    if not isinstance(node, str):
        raise ValueError(f"pin {entry.get('pin_file')}:{key} is not a version string")
    return node


def check(profile: dict, checkout) -> list:
    """Return [{name, kind, id, pinned, latest, drift, error}]. Never raises."""
    results = []
    for entry in profile.get("dependencies", []):
        row = {"name": entry.get("name"), "kind": entry.get("kind"),
               "id": entry.get("id"), "pinned": None, "latest": None,
               "drift": False, "unpublished": False, "error": None}
        try:
            row["pinned"] = read_pin(checkout, entry)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            row["error"] = f"pin read failed: {exc}"
            results.append(row)
            continue
        try:
            row["latest"] = latest(entry["kind"], entry["id"])
        except LookupError as exc:
            row["unpublished"] = True
            row["error"] = None
            row["note"] = str(exc)
            results.append(row)
            continue
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"registry check failed: {exc}"
            results.append(row)
            continue
        row["drift"] = (row["pinned"] != row["latest"])
        results.append(row)
    return results


def drift_body(row: dict) -> str:
    return (
        f"Dependency drift: {row['name']} ({row['kind']}:{row['id']}) "
        f"pinned at {row['pinned']}, registry latest is {row['latest']}.\n"
        "Update the pin, mirrors (docs/tables), and any lockfiles; "
        "verify with the repo's checks before drafting release notes."
    )
