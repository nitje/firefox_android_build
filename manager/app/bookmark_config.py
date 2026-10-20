"""Server-side Firefox bookmark configuration, snapshots, and HTML imports.

Uses the Firefox Bookmarks enterprise policy (editable bookmark items), not
ManagedBookmarks. Policy supports toolbar/menu and a single folder level.
"""
from __future__ import annotations
import json
import re
import uuid
from html.parser import HTMLParser
from urllib.parse import urlsplit
from typing import Any


def starter_config() -> dict[str, Any]:
    return {"schema": 1, "defaults_revision": 2, "folders": ["About Settings"], "entries": [
        {"id": "about_" + target.split(":")[1], "enabled": True,
         "title": target, "url": target, "placement": "toolbar", "folder": "About Settings"}
        for target in ("about:sync-log", "about:config", "about:support", "about:profiles", "about:buildconfig")
    ]}


def valid_url(raw: str) -> bool:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 4096 or any(ord(c) < 32 for c in raw):
        return False
    u = urlsplit(raw)
    return u.scheme.lower() in {"http", "https", "ftp", "file", "about", "mailto"} and (
        bool(u.netloc) if u.scheme.lower() in {"http", "https", "ftp"} else bool(u.path)
    )


def validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ValueError("Lesezeichen-Konfiguration benötigt schema=1.")
    folders, entries = raw.get("folders"), raw.get("entries")
    if not isinstance(folders, list) or len(folders) > 200 or not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("Maximal 200 Ordner und 1000 Lesezeichen.")
    revision = raw.get("defaults_revision", 0)
    if type(revision) is not int or not 0 <= revision <= 1000:
        raise ValueError("Ungültige Lesezeichen-Standardversion.")
    result: dict[str, Any] = {"schema": 1, "defaults_revision": revision, "folders": [], "entries": []}
    for name in folders:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
            raise ValueError("Ungültiger Lesezeichen-Ordnername.")
        name = name.strip()
        if name not in result["folders"]:
            result["folders"].append(name)
    ids: set[str] = set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("Ungültiger Lesezeicheneintrag.")
        rid = row.get("id")
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", rid) or rid in ids:
            raise ValueError("Doppelte oder ungültige Lesezeichen-ID.")
        ids.add(rid)
        title, url = row.get("title"), row.get("url")
        if not isinstance(title, str) or len(title) > 250 or any(ord(c) < 32 for c in title):
            raise ValueError("Ungültiger Lesezeichen-Titel.")
        if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
            raise ValueError("Ungültige Lesezeichen-URL.")
        placement = row.get("placement")
        folder = row.get("folder", "")
        if placement not in ("toolbar", "menu") or not isinstance(folder, str) or len(folder) > 120:
            raise ValueError("Lesezeichen-Ziel muss Leiste oder Menü sein; Ordner maximal 120 Zeichen.")
        if type(row.get("enabled")) is not bool:
            raise ValueError("Lesezeichen-Checkbox muss Boolean sein.")
        if row["enabled"] and (not title.strip() or not valid_url(url.strip())):
            raise ValueError(f"Aktives Lesezeichen '{title}': Titel und gültige URL erforderlich.")
        if folder and folder.strip() not in result["folders"]:
            result["folders"].append(folder.strip())
        result["entries"].append({"id": rid, "enabled": row["enabled"],
                                  "title": title, "url": url, "placement": placement,
                                  "folder": folder.strip()})
    return result



def upgrade_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """One-time upgrade of saved v0.0.22 defaults; never re-add a user-deleted link.

    Only add the new link to a surviving About Settings folder which still has
    its original about:config bookmark. Other folders and existing rows stay as
    the user saved them. The revision travels with the saved bookmarks JSON.
    """
    config = validate(config)
    if config["defaults_revision"] >= 2:
        return config
    entries = config["entries"]
    if ("About Settings" in config["folders"]
        and any(r["folder"] == "About Settings" and r["url"] == "about:config" for r in entries)
        and not any(r["url"] == "about:sync-log" for r in entries)):
        row = {"id": "about_sync-log", "enabled": True,
               "title": "about:sync-log", "url": "about:sync-log",
               "placement": "toolbar", "folder": "About Settings"}
        if any(r["id"] == row["id"] for r in entries):
            row["id"] = "about_sync_log_v23"
        pos = next(i for i, r in enumerate(entries) if r["folder"] == "About Settings")
        entries.insert(pos, row)
    config["defaults_revision"] = 2
    return validate(config)

