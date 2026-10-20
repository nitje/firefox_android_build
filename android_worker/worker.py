"""Independent Firefox for Android build queue. Builds ARM64 Fenix debug/release APKs.

Manager ↔ worker uses an atomic JSON queue on the existing shared volume. Never
writes the Windows worker_status.json or touches the Windows VM/Dev Drive.
"""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Code version is fixed at image build time. An environment variable MUST NOT
# turn an outdated worker into an apparently current worker.
VERSION = "0.0.16"
CONFIGURED_PROJECT_VERSION = VERSION  # deployed code, not the immutable toolchain image ENV
WORKER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
WORKER_HOSTNAME = platform.node()
WORKER_STARTED_AT = datetime.now(timezone.utc).isoformat()
SHARED = Path(os.getenv("SHARED_DIR", "/data/shared"))
WORK = Path(os.getenv("BUILD_DIR", "/work"))
SIGNING = Path(os.getenv("SIGNING_DIR", "/signing"))
QUEUE = SHARED / "android"
STATUS = SHARED / "android_worker_status.json"
VER_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
BUILDER_UID = 1000
BUILDER_GID = 1000
BUSY_ID: str | None = None
BUSY_PHASE = "IDLE"
BUSY_DETAIL = "Android-Worker bereit; wartet auf Build-Aufträge."
STATE_LOCK = threading.Lock()


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def free_gib() -> float:
    return round(shutil.disk_usage(WORK).free / 1024**3, 1)


def memory_gib() -> float:
    try:
        meminfo = Path("/proc/meminfo").read_text()
        raw = re.search(r"^MemTotal:\s*(\d+) kB", meminfo, re.M)
        return round(int(raw.group(1)) / 1024**2, 1) if raw else 0.0
    except OSError:
        return 0.0


def basic_ready() -> tuple[bool, str]:
    if platform.machine().lower() not in ("x86_64", "amd64"):
        return False, "Android-Worker benötigt Linux x86_64."
    if free_gib() < 30:
        return False, "Zu wenig Platz unter /work (mindestens 30 GiB frei; empfohlen deutlich mehr)."
    for name in ("tar", "git", "clang", "java", "python3", "make"):
        if not shutil.which(name):
            return False, f"Tool {name} fehlt im Android-Worker."
    return True, "Android-Worker bereit; SDK/NDK/JDK werden bei Bedarf durch Mozilla bootstrap ergänzt."


def heartbeat() -> None:
    global BUSY_ID, BUSY_PHASE, BUSY_DETAIL
    while True:
        try:
            ok, detail = basic_ready()
            with STATE_LOCK:
                state, job_id, description = BUSY_PHASE, BUSY_ID, BUSY_DETAIL
            payload = {"ready": ok, "state": state if ok else "NOT_READY",
                       "detail": description if ok and state != "IDLE" else detail,
                       "job_id": job_id, "worker_version": VERSION,
                       "configured_project_version": CONFIGURED_PROJECT_VERSION,
                       "worker_hostname": WORKER_HOSTNAME,
                       "worker_started_at": WORKER_STARTED_AT,
                       "worker_pid": os.getpid(), "worker_sha256": WORKER_SHA256,
                       "free_gib": free_gib(), "memory_gib": memory_gib(),
                       "cpu_count": os.cpu_count(), "toolchain": "Linux / Mozilla mach + Gradle / Fenix",
                       "queue_visible": len(list((QUEUE / "jobs").glob("*.json"))),
                       "updated_at": stamp()}
            atomic(STATUS, payload)
        except Exception as exc:
            print(f"HEARTBEAT: {exc}", flush=True)
        time.sleep(10)


def phase(status: str, job_id: str | None, detail: str, **extra) -> None:
    global BUSY_ID, BUSY_PHASE, BUSY_DETAIL
    with STATE_LOCK:
        BUSY_PHASE, BUSY_ID, BUSY_DETAIL = status, job_id, detail
    if job_id:
        atomic(SHARED / "status" / f"{job_id}.json",
               {"id": job_id, "status": status, "updated_at": stamp(), "detail": detail,
                "error": extra.get("error"), "artifact_name": extra.get("artifact_name"),
                "artifact_path": extra.get("artifact_path")})


def log(job_id: str, message: str) -> None:
    line = f"{stamp()} ANDROID: {message}"
    print(line, flush=True)
    path = SHARED / "logs" / f"{job_id}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def as_builder() -> None:
    # Only run arbitrary Mozilla source/build commands as unprivileged UID.
    os.setgid(BUILDER_GID)
    os.setuid(BUILDER_UID)


