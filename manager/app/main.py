from __future__ import annotations

import json
import hashlib
import tempfile
import zipfile
import os
import io
import secrets
import base64
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
import paramiko
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from .artifact_prefs import repackage
from .bookmark_config import starter_config, validate as validate_bookmarks, upgrade_defaults as upgrade_bookmark_defaults, import_content as import_bookmarks_content

APP_VERSION = "0.0.16"
PROJECT_PLATFORM = os.getenv("PROJECT_PLATFORM", "android-arm64").strip()
if PROJECT_PLATFORM not in {"windows-x64", "android-arm64"}:
    raise RuntimeError(f"Ungültiges PROJECT_PLATFORM: {PROJECT_PLATFORM}")
PROJECT_TITLE = "Firefox Windows Build" if PROJECT_PLATFORM == "windows-x64" else "Firefox Android Build"
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "database" / "manager.sqlite3"
CONFIG_DIR = DATA_DIR / "config"
UI_CONFIG_PATH = CONFIG_DIR / "ui_config.json"
SHARED_DIR = DATA_DIR / "shared"
ANDROID_QUEUE_DIR = SHARED_DIR / "android"
ANDROID_WORKER_STATUS = SHARED_DIR / "android_worker_status.json"
ANDROID_WORKER_OEM_DIR = Path(os.getenv("ANDROID_WORKER_OEM_DIR", "/android_worker_oem"))
ANDROID_WORKER_UPDATES = ANDROID_QUEUE_DIR / "updates"
ANDROID_SIGNING_DIR = DATA_DIR / "android_signing"
ANDROID_SIGNING_KEYSTORE = ANDROID_SIGNING_DIR / "release.p12"
ANDROID_SIGNING_SECRET = ANDROID_SIGNING_DIR / "secret.json"
ANDROID_SIGNING_INFO = ANDROID_SIGNING_DIR / "signing-info.json"
ANDROID_ABOUT_CONFIG_PATH = CONFIG_DIR / "android_about_config.json"
ABOUT_CONFIG_PATH = CONFIG_DIR / "about_config.json"
BOOKMARKS_PATH = CONFIG_DIR / "bookmarks.json"
DEFAULT_ABOUT_CONFIG_PATH = APP_DIR / "default_about_config.json"
PREFS_DIR = SHARED_DIR / "prefs"
STATUS_DIR = SHARED_DIR / "status"
LOGS_DIR = SHARED_DIR / "logs"
ARTIFACTS_DIR = SHARED_DIR / "artifacts"
DIAGNOSTICS_DIR = SHARED_DIR / "diagnostics"
WORKER_HOST = os.getenv("WORKER_HOST", "firefox_windows_builder")
WORKER_SSH_PORT = int(os.getenv("WORKER_SSH_PORT", "22"))
WORKER_USER = os.getenv("WORKER_USER", "Builder")
WORKER_PASSWORD = os.getenv("WORKER_PASSWORD", "")
WORKER_OEM_DIR = Path(os.getenv("WORKER_OEM_DIR", "/worker_oem"))
FIREFOX_VERSIONS_URL = "https://product-details.mozilla.org/1.0/firefox_versions.json"
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")

COMMON_DATA_DIRS = (DB_PATH.parent, CONFIG_DIR, PREFS_DIR, STATUS_DIR, LOGS_DIR, ARTIFACTS_DIR, DIAGNOSTICS_DIR)
PLATFORM_DATA_DIRS = ((ANDROID_QUEUE_DIR / "jobs", ANDROID_QUEUE_DIR / "processing", ANDROID_QUEUE_DIR / "failed", ANDROID_SIGNING_DIR)
                      if PROJECT_PLATFORM == "android-arm64" else ())
for folder in (*COMMON_DATA_DIRS, *PLATFORM_DATA_DIRS):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=PROJECT_TITLE, version=APP_VERSION)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

DEFAULT_UI_CONFIG = {
    "schema": 1,
    "cards": {
        "build": {"open": True, "mode": "manual"},
        "worker": {"open": True, "mode": "auto"},
        "android-build": {"open": False, "mode": "manual"},
        "android-worker": {"open": False, "mode": "manual"},
        "queue": {"open": True, "mode": "auto"},
        "versions": {"open": True, "mode": "manual"},
        "log": {"open": False, "mode": "manual"},
        "about-config": {"open": False, "mode": "manual"},
        "android-about-config": {"open": False, "mode": "manual"},
        "bookmarks": {"open": False, "mode": "manual"},
    },
}
_ui_lock = threading.Lock()
_latest_cache: dict[str, Any] = {"time": 0.0, "value": None}
_auto_worker_update_lock = threading.Lock()
_auto_worker_update_state: dict[str, Any] = {
    "running": False,
    "last_attempt": 0.0,
    "last_error": None,
    "last_result": None,
}
_status_sync_lock = threading.Lock()
_requeue_lock = threading.Lock()

class BuildRequest(BaseModel):
    version: str = Field(pattern=r"^\d+(?:\.\d+){1,3}$")
    platform: str = "windows-x64"
    nightly: bool = False
    about_config: list[dict[str, Any]] | None = None
    about_config_full: dict[str, Any] | None = None
    bookmarks: dict[str, Any] | None = None
    android_build_type: str | None = None
    android_about_config: dict[str, Any] | None = None

class UiConfigPatch(BaseModel):
    cards: dict[str, dict[str, Any]] = Field(default_factory=dict)

class SigningBackupRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256)


def atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600, owner_uid: int | None = None, owner_gid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_bytes(payload)
    os.chmod(tmp, mode)
    if owner_uid is not None and owner_gid is not None:
        try:
            os.chown(tmp, owner_uid, owner_gid)
        except PermissionError:
            pass
    os.replace(tmp, path)


def _fingerprint(cert: x509.Certificate) -> str:
    raw = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(raw[i:i+2] for i in range(0, len(raw), 2))


def _signing_material() -> tuple[bytes, dict[str, Any], x509.Certificate]:
    if not ANDROID_SIGNING_KEYSTORE.is_file() or not ANDROID_SIGNING_SECRET.is_file():
        raise FileNotFoundError("Kein Android-Release-Keystore vorhanden.")
    secret = json.loads(ANDROID_SIGNING_SECRET.read_text(encoding="utf-8"))
    password = secret.get("store_password")
    alias = secret.get("alias")
    if not isinstance(password, str) or not password or not isinstance(alias, str) or not alias:
        raise ValueError("Android-Signing-Metadaten sind unvollständig.")
    blob = ANDROID_SIGNING_KEYSTORE.read_bytes()
    key, cert, _cas = pkcs12.load_key_and_certificates(blob, password.encode("utf-8"))
    if key is None or cert is None:
        raise ValueError("PKCS#12 enthält keinen privaten Schlüssel mit Zertifikat.")
    return blob, secret, cert