def policy_entries(config: dict[str, Any]) -> list[dict[str, str]]:
    """Serialize editable bookmarks with a Firefox policies.json first-item guard.

    Some Firefox releases accept the first Bookmarks-policy item in Places but
    omit it from its intended toolbar/menu folder (Mozilla bug 1922186). Since
    the policies engine keeps only one item per URL, repeating the user's first
    link cannot help. Prefix a *distinct*, non-user-managed entry in the menu
    so the first user bookmark stays visible and first in its chosen folder.

    This is only present in the generated policies.json, never in the UI,
    saved config, or build snapshot. Empty selections must remain empty.
    """
    valid = validate(config)
    selected = [{**{"Title": row["title"].strip(), "URL": row["url"].strip(), "Placement": row["placement"]},
                 **({"Folder": row["folder"]} if row["folder"] else {})}
                for row in valid["entries"] if row["enabled"]]
    if not selected:
        return []

    # About:blank is a valid Firefox bookmark URL. It is not shown inside any
    # user-defined folder. A distinct URL is essential: Firefox de-duplicates
    # the Bookmarks policy by URL before inserting bookmarks.
    guard_url = "about:blank"
    if any(item["URL"].lower() == guard_url for item in selected):
        guard_url = "about:blank#firefox-webbrowser-build-policy-guard"
    return [{"Title": "Firefox Lesezeichen-Initialisierung",
             "URL": guard_url, "Placement": "menu"}, *selected]


class BookmarksHTML(HTMLParser):
    """Parse Firefox/Netscape bookmarks.html, with top-level toolbar/menu roots."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str] | None] = []
        self.pending: tuple[str, str] | None = None
        self.in_heading = False
        self.in_anchor = False
        self.parts: list[str] = []
        self.href = ""
        self.entries: list[dict[str, Any]] = []
        self.skipped = 0
        self.flattened = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h3':
            self.in_heading = True
            self.parts = []
            self.heading_attrs = attrs
        elif tag == 'dl':
            self.stack.append(self.pending)
            self.pending = None
        elif tag == 'a':
            self.in_anchor = True
            self.parts = []
            self.href = attrs.get('href', '')

    def handle_data(self, data):
        if self.in_heading or self.in_anchor:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'h3' and self.in_heading:
            name = ''.join(self.parts).strip()
            flag = self.heading_attrs.get('personal_toolbar_folder', '').lower()
            if flag == 'true' or name.lower() in {'bookmarks toolbar', 'lesezeichen-symbolleiste', 'lesezeichenleiste'}:
                self.pending = ('root', 'toolbar')
            elif name.lower() in {'bookmarks menu', 'lesezeichen-menü', 'lesezeichenmenü'}:
                self.pending = ('root', 'menu')
            elif name:
                self.pending = ('folder', name)
            self.in_heading = False
            self.parts = []
        elif tag == 'a' and self.in_anchor:
            title = ''.join(self.parts).strip() or self.href
            roots = [x[1] for x in self.stack if x and x[0] == 'root']
            folders = [x[1] for x in self.stack if x and x[0] == 'folder']
            # Only one folder depth is expressible in Firefox Bookmarks policy.
            folder = ' / '.join(folders)
            if len(folders) > 1:
                self.flattened += 1
            if title and valid_url(self.href):
                self.entries.append({"id": 'import_' + uuid.uuid4().hex, "enabled": True,
                                     "title": title[:250], "url": self.href,
                                     "placement": roots[-1] if roots else 'toolbar', "folder": folder[:120]})
            else:
                self.skipped += 1
            self.in_anchor = False
            self.parts = []
        elif tag == 'dl' and self.stack:
            self.stack.pop()


def import_content(filename: str, content: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(content, str) or len(content.encode('utf-8')) > 2_000_000:
        raise ValueError('Datei zu groß (maximal 2 MB).')
    if filename.lower().endswith(('.html', '.htm')):
        parser = BookmarksHTML()
        parser.feed(content)
        rows = parser.entries
        info = {"skipped": parser.skipped, "flattened": parser.flattened}
    elif filename.lower().endswith('.json'):
        try:
            doc = json.loads(content)
        except ValueError as exc:
            raise ValueError('Ungültige JSON-Datei.') from exc
        if not isinstance(doc, dict) or doc.get('schema') != 1 or 'entries' not in doc:
            raise ValueError('JSON-Import: Hier wird das JSON aus dieser Lesezeichen-Card erwartet. Firefox-JSONLZ4-Backups werden nicht unterstützt; bitte HTML exportieren.')
        rows = validate(doc)['entries']
        rows = [{**r, 'id': 'import_' + uuid.uuid4().hex} for r in rows]
        info = {"skipped": 0, "flattened": 0}
    else:
        raise ValueError('Bitte Firefox-Lesezeichen-HTML oder das JSON dieser Card importieren.')
    if not rows:
        raise ValueError('Keine unterstützten Lesezeichen gefunden.')
    if len(rows) > 1000:
        raise ValueError('Import enthält mehr als 1000 Lesezeichen.')
    return rows, info