def env_for_build(config: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # Keep HOME equal to the passwd home of UID 1000.  rustup rejects a
    # mismatching HOME/euid-home pair, while its large/persistent payloads still
    # live below /work via CARGO_HOME/RUSTUP_HOME.
    persistent_home = WORK / "home"
    env.update({"HOME": "/home/builder", "MOZBUILD_STATE_PATH": str(WORK / "state"),
                "XDG_CACHE_HOME": str(persistent_home / ".cache"),
                "CARGO_HOME": str(persistent_home / ".cargo"),
                "RUSTUP_HOME": str(persistent_home / ".rustup"), "CI": "1"})
    env.pop("MOZCONFIG", None)
    if config:
        env["MOZCONFIG"] = str(config)
    # Bootstrap installs the release-compatible JDK/SDK/NDK in the persistent
    # MOZBUILD_STATE_PATH.  Put an already installed Rust toolchain on PATH too,
    # otherwise a later bootstrap run can incorrectly report "Could not find a
    # Rust compiler" although rustup succeeded during the previous attempt.
    state = WORK / "state"
    path_parts: list[str] = []
    cargo_bin = persistent_home / ".cargo" / "bin"
    if cargo_bin.is_dir():
        path_parts.append(str(cargo_bin))
    jdks = sorted((state / "jdk").glob("jdk-*")) if (state / "jdk").is_dir() else []
    if jdks:
        env["JAVA_HOME"] = str(jdks[-1])
        path_parts.append(str(jdks[-1] / "bin"))
    sdk = state / "android-sdk-linux"
    if sdk.is_dir():
        env["ANDROID_HOME"] = str(sdk)
        env["ANDROID_SDK_ROOT"] = str(sdk)
        path_parts.append(str(sdk / "platform-tools"))
    ndks = sorted(p for p in state.glob("android-ndk-*") if p.is_dir())
    if ndks:
        env["ANDROID_NDK_ROOT"] = str(ndks[-1])
        env["ANDROID_NDK_HOME"] = str(ndks[-1])
    if path_parts:
        env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])
    return env


def run(job_id: str, argv: list[str], cwd: Path, *, config: Path | None = None,
        extra_env: dict[str, str] | None = None, display_argv: list[str] | None = None) -> None:
    shown = display_argv if display_argv is not None else argv
    log(job_id, "$ " + " ".join(shown))
    env = env_for_build(config)
    if extra_env:
        env.update(extra_env)
    with subprocess.Popen(argv, cwd=cwd, env=env, preexec_fn=as_builder,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          bufsize=1, text=True, errors="replace") as proc:
        assert proc.stdout
        with (SHARED / "logs" / f"{job_id}.log").open("a", encoding="utf-8") as out:
            for line in proc.stdout:
                out.write(line)
                out.flush()
                print(line.rstrip(), flush=True)
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"{argv[0]} {' '.join(shown[1:])} fehlgeschlagen (Exitcode {rc}). Siehe Build-Log.")


def transient_gradle_network_error(output: str) -> bool:
    """Return True only for dependency-download failures worth retrying.

    Do not retry compilation/configuration/signing errors: a retry there would
    only hide a real problem.  Mozilla Maven/CDN can occasionally return a
    transient 5xx/429 response while Gradle resolves Fenix dependencies.
    """
    text = output.lower()
    patterns = (
        "received status code 429",
        "received status code 500",
        "received status code 502",
        "received status code 503",
        "received status code 504",
        "service unavailable",
        "connection reset",
        "connection timed out",
        "connect timed out",
        "read timed out",
        "sockettimeoutexception",
        "temporary failure in name resolution",
        "could not resolve host",
        "network is unreachable",
    )
    # Keep this narrow: require Gradle/Maven-style dependency resolution context
    # in addition to the transient network symptom.
    dependency_context = (
        "could not resolve" in text
        or "could not get resource" in text
        or "could not get 'http" in text
        or "maven" in text
    )
    return dependency_context and any(pattern in text for pattern in patterns)


def run_gradle_with_retry(job_id: str, argv: list[str], cwd: Path, *, config: Path | None = None,
                          attempts: int = 4) -> None:
    """Run a Fenix Gradle command and retry only transient repository failures."""
    delays = (10, 30, 60)
    for attempt in range(1, attempts + 1):
        offset = log_offset(job_id)
        try:
            run(job_id, argv, cwd, config=config)
            return
        except RuntimeError:
            output = log_since(job_id, offset)
            if attempt >= attempts or not transient_gradle_network_error(output):
                raise
            delay = delays[min(attempt - 1, len(delays) - 1)]
            log(job_id,
                f"Gradle-Abhängigkeitsserver vorübergehend nicht erreichbar; "
                f"Versuch {attempt}/{attempts} fehlgeschlagen. Neuer Versuch in {delay}s.")
            time.sleep(delay)