def android_signing_status() -> dict[str, Any]:
    present = any(p.is_file() for p in (ANDROID_SIGNING_KEYSTORE, ANDROID_SIGNING_SECRET, ANDROID_SIGNING_INFO))
    status: dict[str, Any] = {"exists": present, "valid": False, "alias": None, "fingerprint_sha256": None}
    if present and (not ANDROID_SIGNING_KEYSTORE.is_file() or not ANDROID_SIGNING_SECRET.is_file()):
        status["error"] = "Signing-Dateien sind unvollständig; vorhandenen Key nicht automatisch überschreiben. Gültiges Backup importieren."
        return status
    try:
        _blob, secret, cert = _signing_material()
        info = {}
        try:
            info = json.loads(ANDROID_SIGNING_INFO.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        status.update({
            "exists": True, "valid": True, "alias": secret.get("alias"),
            "fingerprint_sha256": _fingerprint(cert),
            "created_at": info.get("created_at"),
            "not_after": cert.not_valid_after_utc.isoformat(),
            "subject": cert.subject.rfc4514_string(),
        })
    except FileNotFoundError:
        pass
    except Exception as exc:
        status.update({"exists": True, "valid": False, "error": str(exc)})
    return status


def generate_android_signing_key() -> dict[str, Any]:
    current = android_signing_status()
    if current.get("exists"):
        if current.get("valid"):
            return current
        raise ValueError("Es existieren bereits ungültige Signing-Dateien. Erst durch einen gültigen Import ersetzen.")
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    now = datetime.now(timezone.utc)
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Firefox Webbrowser Build"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Firefox Android Local Release"),
    ])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365 * 40))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    alias = "firefox-release"
    internal_password = secrets.token_urlsafe(48)
    blob = pkcs12.serialize_key_and_certificates(
        alias.encode("utf-8"), key, cert, None,
        serialization.BestAvailableEncryption(internal_password.encode("utf-8")))
    info = {
        "schema": 1, "alias": alias, "created_at": now.isoformat(),
        "fingerprint_sha256": _fingerprint(cert), "format": "PKCS12",
    }
    secret = {"schema": 1, "alias": alias, "store_password": internal_password}
    # UID/GID 1000 is the unprivileged Android build identity. Files remain 0600.
    atomic_bytes(ANDROID_SIGNING_KEYSTORE, blob, owner_uid=1000, owner_gid=1000)
    atomic_bytes(ANDROID_SIGNING_SECRET, json.dumps(secret, ensure_ascii=False, indent=2).encode("utf-8"), owner_uid=1000, owner_gid=1000)
    atomic_bytes(ANDROID_SIGNING_INFO, json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"), mode=0o644, owner_uid=1000, owner_gid=1000)
    return android_signing_status()


def _backup_key(password: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000).derive(password.encode("utf-8"))


def export_android_signing_backup(password: str) -> bytes:
    blob, secret, cert = _signing_material()
    status = android_signing_status()
    info = {
        "schema": 1, "alias": secret["alias"],
        "fingerprint_sha256": _fingerprint(cert),
        "created_at": status.get("created_at"), "format": "PKCS12",
        "exported_at": now_iso(), "project_version": APP_VERSION,
    }
    payload = json.dumps({
        "schema": 1,
        "release_p12_b64": base64.b64encode(blob).decode("ascii"),
        "secret": secret,
        "info": info,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_backup_key(password, salt)).encrypt(nonce, payload, b"FirefoxAndroidSigningBackup-v1")
    envelope = json.dumps({
        "schema": 1, "kdf": "PBKDF2-HMAC-SHA256", "iterations": 600000, "cipher": "AES-256-GCM",
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }, separators=(",", ":")).encode("utf-8")
    readme = (
        "Firefox Android Release-Signing Backup\n"
        "======================================\n\n"
        "Die private Signing-Sicherung in backup.enc ist mit dem beim Export\n"
        "angegebenen Passwort via PBKDF2-HMAC-SHA256 und AES-256-GCM verschluesselt.\n"
        "Bewahre Backup-Datei und Passwort getrennt und sicher auf.\n"
        "Ohne denselben Release-Key koennen bereits installierte, damit signierte APKs\n"
        "nicht durch kuenftige Builds derselben App-Signatur aktualisiert werden.\n"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.enc", envelope)
        zf.writestr("README.txt", readme.encode("utf-8"))
    return out.getvalue()


def import_android_signing_backup(payload: bytes, password: str, *, replace: bool) -> dict[str, Any]:
    if not payload or len(payload) > 5 * 1024 * 1024:
        raise ValueError("Signing-Backup ist leer oder groesser als 5 MiB.")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
            names = set(zf.namelist())
            if "backup.enc" not in names:
                raise ValueError("Kein gueltiges Firefox-Android-Signing-Backup.")
            envelope = json.loads(zf.read("backup.enc").decode("utf-8"))
        if envelope.get("schema") != 1 or envelope.get("iterations") != 600000:
            raise ValueError("Nicht unterstuetzte Signing-Backup-Version.")
        salt = base64.b64decode(envelope["salt_b64"], validate=True)
        nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext_b64"], validate=True)
        clear = AESGCM(_backup_key(password, salt)).decrypt(nonce, ciphertext, b"FirefoxAndroidSigningBackup-v1")
        bundle = json.loads(clear.decode("utf-8"))
        blob = base64.b64decode(bundle["release_p12_b64"], validate=True)
        secret = bundle["secret"]
        info = bundle["info"]
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup-Passwort falsch oder Signing-Backup beschaedigt.") from exc
    except Exception as exc:
        # AES-GCM authentication failures intentionally do not reveal whether the
        # password or ciphertext was wrong.
        raise ValueError("Backup-Passwort falsch oder Signing-Backup beschaedigt.") from exc
    store_password, alias = secret.get("store_password"), secret.get("alias")
    if not isinstance(store_password, str) or not store_password or not isinstance(alias, str) or not alias:
        raise ValueError("Signing-Backup enthaelt ungueltige interne Metadaten.")
    key, cert, _cas = pkcs12.load_key_and_certificates(blob, store_password.encode("utf-8"))
    if key is None or cert is None:
        raise ValueError("Signing-Backup enthaelt keinen verwendbaren privaten Schluessel.")
    imported_fp = _fingerprint(cert)
    if info.get("fingerprint_sha256") and info.get("fingerprint_sha256") != imported_fp:
        raise ValueError("Fingerprint im Signing-Backup stimmt nicht mit dem Zertifikat ueberein.")
    current = android_signing_status()
    if current.get("exists") and current.get("valid") and current.get("fingerprint_sha256") != imported_fp and not replace:
        raise PermissionError(
            f"Es existiert bereits ein anderer Release-Keystore ({current.get('fingerprint_sha256')}). "
            f"Importierter Fingerprint: {imported_fp}. Zum Ersetzen die Option ausdruecklich aktivieren.")
    clean_info = {
        "schema": 1, "alias": alias, "created_at": info.get("created_at") or now_iso(),
        "fingerprint_sha256": imported_fp, "format": "PKCS12",
        "imported_at": now_iso(),
    }
    clean_secret = {"schema": 1, "alias": alias, "store_password": store_password}
    atomic_bytes(ANDROID_SIGNING_KEYSTORE, blob, owner_uid=1000, owner_gid=1000)
    atomic_bytes(ANDROID_SIGNING_SECRET, json.dumps(clean_secret, ensure_ascii=False, indent=2).encode("utf-8"), owner_uid=1000, owner_gid=1000)
    atomic_bytes(ANDROID_SIGNING_INFO, json.dumps(clean_info, ensure_ascii=False, indent=2).encode("utf-8"), mode=0o644, owner_uid=1000, owner_gid=1000)
    return android_signing_status()



# Server-managed, browser-independent build preferences. Each submitted build takes an
# immutable snapshot; changing the UI later cannot alter a queued job or a retry.
_about_lock = threading.Lock()
_android_about_lock = threading.Lock()
_bookmarks_lock = threading.Lock()
PREF_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")


DEFAULT_ANDROID_ABOUT_CONFIG = {
    "schema": 1,
    "sync_server": {
        "account_server": {"enabled": False, "value": "https://accounts.firefox.com"},
        "sync_server": {"enabled": False, "value": "https://token.services.mozilla.com/1.0/sync/1.5"},
    },
}


def _validate_android_url(label: str, enabled: bool, value: Any) -> dict[str, Any]:
    if type(enabled) is not bool:
        raise ValueError(f"{label}: Checkbox-Wert muss Boolean sein.")
    if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
        raise ValueError(f"{label}: ungültige URL.")
    value = value.strip()
    if enabled:
        from urllib.parse import urlsplit
        if any(ch.isspace() for ch in value):
            raise ValueError(f"{label}: aktivierter Wert benötigt eine gültige http(s)-URL ohne Zugangsdaten.")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"{label}: aktivierter Wert benötigt eine gültige http(s)-URL ohne Zugangsdaten.")
    return {"enabled": enabled, "value": value}


def validate_android_about_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = DEFAULT_ANDROID_ABOUT_CONFIG
    if not isinstance(raw, dict):
        raise ValueError("Android-about:config muss ein Objekt sein.")
    sync = raw.get("sync_server")
    if not isinstance(sync, dict):
        raise ValueError("Android-about:config benötigt den Bereich Sync Server.")
    account = sync.get("account_server")
    token = sync.get("sync_server")
    if not isinstance(account, dict) or not isinstance(token, dict):
        raise ValueError("Android Sync Server enthält ungültige Einträge.")
    return {
        "schema": 1,
        "sync_server": {
            "account_server": _validate_android_url(
                "Benutzerdefinierter Mozilla-Kontoserver", account.get("enabled"), account.get("value")
            ),
            "sync_server": _validate_android_url(
                "Benutzerdefinierter Sync-Server", token.get("enabled"), token.get("value")
            ),
        },
    }


def load_android_about_config() -> dict[str, Any]:
    with _android_about_lock:
        try:
            return validate_android_about_config(json.loads(ANDROID_ABOUT_CONFIG_PATH.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return validate_android_about_config(json.loads(json.dumps(DEFAULT_ANDROID_ABOUT_CONFIG)))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=f"android_about_config.json ungültig: {exc}") from exc


def save_android_about_config(raw: Any) -> dict[str, Any]:
    try:
        validated = validate_android_about_config(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _android_about_lock:
        atomic_json_write(ANDROID_ABOUT_CONFIG_PATH, validated)
    return validated

def validate_selected_prefs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 300:
        raise ValueError("Maximal 300 about:config-Einträge erlaubt.")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Ungültiger about:config-Eintrag.")
        if not item.get("enabled", False):
            continue
        name = item.get("name", "")
        kind = item.get("type")
        value = item.get("value")
        if not isinstance(name, str) or not PREF_KEY_RE.fullmatch(name):
            raise ValueError(f"Ungültiger Firefox-Pref-Name: {name!r}")
        if name in seen:
            raise ValueError(f"Doppelt aktivierter Firefox-Pref: {name}")
        seen.add(name)
        if kind == "Boolean":
            if type(value) is not bool:
                raise ValueError(f"{name}: Boolean muss true/false sein.")
        elif kind == "Number":
            if type(value) is not int or not -(2**31) <= value <= 2**31 - 1:
                raise ValueError(f"{name}: Number benötigt eine 32-bit-Ganzzahl.")
        elif kind == "String":
            if not isinstance(value, str) or len(value) > 8192 or "\x00" in value:
                raise ValueError(f"{name}: ungültiger String (maximal 8192 Zeichen).")
        else:
            raise ValueError(f"{name}: Typ muss Boolean, Number oder String sein.")
        result.append({"enabled": True, "name": name, "type": kind, "value": value})
    return result


def validate_about_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("categories"), list):
        raise ValueError("about:config benötigt eine Kategorien-Liste.")
    cats = raw["categories"]
    if len(cats) > 40:
        raise ValueError("Maximal 40 Kategorien.")
    cleaned = []
    used_categories = set()
    used_ids = set()
    all_rows = []
    for category in cats:
        if not isinstance(category, dict):
            raise ValueError("Ungültige Kategorie.")
        cid = category.get("id")
        title = category.get("title")
        rows = category.get("rows")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", cid) or cid in used_categories:
            raise ValueError("Ungültige oder doppelte Kategorie-ID.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError("Ungültiger Kategorietitel.")
        if not isinstance(rows, list) or len(rows) > 300:
            raise ValueError("Ungültige Eintragsliste.")
        used_categories.add(cid)
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Ungültiger Pref-Eintrag.")
            rid = row.get("id")
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", rid) or rid in used_ids:
                raise ValueError("Ungültige oder doppelte Pref-ID.")
            used_ids.add(rid)
            if type(row.get("enabled")) is not bool:
                raise ValueError("Checkbox-Wert muss Boolean sein.")
            name, kind, value = row.get("name"), row.get("type"), row.get("value")
            if not isinstance(name, str) or len(name) > 200:
                raise ValueError("Ungültiger Pref-Name.")
            if kind not in ("Boolean", "Number", "String"):
                raise ValueError("Ungültiger Pref-Typ.")
            if kind == "Boolean" and type(value) is not bool:
                raise ValueError(f"{name}: Boolean falsch.")
            if kind == "Number" and (type(value) is not int or not -(2**31) <= value <= 2**31 - 1):
                raise ValueError(f"{name}: Number benötigt eine 32-bit-Ganzzahl.")
            if kind == "String" and (not isinstance(value, str) or len(value) > 8192 or "\x00" in value):
                raise ValueError(f"{name}: String ungültig.")
            note = row.get("note", "")
            if not isinstance(note, str) or len(note) > 1000:
                raise ValueError("Hinweis zu lang.")
            entries.append({"id": rid, "enabled": row["enabled"], "name": name, "type": kind, "value": value, "note": note})
            all_rows.append(entries[-1])
        cleaned.append({"id": cid, "title": title.strip(), "rows": entries, "open": bool(category.get("open", False))})
    if len(all_rows) > 300:
        raise ValueError("Maximal 300 Einträge insgesamt.")
    validate_selected_prefs(all_rows)
    return {"schema": 1, "categories": cleaned}


# These v0.0.18 shipped rows have been explicitly withdrawn by the user.
# Remove only those bundled entries, not user-created rows in other categories.
RETIRED_DEFAULT_PREFS_BY_CATEGORY = {
    "normandy": {"services.sync.prefs.sync.app.shield.optoutstudies.enabled"},
    "sponsored": {
        "services.sync.prefs.sync.browser.newtabpage.activity-stream.feeds.section.topstories",
        "browser.newtabpage.activity-stream.feeds.system.topstories",
    },
}


def merge_default_prefs(config: dict[str, Any], *, historical: bool = False) -> dict[str, Any]:
    """Move v0.0.19 homepage prefs into sponsored without resetting UI state.

    The persisted global config and per-build edit snapshots may both contain
    the old homepage category. Transfer all rows (including custom rows) before
    removing that category; never silently erase user-created entries.
    Completed ZIP artifacts are not changed by merely opening their editor.
    """
    template = json.loads(DEFAULT_ABOUT_CONFIG_PATH.read_text(encoding="utf-8"))
    for category in config["categories"]:
        retired = RETIRED_DEFAULT_PREFS_BY_CATEGORY.get(category["id"], set())
        if retired:
            category["rows"] = [row for row in category["rows"]
                                if row["name"] not in retired]

    # The v0.0.19 two built-in explanations have been withdrawn. Preserve
    # user-edited notes instead of blanking every row unconditionally.
    deprecated_notes = {
        "browser.newtabpage.activity-stream.feeds.section.topstories":
            "Empfohlene Artikel auf der Firefox-Startseite ausblenden (nicht nur gesponserte Artikel).",
        "browser.newtabpage.activity-stream.showWeather":
            "Wetter / Weather auf der Firefox-Startseite und in neuen Tabs ausblenden.",
    }
    for category in config["categories"]:
        if category["id"] in ("sponsored", "homepage"):
            for row in category["rows"]:
                if row["name"] in deprecated_notes and row.get("note", "") == deprecated_notes[row["name"]]:
                    row["note"] = ""

    categories = {cat["id"]: cat for cat in config["categories"]}
    homepage = categories.get("homepage")
    if homepage is not None:
        sponsored = categories.get("sponsored")
        if sponsored is None:
            sponsor_default = next(c for c in template["categories"] if c["id"] == "sponsored")
            sponsored = {"id": "sponsored", "title": sponsor_default["title"],
                         "open": bool(homepage.get("open", False)), "rows": []}
            index = config["categories"].index(homepage)
            config["categories"].insert(index, sponsored)
            categories["sponsored"] = sponsored
        present = {row["name"] for c in config["categories"] if c is not homepage
                   for row in c["rows"] if row["name"]}
        for row in homepage["rows"]:
            # Prefer the already stored row when a pref was independently added
            # elsewhere; otherwise retain every field/checkbox and stable ID.
            if row["name"] and row["name"] in present:
                continue
            if row["name"]:
                present.add(row["name"])
            sponsored["rows"].append(row)
        config["categories"].remove(homepage)
        del categories["homepage"]

    names = {row["name"] for cat in config["categories"] for row in cat["rows"]}
    for category_id, insert_before in (
        ("media_compat", ("webrtc", "fingerprinting")),
        ("sync_server", ("webrtc", "fingerprinting")),
        ("normandy", ("crash",)),
        ("sponsored", ("network",)),
    ):
        source = next(cat for cat in template["categories"] if cat["id"] == category_id)
        if category_id in ("media_compat", "sync_server", "sponsored"):
            candidates = source["rows"] if category_id in ("media_compat", "sync_server") else [
                row for row in source["rows"] if row["name"] in (
                    "browser.newtabpage.activity-stream.feeds.section.topstories",
                    "browser.newtabpage.activity-stream.showWeather",
                    "browser.newtabpage.activity-stream.feeds.topsites",
                    "browser.partnerlink.attributionURL",
                    "browser.partnerlink.campaign.topsites",
                )]
        else:
            candidates = []  # Withdrawn Sync override must not be recreated.
        target = categories.get(category_id)
        if target is None and candidates:
            target = {"id": source["id"], "title": source["title"],
                      "open": bool(source.get("open", False)), "rows": []}
            if category_id == "sync_server" and "media_compat" in categories:
                index = config["categories"].index(categories["media_compat"]) + 1
            else:
                index = next((i for i, cat in enumerate(config["categories"])
                              if cat["id"] in insert_before), len(config["categories"]))
            config["categories"].insert(index, target)
            categories[category_id] = target
        if target is None:
            continue
        for template_row in candidates:
            name = template_row["name"]
            if name in names:
                continue
            row = dict(template_row)
            if historical:
                row["enabled"] = False
            source_index = next(i for i, r in enumerate(source["rows"]) if r["name"] == name)
            preceding = [r["name"] for r in source["rows"][:source_index]]
            position = next((i + 1 for previous in reversed(preceding)
                             for i, existing in enumerate(target["rows"])
                             if existing["name"] == previous), len(target["rows"]))
            target["rows"].insert(position, row)
            names.add(name)
    return config


def load_about_config() -> dict[str, Any]:
    with _about_lock:
        try:
            original = json.loads(ABOUT_CONFIG_PATH.read_text(encoding="utf-8"))
            migrated = validate_about_config(merge_default_prefs(original))
            # Persist the migration immediately so withdrawn defaults cannot
            # reappear after a browser or container restart without a UI edit.
            if migrated != original:
                atomic_json_write(ABOUT_CONFIG_PATH, migrated)
            return migrated
        except FileNotFoundError:
            return validate_about_config(json.loads(DEFAULT_ABOUT_CONFIG_PATH.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=f"about_config.json ungültig: {exc}") from exc


def save_about_config(raw: Any) -> dict[str, Any]:
    try:
        validated = validate_about_config(merge_default_prefs(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _about_lock:
        tmp = ABOUT_CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ABOUT_CONFIG_PATH)
    return validated


def atomic_json_write(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def build_about_config(build_id: str) -> tuple[dict[str, Any], bool]:
    """Full snapshot if present; v0.0.14 snapshots only kept selected prefs."""
    full_path = PREFS_DIR / f"{build_id}.full.json"
    if full_path.is_file():
        return validate_about_config(merge_default_prefs(json.loads(full_path.read_text(encoding='utf-8')), historical=True)), True
    # Do NOT load current global user preferences, which can change between
    # builds. Reconstruct the known defaults and overlay selected historical
    # values. Disabled customizations before v0.0.15 were not recorded.
    config = validate_about_config(json.loads(DEFAULT_ABOUT_CONFIG_PATH.read_text(encoding='utf-8')))
    selected = read_build_prefs(build_id)
    by_name = {row['name']: row for cat in config['categories'] for row in cat['rows']}
    for row in (r for cat in config['categories'] for r in cat['rows']):
        row['enabled'] = False
    imported = None
    # Legacy v0.0.14 selected-only snapshots had no category information.
    # Do not resurrect the three withdrawn v0.0.18 builtins as imports.
    withdrawn = set().union(*RETIRED_DEFAULT_PREFS_BY_CATEGORY.values())
    for item in selected:
        if item['name'] in withdrawn:
            continue
        existing = by_name.get(item['name'])
        if existing is not None:
            existing.update({k: item[k] for k in ('enabled', 'name', 'type', 'value')})
        else:
            if imported is None:
                imported = {'id': 'imported', 'title': 'Frühere eigene Einträge', 'rows': [], 'open': True}
                config['categories'].append(imported)
            imported['rows'].append({'id': str(uuid.uuid4()), 'note': '', **item})
    return validate_about_config(config), False


def read_build_prefs(build_id: str) -> list[dict[str, Any]]:
    path = PREFS_DIR / f"{build_id}.json"
    if not path.is_file():
        return []  # Legacy jobs from versions <= 0.0.13
    return validate_selected_prefs(json.loads(path.read_text(encoding="utf-8")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Migrate 0.0.11 in place: retain all old jobs/artifacts as legacy branding."""
    with closing(db()) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='builds'").fetchone()
        if exists:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(builds)")}
            if "flavor" not in columns:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("ALTER TABLE builds RENAME TO builds_legacy_v0011")
                    conn.execute("""CREATE TABLE builds (
                       id TEXT PRIMARY KEY, version TEXT NOT NULL, platform TEXT NOT NULL,
                       flavor TEXT NOT NULL DEFAULT 'release', status TEXT NOT NULL,
                       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                       artifact_name TEXT, artifact_path TEXT, error TEXT,
                       cleanup_job_id TEXT,
                       UNIQUE(version,platform,flavor)
                    )""")
                    conn.execute("""INSERT INTO builds
                      (id,version,platform,flavor,status,created_at,updated_at,artifact_name,artifact_path,error)
                      SELECT id,version,platform,'legacy',status,created_at,updated_at,artifact_name,artifact_path,error
                      FROM builds_legacy_v0011""")
                    conn.execute("DROP TABLE builds_legacy_v0011")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            elif "cleanup_job_id" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN cleanup_job_id TEXT")
                conn.commit()
        else:
            conn.executescript("""CREATE TABLE builds (
                  id TEXT PRIMARY KEY, version TEXT NOT NULL, platform TEXT NOT NULL,
                  flavor TEXT NOT NULL DEFAULT 'release', status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  artifact_name TEXT, artifact_path TEXT, error TEXT,
                  cleanup_job_id TEXT,
                  UNIQUE(version, platform, flavor)
                );""")
            conn.commit()

    with closing(db()) as conn:
        columns = {c[1] for c in conn.execute("PRAGMA table_info(builds)")}
        if 'prefs_delivery' not in columns:
            conn.execute("ALTER TABLE builds ADD COLUMN prefs_delivery TEXT")
            conn.commit()


def load_ui_config() -> dict[str, Any]:
    with _ui_lock:
        cfg = json.loads(json.dumps(DEFAULT_UI_CONFIG))
        if UI_CONFIG_PATH.is_file():
            try:
                stored = json.loads(UI_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(stored, dict) and isinstance(stored.get("cards"), dict):
                    for key, value in stored["cards"].items():
                        if key in cfg["cards"] and isinstance(value, dict):
                            cfg["cards"][key].update({k: v for k, v in value.items() if k in {"open", "mode"}})
            except Exception:
                pass
        return cfg


def save_ui_config(patch: dict[str, dict[str, Any]]) -> dict[str, Any]:
    with _ui_lock:
        cfg = json.loads(json.dumps(DEFAULT_UI_CONFIG))
        if UI_CONFIG_PATH.is_file():
            try:
                old = json.loads(UI_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(old, dict) and isinstance(old.get("cards"), dict):
                    for key, value in old["cards"].items():
                        if key in cfg["cards"] and isinstance(value, dict):
                            cfg["cards"][key].update(value)
            except Exception:
                pass
        for key, value in patch.items():
            if key not in cfg["cards"] or not isinstance(value, dict):
                continue
            if "open" in value:
                cfg["cards"][key]["open"] = bool(value["open"])
            if value.get("mode") in {"auto", "manual", "pinned-open"}:
                cfg["cards"][key]["mode"] = value["mode"]
        tmp = UI_CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(UI_CONFIG_PATH)
        return cfg


async def latest_firefox(force: bool = False) -> dict[str, str]:
    if not force and _latest_cache["value"] and time.time() - _latest_cache["time"] < 300:
        return _latest_cache["value"]
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(FIREFOX_VERSIONS_URL)
        response.raise_for_status()
        data = response.json()
    result = {
        "version": str(data["LATEST_FIREFOX_VERSION"]),
        "esr": str(data.get("FIREFOX_ESR") or ""),
        "source": "Mozilla Product Details",
        "source_url": FIREFOX_VERSIONS_URL,
    }
    _latest_cache.update(time=time.time(), value=result)
    return result


def source_url(version: str) -> str:
    return f"https://archive.mozilla.org/pub/firefox/releases/{version}/source/firefox-{version}.source.tar.xz"


async def firefox_release_exists(version: str) -> bool:
    if not VERSION_RE.fullmatch(version):
        return False
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.head(source_url(version))
        if response.status_code == 405:
            response = await client.get(source_url(version), headers={"Range": "bytes=0-0"})
    return response.status_code in {200, 206}


def ssh_client() -> paramiko.SSHClient:
    if not WORKER_PASSWORD:
        raise RuntimeError("WINDOWS_PASSWORD/WORKER_PASSWORD fehlt")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(WORKER_HOST, port=WORKER_SSH_PORT, username=WORKER_USER, password=WORKER_PASSWORD,
                   timeout=5, banner_timeout=8, auth_timeout=8, look_for_keys=False, allow_agent=False)
    return client


def sftp_read_json(sftp: paramiko.SFTPClient, path: str) -> dict[str, Any] | None:
    try:
        with sftp.open(path, "r") as fh:
            raw = fh.read()
        if isinstance(raw, bytes): raw = raw.decode("utf-8-sig", "replace")
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def publish_android_worker_code() -> dict[str, str]:
    """Distribute real Android worker code over the existing shared bind mount.

    The old Android image cannot self-upgrade; the one-time launcher migration is
    required. After that, manager deployment is enough for subsequent versions.
    """
    src = ANDROID_WORKER_OEM_DIR / "worker.py"
    payload = src.read_bytes()
    version_match = re.search(rb'^VERSION = "([0-9.]+)"$', payload, re.M)
    version = version_match.group(1).decode() if version_match else None
    if version != APP_VERSION:
        raise ValueError(f"Android-Worker-Quelldatei {version or '?'} passt nicht zum Manager {APP_VERSION}")
    compile(payload, str(src), "exec")
    sha = hashlib.sha256(payload).hexdigest()
    target = ANDROID_WORKER_UPDATES / "worker.py"
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        temp.write_bytes(payload)
        os.replace(temp, target)
    manifest = {"version": version, "sha256": sha}
    manifest_path = ANDROID_WORKER_UPDATES / "worker_manifest.json"
    old = None
    try: old = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError): pass
    if old != manifest:
        atomic_json_write(manifest_path, manifest)
    return manifest


def _android_worker_distribution_loop() -> None:
    while True:
        try:
            publish_android_worker_code()
        except Exception as exc:
            print(f"ANDROID: Worker-Code-Verteilung fehlgeschlagen: {exc}", flush=True)
        time.sleep(15)


@app.on_event("startup")
def _start_android_distribution() -> None:
    if PROJECT_PLATFORM != "android-arm64":
        return
    # Immediate first publication avoids racing the Android supervisor at boot.
    try: publish_android_worker_code()
    except Exception as exc: print(f"ANDROID: Worker-Code-Verteilung fehlgeschlagen: {exc}", flush=True)
    threading.Thread(target=_android_worker_distribution_loop,
                     name="android_worker_distribution", daemon=True).start()


def android_worker_status() -> dict[str, Any]:
    """Independent Linux/Android worker heartbeat, never reused as Windows state."""
    status: dict[str, Any] = {
        "online": False, "all_ok": False, "can_accept_jobs": False,
        "worker_version": None, "project_version": APP_VERSION, "version_match": False,
        "state": "OFFLINE", "detail": "Android-Worker ist nicht gestartet.",
        "queue_visible": len(list((ANDROID_QUEUE_DIR / "jobs").glob("*.json"))),
    }
    try:
        marker = json.loads(ANDROID_WORKER_STATUS.read_text(encoding="utf-8-sig"))
        age = time.time() - ANDROID_WORKER_STATUS.stat().st_mtime
        if not isinstance(marker, dict) or age > 90:
            status["detail"] = f"Android-Worker-Heartbeat fehlt oder ist veraltet ({int(age)} s)."
            return status
        status.update({k: marker.get(k) for k in (
            "state", "detail", "worker_version", "free_gib", "memory_gib",
            "cpu_count", "job_id", "toolchain", "source_dir",
            "configured_project_version", "worker_hostname", "worker_started_at", "worker_pid", "worker_sha256", "updated_at",
        ) if k in marker})
        status["online"] = True
        status["heartbeat_age_seconds"] = round(max(0, age), 1)
        try:
            manifest = json.loads((ANDROID_WORKER_UPDATES / "worker_manifest.json").read_text(encoding="utf-8"))
            expected_sha = manifest.get("sha256") if manifest.get("version") == APP_VERSION else None
        except (OSError, ValueError, TypeError):
            expected_sha = None
        status["version_match"] = (status["worker_version"] == APP_VERSION
                                   and status.get("configured_project_version", APP_VERSION) == APP_VERSION)
        status["code_match"] = bool(expected_sha and status.get("worker_sha256") == expected_sha)
        status["all_ok"] = bool(marker.get("ready") and status["version_match"])
        status["can_accept_jobs"] = status["all_ok"]
        status["busy"] = status["state"] == "BUILDING"
        if not status["version_match"]:
            status["detail"] = (
                f"Android-Worker {status['worker_version'] or 'unbekannt'}, Manager {APP_VERSION}: "
                "Worker-Aktualisierung ausstehend; Aktualisierung erfolgt automatisch im IDLE."
            )
    except (OSError, ValueError, TypeError):
        pass
    return status


def android_job_location(build_id: str) -> str | None:
    for location in ("processing", "jobs", "failed"):
        if (ANDROID_QUEUE_DIR / location / f"{build_id}.json").is_file():
            return location
    return None


def push_android_job(job: dict[str, Any]) -> None:
    """Atomic shared-volume queue; Android does not depend on Windows SSH."""
    atomic_json_write(ANDROID_QUEUE_DIR / "jobs" / f"{job['id']}.json", job)


def worker_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "online": False, "ready": False, "all_ok": False, "busy": False, "processing_count": 0,
        "worker_version": None, "project_version": APP_VERSION, "version_match": False,
        "host": WORKER_HOST, "port": WORKER_SSH_PORT, "detail": "Worker nicht erreichbar",
    }
    shared = None
    status_file = SHARED_DIR / "worker_status.json"
    if status_file.is_file():
        try: shared = json.loads(status_file.read_text(encoding="utf-8-sig"))
        except Exception: pass
    if isinstance(shared, dict):
        result["busy"] = shared.get("state") == "BUILDING"
        result["shared_state"] = shared
    try:
        with closing(db()) as conn:
            db_active = int(conn.execute("SELECT COUNT(*) FROM builds WHERE platform='windows-x64' AND status IN ('CLAIMED','BUILDING','PACKAGE')").fetchone()[0])
            result["processing_count"] = db_active
            if db_active > 0:
                result["busy"] = True
    except Exception:
        pass
    try:
        client = ssh_client()
        try:
            result["online"] = True
            sftp = client.open_sftp()
            ready = sftp_read_json(sftp, "C:/firefox_worker/READY.json") or {}
            bootstrap = sftp_read_json(sftp, "C:/firefox_worker/BOOTSTRAP.json") or {}
            dev_live = sftp_read_json(sftp, "C:/firefox_worker/DEVDRIVE.json") or {}
            local = sftp_read_json(sftp, "C:/firefox_worker/worker_local_status.json") or {}
            try:
                remote_processing = len([name for name in sftp.listdir("C:/firefox_worker/processing") if str(name).lower().endswith(".json")])
            except Exception:
                remote_processing = 0
            result["processing_count"] = max(int(result.get("processing_count") or 0), remote_processing)
            result["ready_file"] = ready
            result["bootstrap"] = bootstrap
            result["local_worker"] = local
            # Nie ein positives BUILDING-Signal durch einen eventuell älteren IDLE-
            # Marker überschreiben. DB, SMB-Heartbeat, lokales worker_status und
            # processing/ werden gemeinsam ausgewertet.
            local_busy = str(local.get("state") or "").upper() == "BUILDING"
            result["busy"] = bool(result["busy"] or local_busy or remote_processing > 0)
            result["worker_version"] = local.get("worker_version") or ready.get("worker_version") or bootstrap.get("worker_version")
            result["ready"] = bool(ready.get("ready"))
            result["mozilla_build"] = bool(ready.get("mozilla_build"))
            result["dev_drive"] = dev_live or ready.get("dev_drive") or bootstrap.get("dev_drive")
            result["version_match"] = result["worker_version"] == APP_VERSION
            result["dev_drive_ready"] = bool(isinstance(result["dev_drive"], dict) and result["dev_drive"].get("ready"))
            result["defender"] = ready.get("defender") or {}
            result["vcredist"] = ready.get("vcredist") or {}
            result["pagefile"] = ready.get("pagefile") or {}
            result["mozbuild_state_path"] = ready.get("mozbuild_state_path") or "D:\\mozbuild-state"
            result["vcredist_ready"] = bool(isinstance(result["vcredist"], dict) and result["vcredist"].get("ready"))
            result["pagefile_ready"] = bool(isinstance(result["pagefile"], dict) and result["pagefile"].get("ready"))
            result["all_ok"] = bool(result["online"] and result["ready"] and result["mozilla_build"] and result["dev_drive_ready"] and result["vcredist_ready"] and result["pagefile_ready"] and result["version_match"])
            # Wie im Chromium-Paket bedeutet Queue-Fähigkeit nicht, dass der Worker
            # gerade IDLE sein muss. Während eines Builds dürfen weitere Jobs per
            # SFTP in die lokale Windows-Queue gelegt werden.
            result["can_accept_jobs"] = bool(result["all_ok"])
            if result["all_ok"] and result["busy"]:
                result["detail"] = "Worker beschäftigt · Queue verfügbar"
            elif result["all_ok"]:
                result["detail"] = "Worker bereit"
            else:
                result["detail"] = (result.get("pagefile", {}).get("detail") if not result.get("pagefile_ready") else None) or "Worker erreichbar, Einrichtung/Version noch nicht vollständig"
            sftp.close()
        finally:
            client.close()
    except Exception as exc:
        result["error"] = str(exc)
        result["can_accept_jobs"] = False
    result["auto_update"] = {
        "running": bool(_auto_worker_update_state.get("running")),
        "last_error": _auto_worker_update_state.get("last_error"),
        "last_attempt_age_seconds": (
            round(max(0.0, time.monotonic() - float(_auto_worker_update_state.get("last_attempt") or 0.0)), 1)
            if _auto_worker_update_state.get("last_attempt") else None
        ),
    }
    return result


def ensure_remote_dir(sftp: paramiko.SFTPClient, path: str) -> None:
    normalized = path.replace("\\", "/")
    current = normalized[:2] if re.match(r"^[A-Za-z]:", normalized) else ""
    for part in normalized[2:].strip("/").split("/"):
        current = current + "/" + part
        try: sftp.stat(current)
        except IOError:
            try: sftp.mkdir(current)
            except IOError: pass


def upload_worker_files() -> list[str]:
    files = ["worker.cmd", "dispatch_build.ps1", "build_firefox_windows.ps1", "mark_job_failed.ps1", "repair_worker.cmd", "cleanup_version.cmd", "cleanup_worker_data.ps1", "collect_diagnostics.ps1", "ensure_devdrive.ps1", "ensure_defender_exclusions.ps1", "ensure_vcredist.ps1", "ensure_build_memory.ps1", "update_worker_ready.ps1"]
    missing = [name for name in files if not (WORKER_OEM_DIR / name).is_file()]
    if missing:
        raise RuntimeError("Lokale Worker-Dateien fehlen: " + ", ".join(missing))

    client = ssh_client()
    pushed: list[str] = []
    try:
        sftp = client.open_sftp()
        ensure_remote_dir(sftp, "C:/firefox_worker")
        ensure_remote_dir(sftp, "C:/firefox_worker/jobs")
        for name in files:
            local = WORKER_OEM_DIR / name
            sftp.put(str(local), f"C:/firefox_worker/{name}")
            pushed.append(name)
        sftp.close()

        _, stdout, stderr = client.exec_command(r'cmd.exe /d /c C:\firefox_worker\repair_worker.cmd', timeout=300)
        stdout.channel.settimeout(300)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise RuntimeError(f"Worker-Reparatur fehlgeschlagen (Exit {exit_code}): {err or out or 'unbekannter Fehler'}")
    finally:
        client.close()
    return pushed


def _worker_needs_auto_update(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict):
        return False
    if not status.get("online") or status.get("busy"):
        return False
    # A broken Dev Drive must not block deployment of the very worker update that
    # repairs it. Once Windows/SSH and MozillaBuild are available, a version
    # mismatch is enough to deploy the current scripts automatically.
    if not status.get("mozilla_build"):
        return False
    return str(status.get("worker_version") or "").strip() != APP_VERSION


def _auto_update_worker_once() -> dict[str, Any] | None:
    if not _auto_worker_update_lock.acquire(blocking=False):
        return None
    try:
        now = time.monotonic()
        if now - float(_auto_worker_update_state.get("last_attempt") or 0.0) < 30:
            return None
        status = worker_status()
        if not _worker_needs_auto_update(status):
            return None

        _auto_worker_update_state.update({"last_attempt": now, "running": True, "last_error": None})
        old_version = str(status.get("worker_version") or "?")
        pushed = upload_worker_files()
        result = {"updated": True, "from": old_version, "to": APP_VERSION, "pushed": pushed}
        _auto_worker_update_state.update({"last_result": result, "last_error": None, "running": False})
        return result
    except Exception as exc:
        _auto_worker_update_state.update({
            "last_error": str(exc),
            "running": False,
            "last_attempt": time.monotonic(),
        })
        return None
    finally:
        _auto_worker_update_lock.release()


def _worker_self_heal_loop() -> None:
    # Same principle as the Chromium package: existing Windows VMs receive the
    # current worker scripts automatically whenever Projectstand != Worker-Version.
    # Never interrupt a BUILDING worker; retry later when it is IDLE.
    time.sleep(5)
    while True:
        try:
            _auto_update_worker_once()
        except Exception:
            pass
        time.sleep(10)


@app.on_event("startup")
def _start_worker_self_heal_thread() -> None:
    if PROJECT_PLATFORM != "windows-x64":
        return
    threading.Thread(target=_worker_self_heal_loop, name="worker_self_heal", daemon=True).start()


def remote_job_location(build_id: str) -> str | None:
    try:
        client = ssh_client()
        try:
            sftp = client.open_sftp()
            try:
                for location in ("processing", "jobs", "failed"):
                    try:
                        sftp.stat(f"C:/firefox_worker/{location}/{build_id}.json")
                        return location
                    except IOError:
                        pass
            finally:
                sftp.close()
        finally:
            client.close()
    except Exception:
        return None
    return None


def push_job(job: dict[str, Any]) -> None:
    client = ssh_client()
    try:
        sftp = client.open_sftp()
        ensure_remote_dir(sftp, "C:/firefox_worker/jobs")
        temp = f"C:/firefox_worker/jobs/{job['id']}.tmp"
        final = f"C:/firefox_worker/jobs/{job['id']}.json"
        with sftp.open(temp, "w") as fh:
            fh.write(json.dumps(job, ensure_ascii=False))
        try: sftp.remove(final)
        except IOError: pass
        sftp.rename(temp, final)
        sftp.close()
        client.exec_command(r'schtasks.exe /Run /TN "FirefoxBuildWorker"', timeout=8)
    finally:
        client.close()


def package_job_preferences(build_id: str, artifact_path: str) -> tuple[str, str]:
    """Publish an editable ZIP variant while preserving the original package."""
    source = SHARED_DIR / artifact_path
    if not source.is_file():
        raise ValueError('Fertiges Firefox-Artefakt fehlt')
    if source.suffix.lower() != '.zip':
        raise ValueError('Nachbearbeitung ist derzeit nur für Firefox-ZIP-Artefakte verfügbar.')
    if not source.resolve().is_relative_to(ARTIFACTS_DIR.resolve()):
        raise ValueError('Artefaktpfad liegt außerhalb des Artefakt-Verzeichnisses')
    if source.stem.endswith('-configured'):
        target = source
    else:
        target = source.with_name(source.stem + '-configured.zip')
    repackage(source, target, read_build_prefs(build_id), read_build_bookmarks(build_id))
    return target.name, target.relative_to(SHARED_DIR).as_posix()


def sync_status_files() -> None:
    # /api/queue und /api/builds werden vom Browser parallel abgefragt. Ohne Lock
    # konnten beide Requests denselben alten BUILDING-Status gleichzeitig als
    # verwaist markieren und denselben MANAGER-Logeintrag doppelt schreiben.
    with _status_sync_lock:
        shared_worker_state = None
        shared_worker_job_id = None
        worker_status_path = SHARED_DIR / "worker_status.json"
        if worker_status_path.is_file():
            try:
                shared_worker = json.loads(worker_status_path.read_text(encoding="utf-8-sig"))
                if isinstance(shared_worker, dict):
                    shared_worker_state = str(shared_worker.get("state") or "")
                    shared_worker_job_id = str(shared_worker.get("job_id") or "") or None
            except Exception:
                pass

        with closing(db()) as conn:
            for path in STATUS_DIR.glob("*.json"):
                try:
                    status = json.loads(path.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                bid = str(status.get("id") or path.stem)
                row = conn.execute("SELECT id,status,platform FROM builds WHERE id=?", (bid,)).fetchone()
                if not row:
                    continue

                state = str(status.get("status") or "UNKNOWN")
                # An Android job is independent from the Windows heartbeat. An
                # offline Android container must not leave a BUILDING entry stuck
                # forever when its last heartbeat/status are both stale.
                if row["platform"] == "android-arm64" and state in {"CLAIMED", "BUILDING", "PACKAGE"}:
                    try:
                        android_last_status_age = time.time() - path.stat().st_mtime
                    except OSError:
                        android_last_status_age = 0
                    if not android_worker_status()["online"] and android_last_status_age > 120:
                        state = "FAILED"
                        status.update({"status": "FAILED", "error": "Android-Worker offline oder neu gestartet; Build erneut einreihen, nachdem der Worker bereit ist.",
                                       "updated_at": now_iso()})
                        atomic_json_write(path, status)
                        append_manager_log(bid, "Android-Worker-Heartbeat verloren; Build auf FAILED gesetzt")
                # Wenn der Worker denselben Job aktuell noch als BUILDING meldet,
                # darf ein kurzzeitig/stale gelesener Status niemals als verwaist
                # umgeschrieben werden.
                same_job_still_running = (row["platform"] != "windows-x64" or
                    (shared_worker_state == "BUILDING" and shared_worker_job_id == bid))
                if row["platform"] == "windows-x64" and state in {"CLAIMED", "BUILDING", "PACKAGE"} and shared_worker_state == "IDLE" and not same_job_still_running:
                    try:
                        age = time.time() - path.stat().st_mtime
                    except OSError:
                        age = 0
                    if age >= 60:
                        state = "FAILED"
                        status["status"] = state
                        status["error"] = status.get("error") or "Worker ist bereits wieder IDLE, aber der Build hatte keinen finalen Status. Der Lauf wurde automatisch als FAILED markiert."
                        status["updated_at"] = now_iso()
                        tmp = path.with_suffix(".tmp")
                        try:
                            tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
                            tmp.replace(path)
                            if row["status"] in {"CLAIMED", "BUILDING", "PACKAGE"}:
                                append_manager_log(bid, "Verwaisten aktiven Status erkannt; automatisch auf FAILED gesetzt")
                        except Exception:
                            pass

                if state == 'FINISHED':
                    delivery = conn.execute("SELECT prefs_delivery FROM builds WHERE id=?", (bid,)).fetchone()
                    packaged_marker = PREFS_DIR / f"{bid}.packaged.json"
                    if delivery and delivery[0] == 'overlay' and not packaged_marker.is_file():
                        try:
                            name, pathrel = package_job_preferences(bid, str(status.get('artifact_path') or ''))
                            status['artifact_name'] = name
                            status['artifact_path'] = pathrel
                            atomic_json_write(packaged_marker, {'artifact_path': pathrel, 'updated_at': now_iso()})
                            atomic_json_write(STATUS_DIR / f"{bid}.json", status)
                            append_manager_log(bid, 'about:config als editierbare Standard-Prefs in ZIP gepackt')
                        except Exception as exc:
                            # An actual build succeeded, but do not publish a ZIP without
                            # the promised prefs. Keep PACKAGE until artifact is fixed.
                            state = 'PACKAGE'
                            status['error'] = f'about:config-ZIP konnte nicht erstellt werden: {exc}'
                conn.execute("UPDATE builds SET status=?, updated_at=?, artifact_name=COALESCE(?,artifact_name), artifact_path=COALESCE(?,artifact_path), error=? WHERE id=?",
                             (state, now_iso(), status.get("artifact_name"), status.get("artifact_path"), status.get("error"), bid))
            conn.commit()


def _status_reconcile_loop() -> None:
    """Import worker status independently of the web UI.

    Build completion and artifact finalization must never depend on a browser
    polling /api/queue or /api/builds. A small manager-side loop keeps the DB
    and finished artifacts current even when nobody has the UI open.
    """
    while True:
        try:
            sync_status_files()
        except Exception as exc:
            print(f"STATUS: Hintergrund-Synchronisierung fehlgeschlagen: {exc}", flush=True)
        time.sleep(5)


def append_manager_log(build_id: str, line: str) -> None:
    path = LOGS_DIR / f"{build_id}.log"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()} MANAGER: {line}\n")


@app.on_event("startup")
def startup() -> None:
    init_db()
    # One immediate reconciliation handles a FINISHED status left behind while
    # the manager was restarted. Afterwards the daemon loop owns synchronization.
    try:
        sync_status_files()
    except Exception as exc:
        print(f"STATUS: Initiale Synchronisierung fehlgeschlagen: {exc}", flush=True)
    threading.Thread(target=_status_reconcile_loop, name="status_reconciler", daemon=True).start()

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "app_version": APP_VERSION, "project_platform": PROJECT_PLATFORM, "project_title": PROJECT_TITLE})

@app.get("/health", include_in_schema=False)
def health(): return {"ok": True, "version": APP_VERSION}

@app.get("/api/ui-config")
def api_ui_config(): return load_ui_config()

@app.patch("/api/ui-config")
def api_ui_config_patch(payload: UiConfigPatch): return save_ui_config(payload.cards)

# Global bookmarks are server-side like about:config, with independent per-build snapshots.
def load_bookmarks() -> dict[str, Any]:
    with _bookmarks_lock:
        if not BOOKMARKS_PATH.is_file():
            return starter_config()
        try:
            original = json.loads(BOOKMARKS_PATH.read_text(encoding='utf-8-sig'))
            migrated = upgrade_bookmark_defaults(original)
            if migrated != original:
                atomic_json_write(BOOKMARKS_PATH, migrated)
            return migrated
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=500, detail=f'bookmarks.json ungültig: {exc}') from exc


def read_build_bookmarks(build_id: str) -> dict[str, Any] | None:
    path = PREFS_DIR / f'{build_id}.bookmarks.json'
    if not path.is_file():
        return None  # Older artifact: preserve existing bookmark policy on prefs-only edit.
    return validate_bookmarks(json.loads(path.read_text(encoding='utf-8-sig')))


@app.get('/api/bookmarks')
def api_bookmarks_get():
    return load_bookmarks()


@app.put('/api/bookmarks')
def api_bookmarks_put(payload: dict[str, Any]):
    try:
        doc = validate_bookmarks(payload)
        # Explicit edits must not reintroduce a bookmark removed by the user.
        doc['defaults_revision'] = 2
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _bookmarks_lock:
        atomic_json_write(BOOKMARKS_PATH, doc)
    return doc


@app.post('/api/bookmarks/import')
def api_bookmarks_import(payload: dict[str, Any]):
    filename, content = payload.get('filename'), payload.get('content')
    mode = payload.get('mode', 'append')
    if not isinstance(filename, str) or mode not in ('append', 'replace'):
        raise HTTPException(status_code=422, detail='Ungültiger Dateiname oder Importmodus.')
    try:
        rows, info = import_bookmarks_content(filename, content)
        with _bookmarks_lock:
            old = upgrade_bookmark_defaults(json.loads(BOOKMARKS_PATH.read_text(encoding='utf-8-sig'))) if BOOKMARKS_PATH.is_file() else starter_config()
            doc = {'schema': 1, 'defaults_revision': 2, 'folders': list(old['folders']),
                   'entries': list(old['entries']) if mode == 'append' else []}
            existing = {(e['title'].strip(), e['url'].strip(), e['placement'], e['folder']) for e in doc['entries']}
            added = 0
            for entry in rows:
                key = (entry['title'].strip(), entry['url'].strip(), entry['placement'], entry['folder'])
                if key in existing:
                    continue
                if entry['folder'] and entry['folder'] not in doc['folders']:
                    doc['folders'].append(entry['folder'])
                doc['entries'].append(entry)
                existing.add(key)
                added += 1
            doc = validate_bookmarks(doc)
            atomic_json_write(BOOKMARKS_PATH, doc)
        return {'config': doc, 'added': added, **info}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/about-config")
def api_about_config_get():
    return load_about_config()

@app.put("/api/about-config")
def api_about_config_put(payload: dict[str, Any]):
    return save_about_config(payload)

@app.get("/api/android/about-config")
def api_android_about_config_get():
    return load_android_about_config()

@app.put("/api/android/about-config")
def api_android_about_config_put(payload: dict[str, Any]):
    return save_android_about_config(payload)


@app.get("/api/latest")
async def api_latest(force: bool = False): return await latest_firefox(force)

@app.get("/api/version/{version}/exists")
async def api_version_exists(version: str):
    return {"version": version, "exists": await firefox_release_exists(version), "source_url": source_url(version)}

@app.get("/api/worker")
def api_worker(): return worker_status()

@app.get("/api/worker/android")
def api_android_worker(): return android_worker_status()

@app.get("/api/android/signing")
def api_android_signing_status():
    return android_signing_status()

@app.post("/api/android/signing/generate")
def api_android_signing_generate():
    try:
        return generate_android_signing_key()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@app.post("/api/android/signing/export")
def api_android_signing_export(payload: SigningBackupRequest):
    try:
        data = export_android_signing_backup(payload.password)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    headers = {"Content-Disposition": 'attachment; filename="firefox_android_signing_backup.zip"'}
    return Response(content=data, media_type="application/zip", headers=headers)

@app.post("/api/android/signing/import")
async def api_android_signing_import(
    backup: UploadFile = File(...),
    password: str = Form(..., min_length=12, max_length=256),
    replace: bool = Form(False),
):
    filename = (backup.filename or "").lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=422, detail="Bitte ein exportiertes Signing-Backup (.zip) auswählen.")
    data = await backup.read(5 * 1024 * 1024 + 1)
    try:
        return import_android_signing_backup(data, password, replace=replace)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.post("/api/worker/finalize")
def api_worker_finalize():
    try:
        pushed = upload_worker_files()
        time.sleep(1)
        return {"ok": True, "pushed": pushed, "worker": worker_status()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/api/queue")
def api_queue():
    selected_worker = worker_status() if PROJECT_PLATFORM == "windows-x64" else android_worker_status()
    with closing(db()) as conn:
        queued = conn.execute("SELECT COUNT(*) FROM builds WHERE platform=? AND status='QUEUED'", (PROJECT_PLATFORM,)).fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM builds WHERE platform=? AND status IN ('CLAIMED','BUILDING','PACKAGE')", (PROJECT_PLATFORM,)).fetchone()[0]
    return {"queued": queued, "active": active, "worker": selected_worker, "platform": PROJECT_PLATFORM}

@app.get("/api/builds")
def api_builds():
    with closing(db()) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM builds WHERE platform=? ORDER BY created_at DESC", (PROJECT_PLATFORM,)).fetchall()]
    for row in rows:
        row["artifact_available"] = bool(row.get("artifact_path") and (SHARED_DIR / str(row["artifact_path"])).is_file())
        row["log_available"] = (LOGS_DIR / f"{row['id']}.log").is_file()
        row["diagnostics_available"] = row["log_available"] or (DIAGNOSTICS_DIR / row["id"]).is_dir()
        cleanup_id = row.get("cleanup_job_id")
        row["workspace_cleanup"] = None
        if cleanup_id:
            try:
                row["workspace_cleanup"] = json.loads((STATUS_DIR / f"{cleanup_id}.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                row["workspace_cleanup"] = {"status": "QUEUED"}
    return rows

@app.post("/api/builds", status_code=202)
async def api_create_build(payload: BuildRequest):
    if payload.platform != PROJECT_PLATFORM:
        raise HTTPException(status_code=400, detail=f"Dieses Standalone-Projekt unterstützt nur {PROJECT_PLATFORM}.")
    if not await firefox_release_exists(payload.version):
        raise HTTPException(status_code=404, detail="Firefox-Release bzw. Source-Archiv wurde bei Mozilla nicht gefunden.")
    if payload.platform == "android-arm64":
        return create_android_build(payload)
    worker = worker_status()
    if not worker.get("all_ok"):
        raise HTTPException(status_code=409, detail="Windows-Worker ist noch nicht vollständig bereit oder Worker-Version entspricht nicht dem Projektstand.")
    try:
        full_config = validate_about_config(payload.about_config_full if payload.about_config_full is not None else load_about_config())
        bookmarks_config = validate_bookmarks(payload.bookmarks if payload.bookmarks is not None else load_bookmarks())
        selected_prefs = validate_selected_prefs(payload.about_config if payload.about_config is not None else [
            row for category in full_config["categories"] for row in category["rows"]
        ])
        if selected_prefs != validate_selected_prefs([row for cat in full_config['categories'] for row in cat['rows']]):
            raise ValueError('about:config-Auswahl stimmt nicht mit der vollständigen Ansicht überein.')
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    flavor = "nightly" if payload.nightly else "release"
    bid = str(uuid.uuid4())
    created = now_iso()
    with closing(db()) as conn:
        for other in conn.execute("SELECT cleanup_job_id FROM builds WHERE version=? AND platform='windows-x64' AND cleanup_job_id IS NOT NULL", (payload.version,)):
            cp = STATUS_DIR / f"{other[0]}.json"
            try: cs = json.loads(cp.read_text(encoding="utf-8-sig")).get("status")
            except (OSError, ValueError): cs = None
            if cs not in {"FINISHED", "FAILED"}:
                raise HTTPException(status_code=409, detail="Workerdaten der Firefox-Version werden gerade bereinigt")
        if conn.execute("SELECT id FROM builds WHERE version=? AND platform=? AND flavor=?", (payload.version, payload.platform, flavor)).fetchone():
            raise HTTPException(status_code=409, detail="Diese Firefox-Version mit dieser Branding-Auswahl wurde für diese Plattform bereits angelegt.")
        conn.execute("INSERT INTO builds(id,version,platform,flavor,status,created_at,updated_at,prefs_delivery) VALUES(?,?,?,?,?,?,?,?)",
                     (bid, payload.version, payload.platform, flavor, "QUEUED", created, created, 'overlay'))
        conn.commit()
    prefs_path = PREFS_DIR / f"{bid}.json"
    atomic_json_write(prefs_path, selected_prefs)
    atomic_json_write(PREFS_DIR / f"{bid}.full.json", full_config)
    atomic_json_write(PREFS_DIR / f"{bid}.bookmarks.json", bookmarks_config)
    job = {"id": bid, "version": payload.version, "platform": payload.platform, "created_at": created,
           "source_url": source_url(payload.version), "flavor": flavor, "project_version": APP_VERSION,
           "about_config": selected_prefs}
    append_manager_log(bid, f"Firefox {payload.version} ({flavor}) in Warteschlange angelegt")
    try:
        push_job(job)
        append_manager_log(bid, "Job per SFTP nach C:\\firefox_worker\\jobs übertragen")
    except Exception as exc:
        append_manager_log(bid, f"Queue lokal angelegt, Sofortübertragung fehlgeschlagen: {exc}")
        raise HTTPException(status_code=502, detail=f"Build wurde angelegt, aber Worker-Übertragung schlug fehl: {exc}")
    return job | {"status": "QUEUED"}

def create_android_build(payload: BuildRequest) -> dict[str, Any]:
    """Build Firefox/Fenix for Android ARM64 as debug or locally signed release APK."""
    if payload.nightly or payload.about_config is not None or payload.about_config_full is not None or payload.bookmarks is not None:
        raise HTTPException(status_code=422, detail="Android-Builds übernehmen keine Desktop-Branding-, Desktop-about:config- oder Lesezeichen-Policy.")
    try:
        android_about = validate_android_about_config(
            payload.android_about_config if payload.android_about_config is not None else load_android_about_config()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    build_type = (payload.android_build_type or "debug").strip().lower()
    if build_type not in {"debug", "release"}:
        raise HTTPException(status_code=422, detail="Android-Build-Typ muss debug oder release sein.")
    status = android_worker_status()
    if not status["all_ok"]:
        raise HTTPException(status_code=409, detail=f"Android-Worker nicht bereit: {status['detail']}")
    signing = None
    if build_type == "release":
        signing = android_signing_status()
        if not signing.get("valid"):
            raise HTTPException(status_code=409, detail="Für einen Android-Release-Build muss zuerst ein gültiger eigener Release-Keystore erzeugt oder importiert werden.")
    created = now_iso()
    bid = str(uuid.uuid4())
    flavor = build_type
    job = {"id": bid, "version": payload.version, "platform": "android-arm64",
           "flavor": flavor, "created_at": created, "source_url": source_url(payload.version),
           "project_version": APP_VERSION, "android_about_config": android_about}
    if signing:
        job["signing_fingerprint"] = signing["fingerprint_sha256"]
    with _requeue_lock:
        with closing(db()) as conn:
            for other in conn.execute("SELECT cleanup_job_id FROM builds WHERE version=? AND platform='android-arm64' AND cleanup_job_id IS NOT NULL", (payload.version,)):
                cp = STATUS_DIR / f"{other[0]}.json"
                try: cs = json.loads(cp.read_text(encoding="utf-8-sig")).get("status")
                except (OSError, ValueError): cs = None
                if cs not in {"FINISHED", "FAILED"}:
                    raise HTTPException(status_code=409, detail="Android-Workerdaten dieser Firefox-Version werden gerade bereinigt")
            if conn.execute("SELECT id FROM builds WHERE version=? AND platform=? AND flavor=?",
                            (payload.version, "android-arm64", flavor)).fetchone():
                raise HTTPException(status_code=409, detail=f"Android-{build_type.capitalize()}-Version ist bereits in der Versionsverwaltung vorhanden.")
            conn.execute("INSERT INTO builds(id,version,platform,flavor,status,created_at,updated_at,prefs_delivery) VALUES(?,?,?,?,?,?,?,?)",
                         (bid, payload.version, "android-arm64", flavor, "QUEUED", created, created, "native"))
            conn.commit()
        android_meta = {"schema": 1, "build_type": build_type, "about_config": android_about}
        if signing:
            android_meta["signing_fingerprint"] = signing["fingerprint_sha256"]
        atomic_json_write(PREFS_DIR / f"{bid}.android.json", android_meta)
        active_sync = sum(1 for item in android_about["sync_server"].values() if item["enabled"])
        append_manager_log(bid, f"Android ARM64 Fenix-{build_type.capitalize()}-Build angelegt (eigene Linux-Queue; {active_sync} Sync-Server-Overrides)")
        try:
            push_android_job(job)
        except (OSError, ValueError) as exc:
            with closing(db()) as conn:
                conn.execute("UPDATE builds SET status='FAILED',error=? WHERE id=?", (str(exc), bid))
                conn.commit()
            append_manager_log(bid, f"Übertragung in Android-Queue fehlgeschlagen: {exc}")
            raise HTTPException(status_code=502, detail=f"Android-Queue fehlgeschlagen: {exc}") from exc
    return job | {"status": "QUEUED"}


@app.post("/api/builds/{build_id}/requeue")
def api_requeue(build_id: str):
    # Verhindert Doppelklick/parallel gestartete Retries desselben Build-IDs.
    # Der alte Stand konnte dadurch denselben Job erneut nach jobs/ schreiben,
    # während der Worker ihn bereits nach processing/ verschoben hatte.
    with _requeue_lock:
        sync_status_files()
        with closing(db()) as conn:
            row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Build nicht gefunden")
            current = str(row["status"] or "")
            if current in {"CLAIMED", "BUILDING", "PACKAGE"}:
                raise HTTPException(status_code=409, detail="Build läuft bereits und kann nicht erneut eingereiht werden")
            if current == "FINISHED":
                raise HTTPException(status_code=409, detail="Fertiger Build wird nicht erneut eingereiht")

            pending = conn.execute("SELECT COUNT(*) FROM builds WHERE version=? AND platform=? AND cleanup_job_id IS NOT NULL", (row["version"], row["platform"])).fetchone()[0]
            if pending:
                for cleaning in conn.execute("SELECT cleanup_job_id FROM builds WHERE version=? AND platform=? AND cleanup_job_id IS NOT NULL", (row["version"], row["platform"])):
                    cp = STATUS_DIR / f"{cleaning[0]}.json"
                    if not cp.is_file():
                        raise HTTPException(status_code=409, detail="Workerdaten-Bereinigung läuft bereits")
                    try: cs = json.loads(cp.read_text(encoding="utf-8-sig")).get("status")
                    except Exception: cs = None
                    if cs not in {"FINISHED", "FAILED"}:
                        raise HTTPException(status_code=409, detail="Workerdaten-Bereinigung läuft bereits")
            location = android_job_location(build_id) if row["platform"] == "android-arm64" else remote_job_location(build_id)
            if current == "QUEUED" and location in {"jobs", "processing"}:
                return {"ok": True, "already_queued": True, "location": location}

            job = {"id": row["id"], "version": row["version"], "platform": row["platform"], "created_at": row["created_at"],
                   "source_url": source_url(row["version"]), "flavor": row["flavor"], "project_version": APP_VERSION}
            if row["platform"] == "windows-x64":
                job["about_config"] = read_build_prefs(build_id)
            elif row["platform"] == "android-arm64":
                meta_path = PREFS_DIR / f"{build_id}.android.json"
                try:
                    android_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
                except (OSError, ValueError, TypeError):
                    android_meta = {}
                try:
                    job["android_about_config"] = validate_android_about_config(android_meta.get("about_config"))
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=f"Gespeicherte Android-about:config-Einstellungen sind ungültig: {exc}") from exc
                if row["flavor"] == "release":
                    expected_fp = android_meta.get("signing_fingerprint")
                    signing = android_signing_status()
                    if not signing.get("valid"):
                        raise HTTPException(status_code=409, detail="Release-Build kann nicht erneut eingereiht werden: kein gültiger Release-Keystore vorhanden.")
                    if expected_fp and signing.get("fingerprint_sha256") != expected_fp:
                        raise HTTPException(status_code=409, detail=(
                            "Release-Build wurde mit einem anderen Signing-Key angelegt. "
                            f"Erwartet: {expected_fp}; aktuell: {signing.get('fingerprint_sha256')}."))
                    job["signing_fingerprint"] = expected_fp or signing.get("fingerprint_sha256")
            queued_at = now_iso()
            queue_marker = {"id": build_id, "status": "QUEUED", "updated_at": queued_at, "error": None}
            marker_path = STATUS_DIR / f"{build_id}.json"
            marker_tmp = marker_path.with_suffix(".tmp")
            marker_tmp.write_text(json.dumps(queue_marker, ensure_ascii=False), encoding="utf-8")
            marker_tmp.replace(marker_path)
            conn.execute("UPDATE builds SET status='QUEUED', error=NULL, updated_at=? WHERE id=?", (queued_at, build_id))
            conn.commit()
        try:
            if job["platform"] == "android-arm64":
                push_android_job(job)
            else:
                push_job(job)
        except Exception as exc:
            failed_at = now_iso()
            failed_error = f"Erneutes Einreihen fehlgeschlagen: {exc}"
            failed_marker = {"id": build_id, "status": "FAILED", "updated_at": failed_at, "error": failed_error}
            tmp = (STATUS_DIR / f"{build_id}.json").with_suffix(".tmp")
            tmp.write_text(json.dumps(failed_marker, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATUS_DIR / f"{build_id}.json")
            with closing(db()) as conn:
                conn.execute("UPDATE builds SET status='FAILED', error=?, updated_at=? WHERE id=?",
                             (failed_error, failed_at, build_id))
                conn.commit()
            raise HTTPException(status_code=502, detail=str(exc))
        append_manager_log(build_id, "Build erneut eingereiht")
        return {"ok": True, "already_queued": False}


@app.get('/api/builds/{build_id}/about-config')
def api_build_about_config(build_id: str):
    with closing(db()) as conn:
        row = conn.execute('SELECT * FROM builds WHERE id=?', (build_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='Build nicht gefunden')
    if row['platform'] != 'windows-x64':
        raise HTTPException(status_code=409, detail='about:config-ZIP-Nachbearbeitung ist nur für Windows-Builds vorgesehen.')
    config, exact = build_about_config(build_id)
    return {'id': build_id, 'version': row['version'], 'flavor': row['flavor'],
            'status': row['status'], 'editable': row['status'] == 'FINISHED',
            'prefs_delivery': row['prefs_delivery'] or 'embedded',
            'complete_snapshot': exact, 'config': config}


@app.put('/api/builds/{build_id}/about-config')
def api_build_about_config_put(build_id: str, payload: dict[str, Any]):
    try:
        full = validate_about_config(merge_default_prefs(payload, historical=True))
        selected = validate_selected_prefs([r for cat in full['categories'] for r in cat['rows']])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _requeue_lock:
        sync_status_files()
        with closing(db()) as conn:
            row = conn.execute('SELECT * FROM builds WHERE id=?', (build_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Build nicht gefunden')
            if row['platform'] != 'windows-x64':
                raise HTTPException(status_code=409, detail='about:config-ZIP-Nachbearbeitung ist nur für Windows-Builds vorgesehen.')
            if row['status'] != 'FINISHED':
                raise HTTPException(status_code=409, detail='about:config-Nachbearbeitung nur für FINISHED-Builds; laufende Jobs bleiben unverändert.')
            if not row['artifact_path']:
                raise HTTPException(status_code=409, detail='Build hat kein Paket zum Neupacken.')
            src = SHARED_DIR / row['artifact_path']
            if not src.is_file() or src.suffix.lower() != '.zip':
                raise HTTPException(status_code=409, detail='Nachbearbeitung nur für ZIP-Pakete (nicht für Installer-EXE).')
            try:
                name, pathrel = repackage_artifact(src, selected, read_build_bookmarks(build_id))
            except (ValueError, zipfile.BadZipFile, OSError) as exc:
                raise HTTPException(status_code=422, detail=f'Paket unverändert: {exc}') from exc
            marker_file = STATUS_DIR / f'{build_id}.json'
            if marker_file.is_file():
                try:
                    marker = json.loads(marker_file.read_text(encoding='utf-8-sig'))
                    if marker.get('status') == 'FINISHED':
                        marker['artifact_name'] = name
                        marker['artifact_path'] = pathrel
                        atomic_json_write(marker_file, marker)
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=500, detail=f'Paket erstellt, Status-Metadaten konnten nicht aktualisiert werden: {exc}') from exc
            conn.execute('UPDATE builds SET artifact_name=?,artifact_path=?,updated_at=? WHERE id=?',
                         (name, pathrel, now_iso(), build_id))
            conn.commit()
            atomic_json_write(PREFS_DIR / f'{build_id}.json', selected)
            atomic_json_write(PREFS_DIR / f'{build_id}.full.json', full)
            atomic_json_write(PREFS_DIR / f'{build_id}.packaged.json', {'artifact_path':pathrel, 'updated_at':now_iso()})
            # The original v0.0.14 executable may still contain embedded values.
            # We cannot reliably un-bake them from omni.ja without rebuilding.
            append_manager_log(build_id, f'about:config geändert: {len(selected)} aktive Standard-Prefs; ZIP ohne Compilerlauf neu gepackt')
    return {'ok': True, 'selected_count': len(selected), 'artifact_name': name,
            'artifact_path': pathrel, 'prefs_delivery': row['prefs_delivery'] or 'embedded'}


def repackage_artifact(source: Path, selected: list[dict[str, Any]],
                       bookmarks: dict[str, Any] | None = None) -> tuple[str, str]:
    if not source.resolve().is_relative_to(ARTIFACTS_DIR.resolve()):
        raise ValueError('Ungültiger Artefaktpfad')
    dest = source if source.stem.endswith('-configured') else source.with_name(source.stem + '-configured.zip')
    repackage(source, dest, selected, bookmarks)
    return dest.name, dest.relative_to(SHARED_DIR).as_posix()


@app.get('/api/builds/{build_id}/bookmarks')
def api_build_bookmarks_get(build_id: str):
    with closing(db()) as conn:
        row = conn.execute('SELECT * FROM builds WHERE id=?', (build_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='Build nicht gefunden')
    if row['platform'] != 'windows-x64':
        raise HTTPException(status_code=409, detail='Lesezeichen-Policy-Nachbearbeitung ist nur für Windows-Builds vorgesehen.')
    exact = read_build_bookmarks(build_id)
    return {'id': build_id, 'version': row['version'], 'flavor': row['flavor'],
            'status': row['status'], 'editable': row['status'] == 'FINISHED',
            'complete_snapshot': exact is not None,
            'config': exact if exact is not None else starter_config()}


@app.put('/api/builds/{build_id}/bookmarks')
def api_build_bookmarks_put(build_id: str, payload: dict[str, Any]):
    try:
        bookmarks = validate_bookmarks(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _requeue_lock:
        sync_status_files()
        with closing(db()) as conn:
            row = conn.execute('SELECT * FROM builds WHERE id=?', (build_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='Build nicht gefunden')
            if row['platform'] != 'windows-x64':
                raise HTTPException(status_code=409, detail='Lesezeichen-Policy-Nachbearbeitung ist nur für Windows-Builds vorgesehen.')
            if row['status'] != 'FINISHED' or not row['artifact_path']:
                raise HTTPException(status_code=409, detail='Lesezeichen-Nachbearbeitung nur für fertige ZIP-Builds.')
            src = SHARED_DIR / row['artifact_path']
            if not src.is_file() or src.suffix.lower() != '.zip':
                raise HTTPException(status_code=409, detail='Fertiges Firefox-ZIP fehlt.')
            try:
                name, pathrel = repackage_artifact(src, read_build_prefs(build_id), bookmarks)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                raise HTTPException(status_code=422, detail=f'Paket unverändert: {exc}') from exc
            marker_file = STATUS_DIR / f'{build_id}.json'
            if marker_file.is_file():
                marker = json.loads(marker_file.read_text(encoding='utf-8-sig'))
                if marker.get('status') == 'FINISHED':
                    marker.update({'artifact_name': name, 'artifact_path': pathrel})
                    atomic_json_write(marker_file, marker)
            conn.execute('UPDATE builds SET artifact_name=?,artifact_path=?,updated_at=? WHERE id=?',
                         (name, pathrel, now_iso(), build_id))
            conn.commit()
            atomic_json_write(PREFS_DIR / f'{build_id}.bookmarks.json', bookmarks)
            atomic_json_write(PREFS_DIR / f'{build_id}.packaged.json',
                              {'artifact_path': pathrel, 'updated_at': now_iso()})
            append_manager_log(build_id, f'Lesezeichen angepasst: {sum(x["enabled"] for x in bookmarks["entries"])} aktive Einträge; ZIP ohne Compilerlauf neu gepackt')
    return {'ok': True, 'artifact_name': name, 'artifact_path': pathrel}


@app.post("/api/builds/{build_id}/cleanup-workspace", status_code=202)
def api_cleanup_workspace(build_id: str):
    """Delete only version-specific worker data; shared toolchains/signing remain intact."""
    with _requeue_lock:
        sync_status_files()
        with closing(db()) as conn:
            row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Build nicht gefunden")
            platform_name = str(row["platform"])
            if platform_name not in {"windows-x64", "android-arm64"}:
                raise HTTPException(status_code=409, detail="Workerdaten-Bereinigung wird für diese Plattform nicht unterstützt.")
            if row["status"] not in {"FINISHED", "FAILED"}:
                raise HTTPException(status_code=409, detail="Workerdaten dürfen nur bei FINISHED oder FAILED gelöscht werden")
            version = str(row["version"])
            active = conn.execute(
                "SELECT COUNT(*) FROM builds WHERE version=? AND platform=? AND status IN ('QUEUED','CLAIMED','BUILDING','PACKAGE')",
                (version, platform_name),
            ).fetchone()[0]
            if active:
                raise HTTPException(status_code=409, detail="Eine Variante dieser Firefox-Version ist noch aktiv oder eingereiht")
            for other in conn.execute(
                "SELECT cleanup_job_id FROM builds WHERE version=? AND platform=? AND cleanup_job_id IS NOT NULL",
                (version, platform_name),
            ):
                path = STATUS_DIR / f"{other[0]}.json"
                if not path.is_file():
                    raise HTTPException(status_code=409, detail="Vorherige Workerdaten-Bereinigung ist noch nicht abgeschlossen")
                try:
                    status = json.loads(path.read_text(encoding="utf-8-sig")).get("status")
                except Exception:
                    status = None
                if status not in {"FINISHED", "FAILED"}:
                    raise HTTPException(status_code=409, detail="Vorherige Workerdaten-Bereinigung ist noch nicht abgeschlossen")
            cleanup_id = str(uuid.uuid4())
            job = {"id": cleanup_id, "kind": "cleanup_workspace", "version": version,
                   "platform": platform_name, "created_at": now_iso(), "project_version": APP_VERSION}
            marker = STATUS_DIR / f"{cleanup_id}.json"
            marker.write_text(json.dumps({"id": cleanup_id, "status": "QUEUED", "updated_at": now_iso()}, ensure_ascii=False), encoding="utf-8")
            conn.execute("UPDATE builds SET cleanup_job_id=? WHERE id=?", (cleanup_id, build_id))
            conn.commit()
        try:
            if platform_name == "android-arm64":
                push_android_job(job)
            else:
                push_job(job)
        except Exception as exc:
            marker.write_text(json.dumps({"id": cleanup_id, "status": "FAILED", "error": str(exc), "updated_at": now_iso()}, ensure_ascii=False), encoding="utf-8")
            raise HTTPException(status_code=502, detail=f"Bereinigung konnte nicht übertragen werden: {exc}")
        workspace = (f"/work/sources/firefox-{version} + /work/objs/firefox-{version}-android-arm64"
                     if platform_name == "android-arm64" else f"D:\\mozilla-source\\firefox-{version}")
        return {"queued": True, "cleanup_job_id": cleanup_id, "version": version,
                "platform": platform_name, "workspace": workspace}


@app.delete("/api/builds/{build_id}/logs")
def api_delete_build_logs(build_id: str):
    sync_status_files()
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Build nicht gefunden")
    if row["status"] != "FINISHED":
        raise HTTPException(status_code=409, detail="Log-/Diagnosedaten dürfen nur bei FINISHED gelöscht werden")
    if ((android_job_location(build_id) if row["platform"] == "android-arm64" else remote_job_location(build_id)) == "processing"):
        raise HTTPException(status_code=409, detail="Job befindet sich noch in processing")
    deleted = 0
    freed = 0
    logfile = LOGS_DIR / f"{build_id}.log"
    for f in [logfile]:
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
            deleted += 1
    diag = DIAGNOSTICS_DIR / build_id
    if diag.exists():
        for f in diag.rglob("*"):
            if f.is_file():
                freed += f.stat().st_size
                deleted += 1
        shutil.rmtree(diag)
    # Never append to the build log here: it would re-create a deleted log.
    return {"deleted": True, "deleted_files": deleted, "freed_bytes": freed}


@app.delete("/api/builds/{build_id}")
def api_delete_build(build_id: str):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="Build nicht gefunden")
        if row["status"] in {"CLAIMED", "BUILDING", "PACKAGE"}: raise HTTPException(status_code=409, detail="Laufender Build kann nicht gelöscht werden")
        if row["platform"] == "android-arm64" and row["status"] == "QUEUED":
            (ANDROID_QUEUE_DIR / "jobs" / f"{build_id}.json").unlink(missing_ok=True)
        conn.execute("DELETE FROM builds WHERE id=?", (build_id,)); conn.commit()
    for suffix in ('.json', '.full.json', '.packaged.json', '.bookmarks.json'):
        (PREFS_DIR / f"{build_id}{suffix}").unlink(missing_ok=True)
    for base in (STATUS_DIR, LOGS_DIR):
        p = base / f"{build_id}.json" if base == STATUS_DIR else base / f"{build_id}.log"
        p.unlink(missing_ok=True)
    return {"deleted": True}

@app.get("/api/builds/{build_id}/log")
def api_build_log(build_id: str, lines: int = 120):
    path = LOGS_DIR / f"{build_id}.log"
    if not path.is_file():
        return {"id": build_id, "text": "Noch keine Logausgabe.", "size_bytes": 0, "log_age_seconds": None, "download_available": (DIAGNOSTICS_DIR / build_id).is_dir(), "download_url": f"/api/builds/{build_id}/diagnostics/download"}
    content = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    try:
        stat = path.stat()
        age = max(0.0, time.time() - stat.st_mtime)
        size = stat.st_size
    except OSError:
        age = None
        size = 0
    return {
        "id": build_id,
        "text": "\n".join(content[-max(20, min(lines, 1000)):]),
        "size_bytes": size,
        "log_age_seconds": age,
        "download_available": True,
        "download_url": f"/api/builds/{build_id}/diagnostics/download",
    }


@app.get("/api/builds/{build_id}/log/download")
def api_build_log_download(build_id: str):
    path = LOGS_DIR / f"{build_id}.log"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Build-Log ist noch nicht vorhanden")
    with closing(db()) as conn:
        row = conn.execute("SELECT version FROM builds WHERE id=?", (build_id,)).fetchone()
    version = str(row["version"]) if row else "unknown"
    return FileResponse(path, filename=f"firefox-{version}-{build_id}.log", media_type="text/plain; charset=utf-8")


@app.get("/api/builds/{build_id}/diagnostics/download")
def api_diagnostics_download(build_id: str):
    """Chromium-style full diagnostic ZIP; never include multi-GB source/artifacts."""
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Build nicht gefunden")
    diag = DIAGNOSTICS_DIR / build_id
    logfile = LOGS_DIR / f"{build_id}.log"
    if not logfile.is_file() and not diag.is_dir():
        raise HTTPException(status_code=404, detail="Keine Diagnose- oder Logdateien vorhanden")
    tmp = tempfile.NamedTemporaryFile(prefix="firefox_diagnostics_", suffix=".zip", delete=False)
    name = tmp.name
    tmp.close()
    metadata = dict(row)
    metadata["exported_at"] = now_iso()
    metadata["project_version"] = APP_VERSION
    skipped = []
    try:
        with zipfile.ZipFile(name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as z:
            z.writestr("README.txt", "Firefox Build - vollständige Diagnose\n"
                "build.log = vollständiger Manager-/Worker-Log; build_metadata.json = DB-Metadaten.\n"
                "status.json = Build-Status; job_*.json = Queue-Kontext.\n"
                "worker/ = mach-/configure-/mozconfig-/Ressourcen-Diagnosen, soweit vorhanden.\n"
                "Binaries, Firefox-Source und gesamte obj-Verzeichnisse sind keine Diagnose und werden nicht gepackt.\n"
                "Ältere Builds (vor 0.0.12) haben ggf. keine worker/-Snapshots.\n")
            z.writestr("build_metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
            for src, archive in ((logfile, "build.log"),
                                 (PREFS_DIR / f"{build_id}.json", "about_config_selected.json"),
                                 (PREFS_DIR / f"{build_id}.full.json", "about_config_full.json"),
                                 (PREFS_DIR / f"{build_id}.bookmarks.json", "bookmarks_config.json"),
                                 (STATUS_DIR / f"{build_id}.json", "status.json")):
                if src.is_file():
                    z.write(src, archive)
            # Show exactly which editable defaults are in the final ZIP.
            artifact_path = row['artifact_path']
            if artifact_path and str(artifact_path).lower().endswith('.zip'):
                archive = SHARED_DIR / str(artifact_path)
                if archive.is_file():
                    try:
                        with zipfile.ZipFile(archive) as artifact:
                            for part in artifact.namelist():
                                if part.endswith('/firefox-webbrowser-build.cfg') or part.endswith('/defaults/pref/firefox-webbrowser-build.js') or part.endswith('/distribution/policies.json'):
                                    z.writestr('packaged_prefs/' + part.split('/')[-1], artifact.read(part))
                    except (OSError, zipfile.BadZipFile):
                        pass
            for src in sorted(diag.rglob("*")) if diag.is_dir() else []:
                if not src.is_file():
                    continue
                rel = src.relative_to(diag)
                if src.stat().st_size > 80*1024*1024:
                    skipped.append(f"{rel.as_posix()} ({src.stat().st_size} bytes)")
                    continue
                z.write(src, f"worker/{rel.as_posix()}")
            if skipped:
                z.writestr("SKIPPED_LARGE_FILES.txt", "Dateien >80 MiB wurden zur Begrenzung des Diagnosearchivs ausgelassen:\n" + "\n".join(skipped))
        return FileResponse(name, filename=f"firefox_build_diagnostics_{row['version']}_{row['flavor']}_{build_id}.zip",
                            media_type="application/zip", background=BackgroundTask(lambda: Path(name).unlink(missing_ok=True)))
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


@app.get("/api/builds/{build_id}/download")
def api_download(build_id: str):
    sync_status_files()
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
    if not row or not row["artifact_path"]: raise HTTPException(status_code=404, detail="Artefakt nicht vorhanden")
    path = SHARED_DIR / row["artifact_path"]
    if not path.is_file(): raise HTTPException(status_code=404, detail="Artefaktdatei fehlt")
    return FileResponse(path, filename=row["artifact_name"] or path.name)
