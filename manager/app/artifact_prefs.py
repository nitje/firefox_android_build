"""Repackage a Firefox Windows portable ZIP with editable default preferences.

No Mozilla source recompilation or omni.ja mutation. AutoConfig uses defaultPref,
so prefs are user-editable and apply to new/unchanged profiles. The supplied ZIP
must contain one top-level directory holding firefox.exe. EXEs are not ZIPs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

TRIGGER_NAME = "firefox-webbrowser-build.js"
CONFIG_NAME = "firefox-webbrowser-build.cfg"
PORTABLE_FILES = ("FirefoxPortable.cmd", "FirefoxPortable.vbs", "PORTABLE_README.txt")


def value_js(row: dict[str, Any]) -> str:
    value = row["value"]
    kind = row["type"]
    if kind == "Boolean":
        return "true" if value else "false"
    if kind == "Number":
        return str(value)
    if kind == "String":
        return json.dumps(value, ensure_ascii=False)
    raise ValueError(f"Unbekannter Pref-Typ: {kind}")


def autoconfig_files(selected: list[dict[str, Any]]) -> tuple[bytes, bytes]:
    # No arbitrary statements are allowed: all names/types validated by caller.
    trigger = ("// Firefox Webbrowser Build default preferences (editable in about:config)\n"
               f'pref("general.config.filename", "{CONFIG_NAME}");\n'
               'pref("general.config.obscure_value", 0);\n')
    cfg = ["// Firefox Webbrowser Build – dynamically configured defaults; do not lock preferences"]
    for row in selected:
        cfg.append(f'defaultPref({json.dumps(row["name"])}, {value_js(row)});')
    return trigger.encode("utf-8"), ("\n".join(cfg) + "\n").encode("utf-8")


# No compiled launcher or external runtime required. FirefoxPortable.cmd works even
# where Windows Script Host is blocked. FirefoxPortable.vbs is a console-free option.
PORTABLE_CMD = r"""@echo off
setlocal DisableDelayedExpansion
set "PORTABLE_ROOT=%~dp0"
set "PORTABLE_PROFILE=%PORTABLE_ROOT%Data\profile"
set "PORTABLE_LOCAL=%PORTABLE_ROOT%Data\local"
if not exist "%PORTABLE_ROOT%firefox.exe" (
  echo firefox.exe missing next to FirefoxPortable.cmd
  exit /b 1
)
if not exist "%PORTABLE_PROFILE%\" mkdir "%PORTABLE_PROFILE%" || exit /b 1
if not exist "%PORTABLE_LOCAL%\" mkdir "%PORTABLE_LOCAL%" || exit /b 1
rem Do not set or override Mozilla crash-reporter data/ping paths.
rem Remove this line to opt back into Mozilla's crash reporter.
set "MOZ_CRASHREPORTER_DISABLE=1"
start "" "%PORTABLE_ROOT%firefox.exe" -no-remote -profile "%PORTABLE_PROFILE%" %*
exit /b %errorlevel%
"""

PORTABLE_VBS = r"""Option Explicit
Dim shell, fs, root, profile, cache, launch, i, argument
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName) & "\"
profile = root & "Data\profile"
cache = root & "Data\local"
If Not fs.FileExists(root & "firefox.exe") Then
  MsgBox "firefox.exe fehlt im selben Ordner.", vbCritical, "Firefox Portable"
  WScript.Quit 1
End If
If Not fs.FolderExists(root & "Data") Then fs.CreateFolder root & "Data"
If Not fs.FolderExists(profile) Then fs.CreateFolder profile
If Not fs.FolderExists(cache) Then fs.CreateFolder cache
shell.Environment("PROCESS")("XRE_PROFILE_PATH") = profile
shell.Environment("PROCESS")("XRE_PROFILE_LOCAL_PATH") = cache
shell.Environment("PROCESS")("MOZ_CRASHREPORTER_DISABLE") = "1"
launch = Chr(34) & root & "firefox.exe" & Chr(34) & " -no-remote -profile " & Chr(34) & profile & Chr(34)
For i = 0 To WScript.Arguments.Count - 1
  argument = WScript.Arguments(i)
  launch = launch & " " & Chr(34) & Replace(argument, Chr(34), Chr(34) & Chr(34)) & Chr(34)
Next
shell.Run launch, 1, False
"""

PORTABLE_README = r"""Firefox Webbrowser Build - Windows Portable
=========================================

START: Doppelklick auf FirefoxPortable.cmd (keine Zusatzsoftware noetig).
Optional ohne Konsolenfenster: FirefoxPortable.vbs (benoetigt Windows Script Host).
Nicht firefox.exe direkt starten: das umgeht den Portable-Starter und kann das
normale Mozilla-Profil unter AppData verwenden.

Verzeichnisse relativ zum entpackten firefox-Ordner:
  Data\profile   Lesezeichen, Erweiterungen, Verlauf, persoenliche Einstellungen
  Data\local     Cache und temporaere profilbezogene Dateien

Beide Starter verwenden -no-remote und -profile sowie XRE_PROFILE_PATH /
XRE_PROFILE_LOCAL_PATH. Damit wird ein bereits installiertes Firefox-Profil
nicht absichtlich geoeffnet. Zum Umzug den gesamten entpackten Ordner kopieren.
Den Browser davor vollstaendig beenden. Vorhandene AppData-Profile werden nicht
automatisch importiert oder geloescht.

Automatic Firefox application updates are disabled via distribution/policies.json,
because upstream updates can replace this custom compiled browser. Extension
updates are not disabled. Update Firefox by downloading a newer self-built ZIP,
then keep/transfer the entire Data directory. Back up Data regularly.