def run_capture(job_id: str, argv: list[str], cwd: Path, *, config: Path | None = None,
                extra_env: dict[str, str] | None = None, display_argv: list[str] | None = None) -> str:
    shown = display_argv if display_argv is not None else argv
    log(job_id, "$ " + " ".join(shown))
    env = env_for_build(config)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(argv, cwd=cwd, env=env, preexec_fn=as_builder,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace")
    output = proc.stdout or ""
    with (SHARED / "logs" / f"{job_id}.log").open("a", encoding="utf-8") as out:
        out.write(output)
        out.flush()
    if output:
        print(output.rstrip(), flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} {' '.join(shown[1:])} fehlgeschlagen (Exitcode {proc.returncode}). Siehe Build-Log.")
    return output


def ensure_source(job_id: str, version: str) -> Path:
    root = WORK / "sources" / f"firefox-{version}"
    if (root / "mach").is_file():
        log(job_id, f"Persistenter Source-Cache verwendet: {root}")
        return root
    archive = WORK / "archives" / f"firefox-{version}.source.tar.xz"
    if not archive.is_file() or archive.stat().st_size < 1024:
        url = f"https://archive.mozilla.org/pub/firefox/releases/{version}/source/firefox-{version}.source.tar.xz"
        log(job_id, f"Mozilla-Source laden: {url}")
        tmp = archive.with_suffix(archive.suffix + ".partial")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Firefox-Webbrowser-Build/0.0.16"}), timeout=120) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out, length=4*1024*1024)
        if tmp.stat().st_size < 1024:
            raise RuntimeError("Mozilla-Source-Download ist leer.")
        tmp.replace(archive)

    # The supervisor runs as root, while tar/mach intentionally run as the
    # unprivileged builder (1000:1000).  A migrated bind mount or a root-created
    # download may therefore contain a valid archive that builder cannot read.
    # Repair only this small immutable input file instead of recursively chowning
    # the large persistent Android cache/source tree on every worker start.
    try:
        os.chown(archive, BUILDER_UID, BUILDER_GID)
        archive.chmod(0o644)
    except OSError as exc:
        raise RuntimeError(f"Mozilla-Source-Archiv kann nicht für den Android-Builder freigegeben werden: {exc}") from exc

    log(job_id, f"Mozilla-Source entpacken: {archive.name}")
    extract = WORK / "sources" / f".extract-firefox-{version}"
    # An interrupted previous extraction must not poison an otherwise valid retry.
    if root.exists() and not (root / "mach").is_file():
        shutil.rmtree(root)
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir(parents=True, exist_ok=True)
    os.chown(extract, BUILDER_UID, BUILDER_GID)
    run(job_id, ["tar", "-xf", str(archive), "-C", str(extract)], WORK)
    mach_files = [p for p in extract.glob("*/mach") if p.is_file()]
    if not mach_files:
        raise RuntimeError("Mozilla-Archiv enthält keinen erwarteten mozilla-central/mach-Quellbaum.")
    mach_files[0].parent.rename(root)
    shutil.rmtree(extract)
    return root



def validate_android_about_config(raw: object) -> dict:
    """Validate the small native Fenix preference snapshot carried by the job."""
    default = {
        "schema": 1,
        "sync_server": {
            "account_server": {"enabled": False, "value": "https://accounts.firefox.com"},
            "sync_server": {"enabled": False, "value": "https://token.services.mozilla.com/1.0/sync/1.5"},
        },
    }
    if raw is None:
        return default
    if not isinstance(raw, dict) or not isinstance(raw.get("sync_server"), dict):
        raise ValueError("Android-about:config benötigt den Bereich Sync Server.")
    result = {"schema": 1, "sync_server": {}}
    for key, label in (("account_server", "Mozilla-Kontoserver"), ("sync_server", "Sync-Server")):
        item = raw["sync_server"].get(key)
        if not isinstance(item, dict) or type(item.get("enabled")) is not bool:
            raise ValueError(f"{label}: ungültiger Checkbox-Wert.")
        value = item.get("value")
        if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
            raise ValueError(f"{label}: ungültige URL.")
        value = value.strip()
        if item["enabled"]:
            if any(ch.isspace() for ch in value):
                raise ValueError(f"{label}: aktivierter Wert benötigt eine gültige http(s)-URL ohne Zugangsdaten.")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{label}: aktivierter Wert benötigt eine gültige http(s)-URL ohne Zugangsdaten.")
        result["sync_server"][key] = {"enabled": item["enabled"], "value": value}
    return result


def kotlin_string_literal(value: str) -> str:
    return '"' + (value.replace('\\', '\\\\').replace('"', '\\"')
                  .replace('$', '\\$').replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t')) + '"'


def _replace_fenix_string_default(text: str, variable: str, resource_key: str, value: str) -> str:
    pattern = re.compile(
        rf"(var\s+{re.escape(variable)}\s+by\s+stringPreference\(\s*"
        rf"appContext\.getPreferenceKey\(R\.string\.{re.escape(resource_key)}\),\s*"
        rf"default\s*=\s*)\"\"",
        re.S,
    )
    literal = kotlin_string_literal(value)
    patched, count = pattern.subn(lambda match: match.group(1) + literal, text, count=1)
    if count != 1:
        raise RuntimeError(
            f"Fenix-Quelle enthält den erwarteten {variable}-Default nicht; Sync-Override wird nicht stillschweigend übersprungen."
        )
    return patched


def apply_android_sync_defaults(job_id: str, root: Path, version: str, raw_config: object) -> dict:
    """Apply build defaults to Fenix Settings.kt while keeping a pristine per-release baseline.

    Fenix' FxaServer consumes Settings.overrideFxAServer and
    Settings.overrideSyncTokenServer.  Changing the default here makes the
    build start with the selected endpoints, while a user-set SharedPreference
    from the hidden Sync Debug UI can still override the build default later.
    """
    config = validate_android_about_config(raw_config)
    target = root / "mobile" / "android" / "fenix" / "app" / "src" / "main" / "java" / "org" / "mozilla" / "fenix" / "utils" / "Settings.kt"
    if not target.is_file():
        raise RuntimeError(f"Fenix Settings.kt nicht gefunden: {target}")
    baseline_dir = WORK / "state" / "android-source-baselines" / version
    baseline = baseline_dir / "Settings.kt"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    os.chown(baseline_dir, BUILDER_UID, BUILDER_GID)
    if not baseline.is_file():
        shutil.copyfile(target, baseline)
        os.chown(baseline, BUILDER_UID, BUILDER_GID)
        log(job_id, "Fenix Settings.kt: unveränderte Release-Basis für wiederholbare Android-Builds gesichert.")
    original = baseline.read_text(encoding="utf-8")
    patched = original
    account = config["sync_server"]["account_server"]
    token = config["sync_server"]["sync_server"]
    # Always assert both expected source anchors.  Even when disabled this
    # prevents a future Fenix source-layout change from silently producing a
    # build whose UI configuration is no longer connected to native settings.
    patched = _replace_fenix_string_default(
        patched, "overrideFxAServer", "pref_key_override_fxa_server",
        account["value"] if account["enabled"] else "",
    )
    patched = _replace_fenix_string_default(
        patched, "overrideSyncTokenServer", "pref_key_override_sync_tokenserver",
        token["value"] if token["enabled"] else "",
    )
    if target.read_text(encoding="utf-8") != patched:
        target.write_text(patched, encoding="utf-8")
        os.chown(target, BUILDER_UID, BUILDER_GID)
    active = []
    if account["enabled"]:
        active.append(f"Mozilla-Kontoserver={account['value']}")
    if token["enabled"]:
        active.append(f"Sync-Server={token['value']}")
    log(job_id, "Android Sync Server: " + ("; ".join(active) if active else "keine Overrides; Mozilla/Fenix-Defaults unverändert."))
    return config

def android_mozconfig_text(objdir: Path, *, disable_onnx: bool = False) -> str:
    lines = [
        "# Firefox for Android / Fenix / ARM64 (no desktop policies)",
        "ac_add_options --enable-project=mobile/android",
        "ac_add_options --target=aarch64",
    ]
    if disable_onnx:
        # Release source archives can occasionally reference a local-toolchain
        # hash that was never published in Taskcluster.  Disabling ONNX is a
        # narrow fallback for local Android builds only; normal bootstrap remains
        # the first attempt.
        lines.append("ac_add_options --without-onnx-runtime")
    lines.append(f"mk_add_options MOZ_OBJDIR={objdir}")
    return "\n".join(lines) + "\n"


def write_android_mozconfig(config: Path, objdir: Path, *, disable_onnx: bool = False) -> None:
    config.write_text(android_mozconfig_text(objdir, disable_onnx=disable_onnx), encoding="utf-8")
    os.chown(config, BUILDER_UID, BUILDER_GID)


def bootstrap_marker(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def log_offset(job_id: str) -> int:
    path = SHARED / "logs" / f"{job_id}.log"
    try:
        return path.stat().st_size
    except OSError:
        return 0


def log_since(job_id: str, offset: int, max_bytes: int = 512 * 1024) -> str:
    """Read only output written by the current command attempt.

    Jobs are retried with the same log file. Looking at a generic log tail can
    therefore mistake an older ONNX failure for the current error.
    """
    path = SHARED / "logs" / f"{job_id}.log"
    try:
        with path.open("rb") as f:
            f.seek(max(0, offset))
            data = f.read(max_bytes)
            if f.read(1):
                # Keep the newest portion if a single command was exceptionally noisy.
                f.seek(max(offset, path.stat().st_size - max_bytes))
                data = f.read(max_bytes)
            return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def missing_android_onnx_toolchain(output: str, *, require_x86_64: bool = False) -> bool:
    if require_x86_64:
        return "toolchain-onnxruntime-x86_64-linux-android" in output
    return (
        ("Could not find artifacts for a toolchain build named `toolchain-onnxruntime-" in output
         and "linux-android`" in output)
        or ("Cannot find onnxruntime for" in output and "android" in output.lower())
        or "Cannot find bootstrapped onnxruntime" in output
    )


def bootstrap_android(job_id: str, root: Path, config: Path, objdir: Path, marker: Path) -> None:
    """Prepare Mozilla's Android toolchain.

    Firefox release source archives can reference a Taskcluster ONNX artifact
    that is no longer resolvable by `mach bootstrap`. On an x86_64 build host,
    bootstrap asks for the x86_64 Android ONNX toolchain even when the actual
    mozconfig targets ARM64. That single late bootstrap failure is safe to hand
    over to the real ARM64 configure pass; every other bootstrap failure stays
    fatal.
    """
    write_android_mozconfig(config, objdir, disable_onnx=False)
    previous = bootstrap_marker(marker)
    if previous.get("version") == root.name.removeprefix("firefox-") and previous.get("sufficient"):
        result = previous.get("result", "complete")
        log(job_id, f"Mozilla bootstrap aus persistentem Cache verwendet ({result}).")
        return

    command = ["./mach", "--no-interactive", "bootstrap",
               "--application-choice=GeckoView/Firefox for Android"]
    offset = log_offset(job_id)
    try:
        run(job_id, command, root, config=config)
        result = "complete"
    except RuntimeError:
        output = log_since(job_id, offset)
        if not missing_android_onnx_toolchain(output, require_x86_64=True):
            raise
        # The log shows SDK/NDK/JDK/Rust and Mozilla host toolchains were already
        # prepared before bootstrap reached this host/x86_64 ONNX artifact. Do
        # not rerun the same command with a mozconfig switch it does not honor.
        result = "host-x86_64-onnx-unavailable"
        log(job_id,
            "Mozilla bootstrap hat SDK/NDK/JDK/Rust und die übrigen Toolchains vorbereitet, "
            "kann aber das x86_64-Android-ONNX-Artefakt dieses Release-Stands nicht auflösen. "
            "Der Worker fährt deshalb mit der echten ARM64-Configure-Prüfung fort; ONNX wird "
            "nur dann deaktiviert, wenn auch dort das ARM64-Artefakt fehlt.")

    atomic(marker, {"version": root.name.removeprefix("firefox-"),
                    "completed": stamp(), "sufficient": True, "result": result})


def configure_android(job_id: str, root: Path, config: Path, objdir: Path, marker: Path) -> bool:
    """Configure ARM64 and preserve ONNX whenever its ARM64 artifact is available.

    Returns True only when the actual ARM64 configure pass had to disable the
    native ONNX runtime.
    """
    previous = bootstrap_marker(marker)
    disable_onnx = bool(previous.get("onnx_disabled"))
    write_android_mozconfig(config, objdir, disable_onnx=disable_onnx)

    if disable_onnx:
        log(job_id, "ARM64-Configure: gespeicherter ONNX-Fallback für diesen Firefox-Release wird wiederverwendet.")

    offset = log_offset(job_id)
    try:
        run(job_id, ["./mach", "configure"], root, config=config)
    except RuntimeError:
        output = log_since(job_id, offset)
        if disable_onnx or not missing_android_onnx_toolchain(output):
            raise
        disable_onnx = True
        log(job_id,
            "Auch die echte ARM64-Configure-Prüfung kann das passende Android-ONNX-Artefakt "
            "nicht beziehen. Nur für diesen lokalen Android-Build wird native ONNX jetzt deaktiviert; "
            "alle übrigen Firefox-/GeckoView-Funktionen bleiben unverändert.")
        write_android_mozconfig(config, objdir, disable_onnx=True)
        run(job_id, ["./mach", "configure"], root, config=config)

    atomic(marker, {"version": root.name.removeprefix("firefox-"),
                    "configured": stamp(), "onnx_disabled": disable_onnx})
    return disable_onnx


def _version_key(path: Path) -> tuple[int, ...]:
    parts = re.findall(r"\d+", path.name)
    return tuple(int(x) for x in parts)


def find_apksigner() -> Path:
    sdk = WORK / "state" / "android-sdk-linux" / "build-tools"
    if sdk.is_dir():
        for folder in sorted((p for p in sdk.iterdir() if p.is_dir()), key=_version_key, reverse=True):
            candidate = folder / "apksigner"
            if candidate.is_file():
                return candidate
    direct = shutil.which("apksigner")
    if direct:
        return Path(direct)
    raise RuntimeError("Android SDK apksigner wurde nicht gefunden.")


def release_signing_credentials() -> tuple[Path, str, str]:
    keystore = SIGNING / "release.p12"
    secret_path = SIGNING / "secret.json"
    if not keystore.is_file() or not secret_path.is_file():
        raise RuntimeError("Kein Android-Release-Keystore vorhanden. Im Manager zuerst erzeugen oder importieren.")
    try:
        secret = json.loads(secret_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Android-Signing-Metadaten sind unlesbar.") from exc
    alias = secret.get("alias")
    password = secret.get("store_password")
    if not isinstance(alias, str) or not alias or not isinstance(password, str) or not password:
        raise RuntimeError("Android-Signing-Metadaten sind unvollständig.")
    return keystore, alias, password


def normalized_fingerprint(value: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    return ":".join(compact[i:i+2] for i in range(0, len(compact), 2))


def sign_release_apk(job_id: str, unsigned_apk: Path, signed_apk: Path, expected_fingerprint: str) -> None:
    keystore, alias, password = release_signing_credentials()
    apksigner = find_apksigner()
    signed_apk.parent.mkdir(parents=True, exist_ok=True)
    # The worker/supervisor runs as root, while apksigner deliberately runs as
    # builder (1000:1000).  A root-owned 0755 /work/signed directory makes even
    # apksigner's initial Files.deleteIfExists(<apk>.idsig) fail with EACCES,
    # including when the sidecar does not exist yet.  Repair the directory
    # itself before dropping privileges; cleaning only stale files is not enough.
    try:
        os.chown(signed_apk.parent, BUILDER_UID, BUILDER_GID)
        signed_apk.parent.chmod(0o775)
    except OSError as exc:
        raise RuntimeError(f"Signing-Arbeitsverzeichnis kann nicht für den Android-Builder freigegeben werden: {signed_apk.parent}: {exc}") from exc
    # apksigner may leave a V4 sidecar (<apk>.idsig).  Older/failed jobs can
    # leave this file owned by another uid.  The worker itself runs as root,
    # so remove both outputs before dropping privileges for apksigner.
    stale_outputs = [signed_apk, Path(str(signed_apk) + ".idsig")]
    for stale in stale_outputs:
        if not stale.exists() and not stale.is_symlink():
            continue
        try:
            stale.chmod(0o644)
        except OSError:
            pass
        try:
            stale.unlink()
        except OSError as exc:
            raise RuntimeError(f"Alte Signing-Ausgabedatei konnte nicht entfernt werden: {stale}: {exc}") from exc
    secret_env = {"ANDROID_KEYSTORE_PASSWORD": password, "ANDROID_KEY_PASSWORD": password}
    sign_cmd = [str(apksigner), "sign", "--ks", str(keystore), "--ks-type", "PKCS12",
                "--ks-key-alias", alias, "--ks-pass", "env:ANDROID_KEYSTORE_PASSWORD",
                "--key-pass", "env:ANDROID_KEY_PASSWORD", "--out", str(signed_apk), str(unsigned_apk)]
    run(job_id, sign_cmd, WORK, extra_env=secret_env)
    output = run_capture(job_id, [str(apksigner), "verify", "--verbose", "--print-certs", str(signed_apk)], WORK)
    match = re.search(r"certificate SHA-256 digest:\s*([0-9A-Fa-f:]+)", output, re.IGNORECASE)
    if not match:
        raise RuntimeError("Signierte Release-APK konnte nicht auf ihren Zertifikat-Fingerprint geprüft werden.")
    actual = normalized_fingerprint(match.group(1))
    expected = normalized_fingerprint(expected_fingerprint)
    if actual != expected:
        raise RuntimeError(f"Release-APK wurde mit unerwartetem Signing-Key signiert: {actual}; erwartet {expected}.")
    log(job_id, f"Release-Signatur geprüft: SHA-256 {actual}")


def select_arm64_apk(folder: Path) -> Path:
    candidates = sorted(folder.rglob("*.apk"), key=lambda f: f.stat().st_mtime, reverse=True) if folder.is_dir() else []
    # Prefer the explicit ABI split over a universal APK when both exist.
    candidates.sort(key=lambda p: ("arm64" not in p.name.lower(), "universal" in p.name.lower(), -p.stat().st_mtime))
    for candidate in candidates:
        try:
            with zipfile.ZipFile(candidate) as apk:
                if any(n.startswith("lib/arm64-v8a/") and n.endswith(".so") for n in apk.namelist()):
                    return candidate
        except zipfile.BadZipFile:
            continue
    raise RuntimeError("Keine Fenix-APK mit nativen lib/arm64-v8a/*.so-Bibliotheken gefunden.")



def cleanup_workspace(job: dict) -> None:
    """Remove version-specific Android build data, never shared SDK/NDK/Gradle caches or signing keys."""
    job_id = str(job["id"])
    version = str(job["version"])
    if not VER_RE.fullmatch(version) or job.get("platform") != "android-arm64":
        raise ValueError("Ungültiger Android-Cleanup-Job (Version/Plattform).")
    if job.get("project_version") != VERSION:
        raise ValueError("Cleanup-Job stammt aus anderem Projektstand; bitte erneut auslösen.")

    phase("BUILDING", job_id, f"Firefox {version} Android ARM64: versionsbezogene Workerdaten werden gelöscht")
    targets = [
        WORK / "sources" / f"firefox-{version}",
        WORK / "archives" / f"firefox-{version}.source.tar.xz",
        WORK / "objs" / f"firefox-{version}-android-arm64",
        WORK / "configs" / f"android-aarch64-{version}.mozconfig",
        WORK / "state" / f"android-bootstrap-{version}.json",
        WORK / "state" / f"android-configure-{version}.json",
        WORK / "state" / "android-source-baselines" / version,
    ]
    removed: list[str] = []
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(str(target))
        elif target.exists():
            target.unlink()
            removed.append(str(target))

    signed_dir = WORK / "signed"
    if signed_dir.is_dir():
        for pattern in (
            f"firefox-{version}-android-arm64-*.apk",
            f"firefox-{version}-android-arm64-*.apk.idsig",
        ):
            for candidate in signed_dir.glob(pattern):
                try:
                    candidate.chmod(0o644)
                except OSError:
                    pass
                candidate.unlink(missing_ok=True)
                removed.append(str(candidate))

    # Intentionally retained: WORK/state Android SDK/NDK/JDK/toolchains,
    # WORK/home Gradle/cargo caches, and /signing release key.
    detail = (f"Android-Workerdaten für Firefox {version} gelöscht ({len(removed)} Pfade). "
              "Gemeinsame SDK/NDK/Gradle-Caches und Release-Keystore bleiben erhalten.")
    log(job_id, detail)
    phase("FINISHED", job_id, detail, removed_paths=len(removed))

def build(job: dict) -> None:
    job_id = str(job["id"])
    version = str(job["version"])
    flavor = str(job.get("flavor") or "debug").lower()
    if not VER_RE.fullmatch(version) or job.get("platform") != "android-arm64" or flavor not in {"debug", "release"}:
        raise ValueError("Ungültiger Android-Job (Version/Plattform/Profil).")
    if job.get("project_version") != VERSION:
        raise ValueError("Job stammt aus anderem Projektstand; erneut über Manager einreihen.")
    expected_fingerprint = str(job.get("signing_fingerprint") or "")
    if flavor == "release" and not expected_fingerprint:
        raise ValueError("Release-Job enthält keinen erwarteten Signing-Fingerprint.")

    profile_label = "Debug" if flavor == "debug" else "Release"
    phase("BUILDING", job_id, f"Firefox {version} Android ARM64 {profile_label}: Source herunterladen")
    root = ensure_source(job_id, version)
    phase("BUILDING", job_id, f"Firefox {version} Android ARM64 {profile_label}: Fenix Sync-Server-Konfiguration")
    android_about = apply_android_sync_defaults(job_id, root, version, job.get("android_about_config"))
    phase("BUILDING", job_id, f"Firefox {version} Android ARM64 {profile_label}: Toolchain/bootstrap")
    config = WORK / "configs" / f"android-aarch64-{version}.mozconfig"
    objdir = WORK / "objs" / f"firefox-{version}-android-arm64"
    bootstrap_state = WORK / "state" / f"android-bootstrap-{version}.json"
    configure_state = WORK / "state" / f"android-configure-{version}.json"
    bootstrap_android(job_id, root, config, objdir, bootstrap_state)
    phase("BUILDING", job_id, f"Firefox {version} Android ARM64 {profile_label}: Configure")
    onnx_disabled = configure_android(job_id, root, config, objdir, configure_state)
    if onnx_disabled:
        log(job_id, f"Buildprofil: ARM64 {profile_label}; native ONNX-Runtime nur wegen fehlendem ARM64-Mozilla-Release-Artefakt deaktiviert.")
    else:
        log(job_id, f"Buildprofil: ARM64 {profile_label}; native ONNX-Runtime bleibt aktiviert.")
    phase("BUILDING", job_id, f"Firefox {version} Android ARM64 {profile_label}: GeckoView kompilieren")
    run(job_id, ["./mach", "build"], root, config=config)

    if flavor == "debug":
        phase("BUILDING", job_id, f"Firefox {version} Android ARM64: Fenix-Debug-APK erzeugen")
        run_gradle_with_retry(job_id, ["./mach", "gradle", "fenix:assembleDebug"], root, config=config)
        output_dir = objdir / "gradle" / "build" / "mobile" / "android" / "fenix" / "app" / "outputs" / "apk" / "debug"
        chosen = select_arm64_apk(output_dir)
        source_apk = chosen
        result_detail = "ARM64 Fenix-Debug-APK erstellt"
    else:
        phase("BUILDING", job_id, f"Firefox {version} Android ARM64: Fenix-Release-APK erzeugen")
        # Fenix locally applies the debug signing key to production variants unless
        # disableDebugSigning is set. Build unsigned, then sign with this installation's
        # persistent private release key.
        run_gradle_with_retry(job_id, ["./mach", "gradle", "-PdisableDebugSigning", "fenix:assembleRelease"], root, config=config)
        output_dir = objdir / "gradle" / "build" / "mobile" / "android" / "fenix" / "app" / "outputs" / "apk" / "release"
        chosen = select_arm64_apk(output_dir)
        phase("PACKAGE", job_id, f"Firefox {version}: Release-APK mit eigenem Keystore signieren")
        signed_stage = WORK / "signed" / f"firefox-{version}-android-arm64-release-{job_id[:8]}.apk"
        sign_release_apk(job_id, chosen, signed_stage, expected_fingerprint)
        source_apk = signed_stage
        result_detail = "ARM64 Fenix-Release-APK erstellt und mit eigenem Keystore signiert"

    phase("PACKAGE", job_id, f"Firefox {version}: APK wird ins Artefakt-Verzeichnis kopiert")
    name = f"firefox-{version}-fenix-android-arm64-{flavor}-{job_id[:8]}.apk"
    dst = SHARED / "artifacts" / "android" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".apk.partial")
    shutil.copyfile(source_apk, tmp)
    tmp.replace(dst)
    rel = dst.relative_to(SHARED).as_posix()
    log(job_id, f"FERTIG: {name} ({dst.stat().st_size} bytes)")
    phase("FINISHED", job_id, result_detail, artifact_name=name, artifact_path=rel)


def prepare() -> None:
    for folder in (WORK, WORK / "home", WORK / "home" / ".cache", WORK / "sources",
                   WORK / "archives", WORK / "state", WORK / "configs", WORK / "objs",
                   WORK / "signed",
                   QUEUE / "jobs", QUEUE / "processing", QUEUE / "failed",
                   SHARED / "logs", SHARED / "status", SHARED / "artifacts" / "android"):
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_relative_to(WORK):
            os.chown(folder, BUILDER_UID, BUILDER_GID)
            if folder == WORK / "signed":
                folder.chmod(0o775)
    if platform.machine().lower() not in ("x86_64", "amd64"):
        print("Android worker requires an x86_64 Linux host", file=sys.stderr, flush=True)


def newer_worker_staged() -> bool:
    """Leave only between builds so supervisor restarts with manager's new code."""
    folder = SHARED / "android" / "updates"
    try:
        manifest = json.loads((folder / "worker_manifest.json").read_text(encoding="utf-8"))
        payload = (folder / "worker.py").read_bytes()
        source_version = re.search(rb'^VERSION = "([0-9.]+)"$', payload, re.M)
        return (source_version is not None
                and source_version.group(1).decode() == manifest.get("version")
                and manifest.get("sha256") == hashlib.sha256(payload).hexdigest()
                and manifest.get("sha256") != WORKER_SHA256)
    except (OSError, ValueError, TypeError):
        return False


def main() -> None:
    prepare()
    # Jobs in processing/ were interrupted by a worker/container restart.
    # Do not lose them silently or re-execute them with stale input: expose
    # FAILED so the manager's "Neu einreihen" action can recover explicitly.
    for lost in sorted((QUEUE / "processing").glob("*.json")):
        bid = lost.stem
        try:
            status_file = SHARED / "status" / f"{bid}.json"
            if status_file.is_file():
                old_status = json.loads(status_file.read_text(encoding="utf-8-sig"))
                rel = str(old_status.get("artifact_path") or "")
                if old_status.get("status") == "FINISHED" and rel and (SHARED / rel).is_file():
                    log(bid, "Bereits abgeschlossenes APK-Artefakt nach Neustart bestätigt.")
                    lost.unlink()
                    continue
            log(bid, "Worker wurde während dieses Jobs neu gestartet; Build bitte erneut einreihen.")
            phase("FAILED", bid, "Android-Worker wurde während des Builds neu gestartet; bitte erneut einreihen.",
                  error="Android-Worker-Neustart vor erfolgreichem Abschluss")
            lost.replace(QUEUE / "failed" / lost.name)
        except OSError as exc:
            print(f"RECOVERY {bid}: {exc}", file=sys.stderr, flush=True)
    threading.Thread(target=heartbeat, daemon=True, name="android-heartbeat").start()
    print(f"Firefox Android Worker Code v{VERSION} gestartet; Hostname={WORKER_HOSTNAME} (eigene Queue).", flush=True)
    while True:
        # Never reload while a build is in progress; after completion we loop
        # back here. The supervisor will restart with verified staged code.
        if newer_worker_staged():
            print("ANDROID: neue Worker-Version bereitgestellt; sicherer Neustart im IDLE", flush=True)
            return
        jobs = sorted((QUEUE / "jobs").glob("*.json"), key=lambda x: x.stat().st_mtime)
        if not jobs:
            time.sleep(3)
            continue
        path = jobs[0]
        ready, _ = basic_ready()
        if not ready:
            # Cleanup must still be able to run when /work is nearly full;
            # normal builds remain blocked by the regular readiness checks.
            try:
                queued_preview = json.loads(path.read_text(encoding="utf-8"))
                cleanup_can_run = queued_preview.get("kind") == "cleanup_workspace"
            except (OSError, ValueError, TypeError):
                cleanup_can_run = False
            if not cleanup_can_run:
                time.sleep(5)
                continue
        process = QUEUE / "processing" / path.name
        try:
            path.rename(process)
        except OSError:
            continue
        job_id = process.stem
        try:
            job = json.loads(process.read_text(encoding="utf-8"))
            if job.get("id") != job_id:
                raise ValueError("Job-ID stimmt nicht mit Dateiname überein.")
            phase("CLAIMED", job_id, "Android-Worker hat den Auftrag übernommen")
            if job.get("kind") == "cleanup_workspace":
                cleanup_workspace(job)
            else:
                build(job)
            process.unlink(missing_ok=True)
        except Exception as exc:
            log(job_id, f"FEHLER: {type(exc).__name__}: {exc}")
            phase("FAILED", job_id, str(exc), error=str(exc))
            process.replace(QUEUE / "failed" / process.name)
        finally:
            phase("IDLE", None, "Warte auf Android-Build-Aufträge")


if __name__ == "__main__":
    main()