WICHTIGE GRENZE: Unter Windows berechnet Mozilla den 'Update Folder' in
about:support unabhaengig vom Profil unter ProgramData. Dieser Pfad kann dort
trotz deaktivierter Selbstupdates erscheinen; allein ein ZIP-Starter kann ihn
nicht garantiert umleiten. Browser-/Windows-Integrationen, temporaere Dateien
oder Betriebssystemhistorie koennen ebenfalls Spuren ausserhalb von Data anlegen.
Das Paket ist deshalb ein portabler Starter mit lokalem Profil/Cache, keine
vollstaendig installations- oder spurenfreie Firefox-Quellcode-Variante.

MOZ_CRASHREPORTER_DISABLE=1 bleibt in beiden Startern gesetzt.
Die Variablen MOZ_CRASHREPORTER_DATA_DIRECTORY und
MOZ_CRASHREPORTER_PING_DIRECTORY werden wieder NICHT gesetzt oder umgeleitet.
Firefox kann daher in about:support weiterhin AppData-Pfade dafuer nennen.
Vorhandene Verzeichnisse werden nicht automatisch entfernt.
Die Build-Diagnose im Docker-Manager ist davon unabhaengig.
"""


def portable_policy(existing: bytes | None, bookmarks: dict[str, Any] | None = None) -> bytes:
    """Preserve any other bundled enterprise policies, but prevent self-update."""
    if existing is None:
        document: dict[str, Any] = {}
    else:
        try:
            document = json.loads(existing.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Vorhandene distribution/policies.json ist ungueltig; Portable-Update-Policy nicht blind ueberschreiben.') from exc
    if not isinstance(document, dict) or not isinstance(document.get('policies', {}), dict):
        raise ValueError('Vorhandene distribution/policies.json hat ungueltige Struktur.')
    document.setdefault('policies', {})['DisableAppUpdate'] = True
    if bookmarks is not None:
        from .bookmark_config import policy_entries
        document['policies']['Bookmarks'] = policy_entries(bookmarks)
    return (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def repackage(source: Path, target: Path, selected: list[dict[str, Any]],
              bookmarks: dict[str, Any] | None = None) -> dict[str, Any]:
    """Atomically publish a checked ZIP. Existing target is never truncated on error."""
    source, target = Path(source), Path(target)
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise ValueError("about:config-Nachbearbeitung erfordert ein vorhandenes Firefox-ZIP (kein Installer-EXE).")
    trigger, cfg = autoconfig_files(selected)
    temp_name = None
    try:
        with zipfile.ZipFile(source, "r") as zin:
            if zin.testzip() is not None:
                raise ValueError("Firefox-ZIP ist beschädigt")
            members = zin.infolist()
            candidates = [m.filename for m in members if re.fullmatch(r"[^/]+/firefox\.exe", m.filename, re.I)]
            if len(candidates) != 1:
                raise ValueError("ZIP muss genau einen Firefox-Programmordner mit firefox.exe enthalten")
            root = candidates[0].rsplit("/", 1)[0]
            trigger_path = f"{root}/defaults/pref/{TRIGGER_NAME}"
            config_path = f"{root}/{CONFIG_NAME}"
            policy_path = f"{root}/distribution/policies.json"
            extra_paths = {name: f"{root}/{name}" for name in PORTABLE_FILES}
            blocked = {trigger_path.lower(), config_path.lower(), policy_path.lower(),
                       *(path.lower() for path in extra_paths.values())}
            old_policy = next((zin.read(m) for m in members if m.filename.lower() == policy_path.lower()), None)
            portable_policies = portable_policy(old_policy, bookmarks)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".firefox-prefs-", suffix=".zip", dir=target.parent, delete=False) as fh:
                temp_name = fh.name
            with zipfile.ZipFile(temp_name, "w", allowZip64=True) as zout:
                zout.comment = zin.comment
                copied = set()
                for info in members:
                    name = info.filename
                    if name.lower() in blocked:
                        continue
                    if name in copied:
                        raise ValueError(f"Doppelter ZIP-Eintrag: {name}")
                    copied.add(name)
                    if info.is_dir():
                        zout.writestr(info, b"")
                    else:
                        # Stream content to avoid reading hundreds of MiB into RAM.
                        with zin.open(info, "r") as inp, zout.open(info, "w", force_zip64=True) as out:
                            shutil.copyfileobj(inp, out, length=1024*1024)
                zout.writestr(trigger_path, trigger, compress_type=zipfile.ZIP_DEFLATED)
                zout.writestr(config_path, cfg, compress_type=zipfile.ZIP_DEFLATED)
                zout.writestr(policy_path, portable_policies, compress_type=zipfile.ZIP_DEFLATED)
                for name, payload in (("FirefoxPortable.cmd", PORTABLE_CMD),
                                      ("FirefoxPortable.vbs", PORTABLE_VBS),
                                      ("PORTABLE_README.txt", PORTABLE_README)):
                    zout.writestr(extra_paths[name], payload.encode('ascii') if name.endswith('.vbs') else payload.encode('utf-8'),
                                  compress_type=zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(temp_name) as check:
            if check.testzip() is not None:
                raise ValueError("Neu gepacktes Firefox-ZIP ist beschädigt")
            if (check.read(config_path) != cfg or check.read(trigger_path) != trigger
                    or check.read(policy_path) != portable_policies
                    or any(check.read(extra_paths[n]) == b'' for n in PORTABLE_FILES)):
                raise ValueError("about:config-Dateien fehlen nach dem Neupacken")
        os.replace(temp_name, target)
        temp_name = None
        return {"artifact_name": target.name, "config_file": config_path, "selected_count": len(selected), "portable": True}
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
