#!/usr/bin/env python3
"""
mac_maintenance_v7.11.py
Manutenzione completa per macOS — con automazione proattiva per Ruby.
Tutti i file di log e stato vengono salvati in ~/Documents/MacMaintenance.

Changelog:
  v7.11 (2026-10-02):
    - FIX pip: enumera TUTTI i pip candidati (pip, pip3, python3.X -m pip)
      e sceglie quello con la versione Python più alta (3.14 > 3.9)
    - Log di tutti i candidati trovati per trasparenza
    - Warning cask 'app già presente' ora suggerisce --force
  v7.10 (2026-10-02):
    - FIX pip: preferisce 'pip' a 'pip3'
    - FIX cask: diagnosi 'Failure while executing; /usr/bin/sudo' come errore sudo
    - FIX brew: warning per 'brew link did not complete successfully'
    - NUOVO: --pip-cmd per override esplicito
  v7.9 (2026-10-01):
    - --only, --auto-upgrade-pip, --notify-when, history tracking
  v7.8 (rev. 2026-10-01):
    - PATCH-1..6 (parsing pip JSON, brew --formula separato, sudo check,
      composer condizionale, sommario raggruppato, diagnosi cask)
"""

import subprocess
import os
import sys
import datetime
import time
import re
import argparse
import shutil
import json
import glob
import shlex
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------------

VERSION = "7.11"
HISTORY_MAX_ENTRIES = 30

PIP_CMD_OVERRIDE = ""

def _resolve_log_file() -> str:
    try:
        import pwd
        real_home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        real_home = os.path.expanduser("~")

    maint_folder = os.path.join(real_home, "Documents", "MacMaintenance")
    try:
        if not os.path.exists(maint_folder):
            os.makedirs(maint_folder, exist_ok=True)
        if os.path.isdir(maint_folder) and os.access(maint_folder, os.W_OK):
            return os.path.join(maint_folder, "mac_update_log.txt")
    except Exception:
        pass

    for folder in [
        os.path.join(real_home, "Documents"),
        os.path.join(real_home, "Desktop"),
        real_home,
    ]:
        if os.path.isdir(folder) and os.access(folder, os.W_OK):
            return os.path.join(folder, "mac_update_log.txt")

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "mac_update_log.txt")

LOG_FILE = _resolve_log_file()
TOOLS_STATE_FILE = LOG_FILE.replace("mac_update_log.txt", "mac_update_tools.json")
HISTORY_FILE = LOG_FILE.replace("mac_update_log.txt", "mac_update_history.json")

STDERR_IGNORELIST = [
    "updating homebrew",
    "already up-to-date",
    "npm warn using --force",
    "npm fund",
    "packages are looking for funding",
    "notice",
    "no new software available",
    "go: downloading",
    "warning: these files were overwritten",
]

UNIT_MULTIPLIERS = {
    "B": 1, "KB": 1_000, "MB": 1_000_000, "GB": 1_000_000_000,
    "KIB": 1_024, "MIB": 1_048_576, "GIB": 1_073_741_824,
}

ALL_SECTIONS = ["brew", "mas", "node", "python", "go", "macos", "tools"]


# ---------------------------------------------------------------------------
# AMBIENTE
# ---------------------------------------------------------------------------

def build_env() -> dict:
    env = os.environ.copy()
    paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]

    try:
        for p in sorted(glob.glob(os.path.expanduser("~/Library/Python/*/bin")), reverse=True):
            if os.path.isdir(p) and p not in paths:
                paths.insert(0, p)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["python3", "-m", "site", "--user-base"],
            capture_output=True, text=True, timeout=5
        )
        user_base = result.stdout.strip()
        if user_base:
            user_bin = os.path.join(user_base, "bin")
            if os.path.isdir(user_bin) and user_bin not in paths:
                paths.insert(0, user_bin)
    except Exception:
        pass

    env["PATH"] = ":".join(paths) + ":" + env.get("PATH", "")
    return env

CUSTOM_ENV = build_env()


# ---------------------------------------------------------------------------
# REPORT DI SESSIONE
# ---------------------------------------------------------------------------

def bytes_to_human(n: float) -> str:
    for unit, threshold in [("GB", 1e9), ("MB", 1e6), ("KB", 1e3)]:
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{int(n)} B"


@dataclass
class SessionReport:
    updated:     list = field(default_factory=list)
    skipped:     list = field(default_factory=list)
    errors:      list = field(default_factory=list)
    warnings:    list = field(default_factory=list)
    freed_bytes: float = 0.0
    start_time:  float = field(default_factory=time.time)
    sections:    list = field(default_factory=list)

    def add_freed(self, amount: str, unit: str):
        multiplier = UNIT_MULTIPLIERS.get(unit.upper(), 1)
        try:
            self.freed_bytes += float(amount) * multiplier
        except ValueError:
            pass

    @property
    def freed_space(self) -> str:
        return bytes_to_human(self.freed_bytes) if self.freed_bytes > 0 else ""


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def log(message: str, level: str = "INFO"):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [{level}] {message}\n")


def is_noise(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in STDERR_IGNORELIST)


# ---------------------------------------------------------------------------
# UTILITÀ
# ---------------------------------------------------------------------------

def cmd_exists(cmd: str) -> bool:
    return shutil.which(cmd, path=CUSTOM_ENV["PATH"]) is not None


def _pip_python_version(pip_cmd: list) -> str:
    """Interroga 'pip --version' per sapere a quale Python è associato."""
    try:
        r = subprocess.run(
            pip_cmd + ["--version"],
            capture_output=True, text=True, timeout=5, env=CUSTOM_ENV
        )
        text = (r.stdout or "") + " " + (r.stderr or "")
        m = re.search(r'python\s+(\d+\.\d+)', text, re.IGNORECASE)
        if m:
            return m.group(1)
        # Fallback: cerca nella path
        m = re.search(r'python(\d+\.\d+)', text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "?"


def _find_all_pythons() -> list:
    """Trova tutti i python3.X disponibili nel PATH e in /opt/homebrew/bin."""
    found = set()
    for name in ("python3", "python3.15", "python3.14", "python3.13",
                 "python3.12", "python3.11", "python3.10", "python3.9"):
        if cmd_exists(name):
            found.add(name)
    # Aggiungi anche quelli non nel PATH ma in /opt/homebrew/bin
    for p in glob.glob("/opt/homebrew/bin/python3.*"):
        base = os.path.basename(p)
        if re.match(r'^python3\.\d+$', base):
            found.add(base)
    return sorted(found, reverse=True)


def _pip_candidates() -> list:
    """
    v7.11: enumera tutti i pip candidati con la versione Python associata.
    Ritorna lista di dict: {'cmd': [...], 'python': 'X.Y', 'label': '...'}.
    Deduplica per versione Python (tiene il primo candidato per ogni versione).
    """
    candidates = []
    seen_versions = set()

    def try_add(cmd, label):
        # Salta se il binario base non esiste
        if not cmd:
            return
        first = cmd[0]
        if first.startswith("python") and not cmd_exists(first):
            return
        if not first.startswith("python") and not cmd_exists(first):
            return

        try:
            r = subprocess.run(
                cmd + ["--version"],
                capture_output=True, text=True, timeout=5, env=CUSTOM_ENV
            )
            if r.returncode != 0:
                return
            text = (r.stdout or "") + " " + (r.stderr or "")
            m = re.search(r'python\s+(\d+\.\d+)', text, re.IGNORECASE)
            if not m:
                return
            ver = m.group(1)
            if ver in seen_versions:
                return
            seen_versions.add(ver)
            candidates.append({"cmd": cmd, "python": ver, "label": label})
        except Exception:
            pass

    if cmd_exists("pip"):
        try_add(["pip"], "pip")
    if cmd_exists("pip3"):
        try_add(["pip3"], "pip3")
    for py in _find_all_pythons():
        try_add([py, "-m", "pip"], f"{py} -m pip")

    return candidates


def _get_pip_cmd() -> list:
    """
    v7.11: sceglie il pip con la versione Python più alta tra i candidati.
    Logga TUTTI i candidati trovati per trasparenza.
    """
    if PIP_CMD_OVERRIDE:
        return shlex.split(PIP_CMD_OVERRIDE)

    candidates = _pip_candidates()
    if not candidates:
        return ["python3", "-m", "pip"]

    # Log tutti i candidati
    summary = ", ".join(f"{c['label']}→Py{c['python']}" for c in candidates)
    log(f"Pip candidates trovati: {summary}")

    def _key(c):
        try:
            return tuple(int(x) for x in c["python"].split("."))
        except Exception:
            return (0, 0)

    best = max(candidates, key=_key)
    log(f"Pip scelto: {best['label']} (Python {best['python']})")
    return best["cmd"]


def has_passwordless_sudo() -> bool:
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_mas_version() -> str:
    try:
        result = subprocess.run(
            ["mas", "version"],
            capture_output=True, text=True, timeout=5,
            env=CUSTOM_ENV
        )
        m = re.search(r'(\d+\.\d+(?:\.\d+)?)', result.stdout or "")
        if m:
            return m.group(1)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["brew", "list", "--versions", "mas"],
            capture_output=True, text=True, timeout=5, env=CUSTOM_ENV
        )
        parts = (result.stdout or "").split()
        if len(parts) >= 2:
            return parts[1]
    except Exception:
        pass

    return "?"


def run(cmd: list, description: str, dry_run: bool = False, timeout: int = 300) -> tuple[str, str, int]:
    if dry_run:
        log(f"[DRY-RUN] {description}")
        return "", "", 0

    log(f"▶ {description}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env=CUSTOM_ENV, timeout=timeout
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        rc = result.returncode

        if stdout:
            lines = [
                l for l in stdout.splitlines()
                if "permission denied" not in l.lower()
                and "operation not permitted" not in l.lower()
                and "looking for funding" not in l.lower()
                and "run `npm fund`" not in l.lower()
            ]
            if lines:
                preview = lines[:5] + (["  ... [troncato] ..."] if len(lines) > 5 else [])
                log("Output:\n" + "\n".join(dict.fromkeys(preview)))

        if stderr and not is_noise(stderr):
            log(f"⚠ Stderr: {stderr[:300]}", level="WARNING")

        return stdout, stderr, rc

    except subprocess.TimeoutExpired:
        log(f"TIMEOUT dopo {timeout}s: {description}", level="ERROR")
        return "", "timeout", 1
    except FileNotFoundError:
        log(f"Comando non trovato: {cmd[0]}", level="ERROR")
        return "", "not_found", 127
    except Exception as e:
        log(f"Errore in '{description}': {e}", level="ERROR")
        return "", str(e), 1


def diagnose_error(stderr: str, description: str) -> str:
    err = (stderr or "").lower()

    if "already an app at" in err:
        m = re.search(r"'/opt/homebrew/Caskroom/([^/]+)/", stderr or "")
        cask = m.group(1) if m else "il cask"
        return (f"Cask '{cask}' non aggiornato: app già presente nella stessa posizione. "
                f"Prova: brew reinstall --cask --force {cask}")

    if "app source" in err and "is not there" in err:
        m = re.search(r"'/Applications/([^']+\.app)'", stderr or "")
        app = m.group(1) if m else "l'app"
        return (f"Cask registrato in Homebrew ma '{app}' non è presente in /Applications. "
                f"Prova: brew reinstall --cask <nome-cask>")

    if re.search(r'\bsudo\b', err) or "password is required" in err:
        return (f"Privilegi mancanti per '{description}'. "
                f"Esegui da Terminale interattivo o configura SUDO_ASKPASS.")

    if "permission denied" in err or "operation not permitted" in err:
        return ("Permessi disco negati. Controlla 'Accesso totale al disco' "
                "nelle Impostazioni di Sistema per il Terminale.")

    if re.search(r'could not resolve host|network is unreachable', err):
        return "Connessione internet assente o instabile. Verifica la rete."

    if re.search(r'\balready installed\b', err) or "conflict" in err:
        return (f"Conflitto o già installato in '{description}'. "
                f"Se necessario: brew upgrade --cask oppure brew upgrade.")

    return (f"Errore sconosciuto in '{description}'. "
            f"Controlla il log per i dettagli tecnici.")


def notify(message: str):
    try:
        script = f'display notification "{message}" with title "Mac Maintenance v{VERSION}"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, check=False,
            env=CUSTOM_ENV, timeout=5
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HISTORY
# ---------------------------------------------------------------------------

def load_history() -> dict:
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                data = json.load(f)
                if isinstance(data, dict) and "runs" in data:
                    return data
    except Exception:
        pass
    return {"version": VERSION, "runs": []}


def save_history(report: SessionReport):
    try:
        history = load_history()
        entry = {
            "timestamp": datetime.datetime.fromtimestamp(report.start_time).isoformat(timespec="seconds"),
            "duration_sec": int(time.time() - report.start_time),
            "freed_bytes": int(report.freed_bytes),
            "updated_count": len(report.updated),
            "warnings_count": len(report.warnings),
            "errors_count": len(report.errors),
            "sections": list(report.sections),
        }
        history["runs"].append(entry)
        history["runs"] = history["runs"][-HISTORY_MAX_ENTRIES:]
        history["version"] = VERSION
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log(f"Impossibile salvare history: {e}", level="WARNING")


def show_history():
    history = load_history()
    runs = history.get("runs", [])
    if not runs:
        print("Nessuna run registrata.")
        return
    print(f"{'Timestamp':<22} {'Durata':>8} {'Liberato':>12} {'Upd':>4} {'Warn':>5} {'Err':>4}")
    print("-" * 62)
    for r in runs[-20:]:
        ts = r.get("timestamp", "?")
        dur = f"{r.get('duration_sec', 0)}s"
        freed = bytes_to_human(r.get("freed_bytes", 0))
        print(f"{ts:<22} {dur:>8} {freed:>12} "
              f"{r.get('updated_count', 0):>4} "
              f"{r.get('warnings_count', 0):>5} "
              f"{r.get('errors_count', 0):>4}")


def prune_history():
    try:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
            print(f"History rimossa: {HISTORY_FILE}")
        else:
            print("Nessun file di history da rimuovere.")
    except Exception as e:
        print(f"Errore rimuovendo history: {e}", file=sys.stderr)


def _trend_message(report: SessionReport) -> str:
    try:
        history = load_history()
        runs = history.get("runs", [])
        if not runs:
            return ""
        prev = runs[-1]
        dur_now = int(time.time() - report.start_time)
        dur_prev = prev.get("duration_sec", 0)
        freed_now = int(report.freed_bytes)
        freed_prev = prev.get("freed_bytes", 0)

        parts = []
        dur_delta = dur_now - dur_prev
        if abs(dur_delta) >= 5:
            sign = "+" if dur_delta > 0 else ""
            parts.append(f"durata {sign}{dur_delta}s")

        freed_delta = freed_now - freed_prev
        if abs(freed_delta) >= 1_000_000:
            sign = "+" if freed_delta > 0 else "-"
            parts.append(f"liberato {sign}{bytes_to_human(abs(freed_delta))}")

        warn_delta = len(report.warnings) - prev.get("warnings_count", 0)
        if warn_delta != 0:
            sign = "+" if warn_delta > 0 else ""
            parts.append(f"warning {sign}{warn_delta}")

        if parts:
            return "📊 Trend vs run precedente: " + ", ".join(parts)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# SEZIONI
# ---------------------------------------------------------------------------

def update_homebrew(report: SessionReport, dry_run: bool):
    if not cmd_exists("brew"):
        log("Homebrew non trovato — saltato.", level="WARNING")
        report.skipped.append("Homebrew")
        return

    _, _, rc = run(["brew", "update"], "Homebrew: update", dry_run)
    if rc != 0:
        report.warnings.append("Homebrew update fallito. Controlla connessione e riprova.")
        return

    # --- Formule ---
    formulae_out, formulae_err, _ = run(
        ["brew", "upgrade", "--formula"],
        "Homebrew: upgrade formule", dry_run
    )
    attempted_formulae = set()
    for line in (formulae_out or "").splitlines():
        m = re.search(r'Upgrading\s+(\S+)', line)
        if m:
            attempted_formulae.add(m.group(1).strip())

    # brew link fallito
    if formulae_err and "brew link" in formulae_err.lower() and \
            "did not complete successfully" in formulae_err.lower():
        link_formula = None
        m = re.search(r'Warning:\s+([a-zA-Z0-9_\-@.]+)\s+is\s+already', formulae_err)
        if m:
            link_formula = m.group(1)
        if link_formula:
            report.warnings.append(
                f"brew link fallito per '{link_formula}'. "
                f"Rilancia: brew link --overwrite {link_formula}"
            )
        else:
            report.warnings.append(
                "brew link non completato per almeno una formula. "
                "Verifica con: brew link --overwrite <formula>"
            )

    if not dry_run and attempted_formulae:
        still_out, _, _ = run(
            ["brew", "outdated", "--formula"],
            "Homebrew: verifica formule rimaste obsolete", dry_run
        )
        still_outdated = set()
        for line in (still_out or "").splitlines():
            parts = line.split()
            if parts:
                still_outdated.add(parts[0].strip())

        for f in sorted(attempted_formulae - still_outdated):
            report.updated.append(f"formula: {f}")
        for f in sorted(attempted_formulae & still_outdated):
            report.warnings.append(f"Formula '{f}' non aggiornata. Prova: brew upgrade {f}")
    else:
        for f in sorted(attempted_formulae):
            report.updated.append(f"formula: {f}")

    # --- Cask ---
    cask_out, cask_err, _ = run(
        ["brew", "upgrade", "--cask", "--greedy"],
        "Homebrew: upgrade cask", dry_run
    )

    attempted_casks = set()
    for line in (cask_out or "").splitlines():
        m = re.search(r'Upgrading\s+(\S+)', line)
        if m:
            attempted_casks.add(m.group(1).strip())
    for line in (cask_err or "").splitlines():
        m = re.search(r'Cask\s+(\S+)\s+\(', line)
        if m:
            attempted_casks.add(m.group(1).strip())

    failed_casks = set()
    for m in re.finditer(r'Error:\s+([a-zA-Z0-9_\-@.]+)\s*:', cask_err or ""):
        failed_casks.add(m.group(1).strip())

    for c in sorted(attempted_casks - failed_casks):
        if c and c != "Cask":
            report.updated.append(f"cask: {c}")

    for cask_name in sorted(failed_casks):
        if not cask_name or cask_name == "Cask":
            continue

        # Caso 1: app mancante
        m = re.search(
            rf"Error:\s+{re.escape(cask_name)}\s*:\s+"
            rf"It seems the App source '/Applications/([^']+\.app)' is not there",
            cask_err or ""
        )
        if m:
            report.warnings.append(
                f"App '{m.group(1)}' mancante in /Applications (cask '{cask_name}'). "
                f"Reinstalla con: brew reinstall --cask {cask_name} "
                f"oppure rimuovi: brew uninstall --cask {cask_name}"
            )
            continue

        # Caso 2: app già presente (v7.11: suggerisce --force)
        if re.search(
            rf"Error:\s+{re.escape(cask_name)}\s*:\s+"
            rf"It seems there is already an App at",
            cask_err or ""
        ):
            report.warnings.append(
                f"Cask '{cask_name}' non aggiornato: versione precedente ancora "
                f"in Caskroom. Prova: brew reinstall --cask --force {cask_name}"
            )
            continue

        # Caso 3: richiede sudo
        if re.search(
            rf"Error:\s+{re.escape(cask_name)}\s*:\s+Failure while executing;\s+`/usr/bin/sudo",
            cask_err or ""
        ) or re.search(
            rf"Error:\s+{re.escape(cask_name)}\s*:\s+.*sudo:\s+a terminal is required",
            cask_err or ""
        ):
            report.warnings.append(
                f"Cask '{cask_name}' richiede sudo (password) per l'aggiornamento. "
                f"Aggiorna da Terminale interattivo con: "
                f"brew upgrade --cask {cask_name}"
            )
            continue

        # Fallback
        report.warnings.append(
            f"Cask '{cask_name}' non aggiornato. Prova: brew reinstall --cask {cask_name}"
        )

    clean_out, _, _ = run(["brew", "cleanup", "--prune=all"], "Homebrew: cleanup", dry_run)
    m = re.search(r'freed approximately ([\d.]+)\s*(\w+)', clean_out or "")
    if m:
        report.add_freed(m.group(1), m.group(2))

    report.updated.append("Homebrew ✓")


def update_mas(report: SessionReport, dry_run: bool):
    if not cmd_exists("mas"):
        log("mas non installato — saltato. Per installarlo: brew install mas")
        report.skipped.append("mas (App Store)")
        return

    mas_ver = _get_mas_version()

    if not has_passwordless_sudo():
        log(f"mas {mas_ver}: sudo non disponibile in modalità non interattiva — saltato.")
        report.warnings.append(
            f"App Store/mas (v{mas_ver}): richiede password sudo in modalità interattiva. "
            f"Questo è normale quando lo script gira fuori dal Terminale. "
            f"Aggiorna le app manualmente dall'App Store grafico. "
            f"Per automatizzare: configura SUDO_ASKPASS o aggiungi una regola sudoers NOPASSWD."
        )
        report.skipped.append("mas (App Store, sudo richiesto)")
        return

    out, err, rc = run(["mas", "upgrade"], "Mac App Store: upgrade", dry_run)

    if rc == 0:
        for line in (out or "").splitlines():
            if "Upgrading" in line or "upgraded" in line:
                match = re.search(r'\d+\s+(.+)$', line)
                if match:
                    report.updated.append(f"App Store: {match.group(1).strip()}")
        report.updated.append("App Store ✓")
        return

    report.warnings.append(diagnose_error(err, "mas upgrade"))


def update_node(report: SessionReport, dry_run: bool):
    if not cmd_exists("npm"):
        if cmd_exists("brew"):
            log("npm non trovato — tentativo installazione Node.js via Homebrew...")
            _, err, rc = run(["brew", "install", "node"], "Homebrew: install node", dry_run)
            if rc != 0:
                report.warnings.append(diagnose_error(err, "installazione Node.js"))
                report.skipped.append("npm")
                return
            log("Node.js installato correttamente.")
        else:
            report.warnings.append(
                "npm non trovato e Homebrew non disponibile. "
                "Installa Node.js manualmente da https://nodejs.org"
            )
            report.skipped.append("npm")
            return

    run(["npm", "install", "-g", "npm@latest"], "NPM: aggiorna CLI", dry_run)
    run(["npm", "update", "-g"], "NPM: aggiorna pacchetti globali", dry_run)
    run(["npm", "cache", "clean", "--force"], "NPM: pulizia cache", dry_run)
    report.updated.append("npm ✓")


def update_python(report: SessionReport, dry_run: bool, auto_upgrade: bool = False):
    """
    v7.11: sceglie il pip con la versione Python più alta tra tutti i candidati.
    """
    pip_cmd = _get_pip_cmd()

    if not cmd_exists(pip_cmd[0]) and not pip_cmd[0].startswith("python"):
        log("pip non trovato — saltato.", level="WARNING")
        report.skipped.append("Python/pip")
        return

    py_ver = _pip_python_version(pip_cmd)
    log(f"Pip: {' '.join(pip_cmd)} (Python {py_ver})")

    pip_cache_out, _, _ = run(
        pip_cmd + ["cache", "purge"],
        "Pip: pulizia cache", dry_run
    )
    m = re.search(r'\((\d+(?:\.\d+)?)\s*(\w+)\)', pip_cache_out or "")
    if m:
        report.add_freed(m.group(1), m.group(2))

    run(
        pip_cmd + ["install", "--upgrade", "--break-system-packages", "pip"],
        "Pip: aggiorna pip", dry_run
    )

    out, _, _ = run(
        pip_cmd + ["list", "--user", "--outdated", "--format=json"],
        "Pip: pacchetti obsoleti (solo info)", dry_run
    )

    packages = []
    if out and out.strip():
        try:
            data = json.loads(out)
            if isinstance(data, list):
                packages = [
                    p for p in data
                    if isinstance(p, dict)
                    and p.get("name")
                    and set(p["name"]) - {"-", " "}
                ]
        except json.JSONDecodeError:
            log("Pip: JSON non disponibile, fallback su parsing testuale.", level="WARNING")
            for l in (out or "").splitlines()[1:]:
                s = l.strip()
                if not s or set(s) <= {"-", " "}:
                    continue
                parts = s.split()
                if parts and not parts[0].startswith("-"):
                    packages.append({"name": parts[0]})

    if packages:
        pkg_names = [p["name"] for p in packages]

        if auto_upgrade:
            log(f"Pip: auto-upgrade di {len(pkg_names)} pacchetti...")
            _, upgrade_err, upgrade_rc = run(
                pip_cmd + ["install", "--user", "--break-system-packages",
                           "--upgrade", "--no-warn-script-location"] + pkg_names,
                f"Pip: upgrade automatico ({len(pkg_names)} pacchetti)",
                dry_run
            )
            if upgrade_rc == 0:
                report.updated.append(f"pip: {len(pkg_names)} pacchetti aggiornati")
            else:
                report.warnings.append(
                    f"Pip: auto-upgrade fallito per: {', '.join(pkg_names)}. "
                    f"Errore: {(upgrade_err or '?')[:120]}"
                )
        else:
            if "ocrmypdf" in [n.lower() for n in pkg_names]:
                report.warnings.append(
                    "ocrmypdf ha dipendenze mancanti o obsolete. "
                    "Esegui: pip install --user --break-system-packages --upgrade ocrmypdf"
                )
            else:
                outdated_list = ", ".join(pkg_names)
                upgrade_cmd = (
                    f"{' '.join(pip_cmd)} install --user --break-system-packages --upgrade "
                    + " ".join(pkg_names)
                )
                report.warnings.append(
                    f"{len(packages)} pacchetto/i pip obsoleto/i "
                    f"(Python {py_ver}): {outdated_list}. "
                    f"Aggiorna manualmente con: {upgrade_cmd}"
                )

    report.updated.append("Python/pip ✓")


def update_go(report: SessionReport, dry_run: bool):
    if not cmd_exists("go"):
        report.skipped.append("Go")
        return
    run(["go", "install", "golang.org/x/tools/cmd/goimports@latest"],
        "Go: goimports", dry_run)
    report.updated.append("Go ✓")


def check_macos(report: SessionReport, dry_run: bool):
    out, err, rc = run(["softwareupdate", "-l"], "macOS: verifica aggiornamenti", dry_run)
    combined = (out + " " + err).lower()
    full_text = out + " " + err

    if "no new software" in combined:
        log("macOS: nessun aggiornamento disponibile.")
    elif "label:" in combined:
        large = []
        for m in re.finditer(r'Title:\s*([^,]+).*?Size:\s*(\d+)KiB', full_text):
            title = m.group(1).strip()
            size_mb = int(m.group(2)) / 1024
            if size_mb > 500:
                large.append(f"{title} ({size_mb:.0f} MB)")

        if large:
            report.warnings.append(
                f"⚠ macOS: aggiornamenti GRANDI disponibili → {', '.join(large)}. "
                f"Installa da Impostazioni di Sistema (richiede riavvio)."
            )
        else:
            report.warnings.append(
                "macOS: aggiornamenti disponibili → installa da Impostazioni di Sistema."
            )
        log("macOS: aggiornamenti disponibili.", level="WARNING")
    elif "error domain=sumaccontrollererror" in combined:
        report.warnings.append(
            "Controllo aggiornamenti macOS fallito (accesso negato). "
            "Riprova più tardi o verifica in Impostazioni di Sistema."
        )
    elif rc != 0:
        report.warnings.append(diagnose_error(err, "softwareupdate -l"))


# ---------------------------------------------------------------------------
# AUTO-DISCOVERY TOOL
# ---------------------------------------------------------------------------

KNOWN_DISCOVERABLE_TOOLS = {
    "gem": {
        "label": "Ruby/gem",
        "update_cmds": [
            ["gem", "update", "--user-install", "--no-document"],
            ["gem", "cleanup", "--user-install"]
        ],
        "permission_note": (
            "Permessi negati per Ruby di sistema. Lo script ha provato l'installazione utente. "
            "Per automazione totale: 'brew install ruby' e aggiungi il path al profilo."
        ),
        "timeout": 60,
        "pre_check": "ensure_ruby_via_brew"
    },
    "cargo": {
        "label": "Rust/cargo",
        "update_cmds": [["rustup", "update"]],
        "timeout": 300,
    },
    "composer": {
        "label": "PHP/Composer",
        "update_cmds": [
            {"cmd": ["composer", "self-update"]},
            {
                "cmd": ["composer", "global", "update"],
                "skip_if_missing": "~/.composer/composer.json",
            },
        ],
        "timeout": 300,
    },
    "pipx": {
        "label": "pipx",
        "update_cmds": [["pipx", "upgrade-all"]],
        "timeout": 300,
    },
    "conda": {
        "label": "Conda",
        "update_cmds": [["conda", "update", "--all", "-y"]],
        "timeout": 300,
    },
    "mamba": {
        "label": "Mamba",
        "update_cmds": [["mamba", "update", "--all", "-y"]],
        "timeout": 300,
    },
    "julia": {
        "label": "Julia",
        "update_cmds": [["julia", "-e", 'import Pkg; Pkg.update()']],
        "timeout": 300,
    },
    "pod": {
        "label": "CocoaPods",
        "update_cmds": [["gem", "update", "cocoapods"], ["pod", "repo", "update"]],
        "timeout": 300,
    },
    "yarn": {
        "label": "Yarn",
        "update_cmds": [["yarn", "global", "upgrade"]],
        "timeout": 300,
    },
    "rustup": {
        "label": "Rustup",
        "update_cmds": [["rustup", "update"]],
        "timeout": 300,
    },
    "flutter": {
        "label": "Flutter",
        "update_cmds": [["flutter", "upgrade"]],
        "timeout": 300,
    },
    "rbenv": {
        "label": "rbenv",
        "update_cmds": [["rbenv", "update"]],
        "timeout": 300,
    },
    "pyenv": {
        "label": "pyenv",
        "update_cmds": [["pyenv", "update"]],
        "timeout": 300,
    },
}

CORE_TOOLS = {"brew", "mas", "npm", "node", "python3", "pip", "pip3", "go", "git"}


def load_tools_state() -> dict:
    try:
        if os.path.exists(TOOLS_STATE_FILE):
            with open(TOOLS_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"known_tools": [], "last_run": ""}


def save_tools_state(state: dict):
    state["last_run"] = datetime.datetime.now().isoformat()
    try:
        with open(TOOLS_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Impossibile salvare tools state: {e}", level="WARNING")


def ensure_ruby_via_brew(report: SessionReport, dry_run: bool):
    global CUSTOM_ENV
    ruby_path = shutil.which("ruby", path=CUSTOM_ENV["PATH"])
    if ruby_path and "/homebrew/" in ruby_path:
        return True

    if not cmd_exists("brew"):
        return False

    log("Ruby di sistema rilevato. Installazione di ruby via Homebrew per evitare permessi...")
    _, err, rc = run(["brew", "install", "ruby"], "Homebrew: install ruby", dry_run)
    if rc != 0:
        log(f"Installazione ruby fallita: {err}", level="WARNING")
        return False

    CUSTOM_ENV = build_env()
    return True


def discover_and_update_tools(report: SessionReport, dry_run: bool):
    log("▶ Auto-discovery: scansione tool installati...")

    state = load_tools_state()
    previously_known = set(state.get("known_tools", []))
    currently_found = set()
    newly_discovered = []

    for cmd, info in KNOWN_DISCOVERABLE_TOOLS.items():
        if not cmd_exists(cmd):
            continue

        currently_found.add(cmd)

        if cmd not in previously_known:
            newly_discovered.append(info["label"])
            log(f"🆕 Nuovo tool rilevato: {info['label']} ({cmd})")

        pre_check = info.get("pre_check")
        if pre_check:
            pre_func = globals().get(pre_check)
            if pre_func:
                pre_func(report, dry_run)

        log(f"▶ Auto-update: {info['label']}")
        all_ok = True
        perm_error = False
        any_cmd_run = False
        timeout = info.get("timeout", 300)

        for update_entry in info["update_cmds"]:
            if isinstance(update_entry, dict):
                skip_if_missing = update_entry.get("skip_if_missing")
                if skip_if_missing:
                    expanded = os.path.expanduser(skip_if_missing)
                    if not os.path.exists(expanded):
                        log(f"{info['label']}: skip (file mancante: {skip_if_missing})")
                        continue
                update_cmd = update_entry["cmd"]
            else:
                update_cmd = update_entry

            if not cmd_exists(update_cmd[0]):
                continue

            any_cmd_run = True
            _, stderr, rc = run(
                update_cmd,
                f"{info['label']}: {' '.join(update_cmd)}",
                dry_run, timeout=timeout
            )
            if rc != 0:
                all_ok = False
            if stderr and ("filepermissionerror" in stderr.lower() or
                           "don't have write permissions" in stderr.lower()):
                perm_error = True
                all_ok = False

        if perm_error and "permission_note" in info:
            report.warnings.append(f"{info['label']}: {info['permission_note']}")
        elif all_ok and any_cmd_run:
            report.updated.append(f"auto: {info['label']} ✓")
        elif not any_cmd_run:
            log(f"{info['label']}: nessun comando eseguito (tutti skippati).")
        else:
            log(f"Auto-update: {info['label']} non aggiornato (timeout o errore)", level="WARNING")

    state["known_tools"] = sorted(currently_found)
    save_tools_state(state)

    if newly_discovered:
        report.warnings.append(
            f"🆕 Nuovi tool rilevati e aggiornati per la prima volta: "
            f"{', '.join(newly_discovered)}. "
            f"Verranno gestiti automaticamente nelle run successive."
        )

    disappeared = previously_known - currently_found
    if disappeared:
        labels = [KNOWN_DISCOVERABLE_TOOLS.get(c, {}).get("label", c) for c in disappeared]
        log(f"Tool non più trovati rispetto alla run precedente: {', '.join(labels)}", level="WARNING")

    if not currently_found:
        log("Auto-discovery: nessun tool aggiuntivo rilevato.")
    else:
        log(f"Auto-discovery: {len(currently_found)} tool gestiti → {', '.join(sorted(currently_found))}")


# ---------------------------------------------------------------------------
# SOMMARIO FINALE
# ---------------------------------------------------------------------------

def _group_updated(items: list) -> dict:
    groups = {"formula": [], "cask": [], "auto": [], "app": [], "pip": [], "altro": []}
    for it in items:
        if it.startswith("formula:"):
            groups["formula"].append(it.split(":", 1)[1].strip())
        elif it.startswith("cask:"):
            groups["cask"].append(it.split(":", 1)[1].strip())
        elif it.startswith("auto:"):
            groups["auto"].append(it.split(":", 1)[1].strip())
        elif it.startswith("App Store:"):
            groups["app"].append(it.split(":", 1)[1].strip())
        elif it.startswith("pip:"):
            groups["pip"].append(it.split(":", 1)[1].strip())
        else:
            groups["altro"].append(it)
    return groups


def print_summary(report: SessionReport, notify_when: str = "always"):
    elapsed = int(time.time() - report.start_time)
    mins, secs = divmod(elapsed, 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    lines = [
        "=" * 55,
        f"  SOMMARIO MANUTENZIONE v{VERSION} — {time_str}",
        "=" * 55,
    ]

    if report.updated:
        g = _group_updated(report.updated)
        parts = []
        if g["formula"]:
            parts.append(f"formule ({len(g['formula'])}): {', '.join(g['formula'])}")
        if g["cask"]:
            parts.append(f"cask ({len(g['cask'])}): {', '.join(g['cask'])}")
        if g["app"]:
            parts.append(f"App Store ({len(g['app'])}): {', '.join(g['app'])}")
        if g["pip"]:
            parts.append(f"pip ({len(g['pip'])}): {', '.join(g['pip'])}")
        if g["auto"]:
            parts.append(f"auto-tool ({len(g['auto'])}): {', '.join(g['auto'])}")
        if g["altro"]:
            parts.extend(g["altro"])
        lines.append(f"  ✅ Aggiornati  : {'; '.join(parts)}")

    if report.skipped:
        lines.append(f"  ⏭  Saltati     : {', '.join(report.skipped)}")
    if report.freed_space:
        lines.append(f"  🗑  Spazio lib. : {report.freed_space}")
    if report.warnings:
        lines.append("  ⚠  Warning     :")
        for w in report.warnings:
            lines.append(f"       • {w}")
    if report.errors:
        lines.append("  ❌ Errori      :")
        for e in report.errors:
            lines.append(f"       • {e}")
    lines.append("=" * 55)

    trend = _trend_message(report)
    if trend:
        lines.append("")
        lines.append(f"  {trend}")

    if report.warnings:
        lines.append("")
        lines.append("  📋 PROSSIMI PASSI:")
        lines.append("     Risolvi i warning indicati sopra, poi esegui nuovamente")
        lines.append("     lo script per verificare che non siano più presenti.")
        lines.append(f"     → python3 {os.path.abspath(__file__)}")
        lines.append("")

    log("\n".join(lines))

    if notify_when == "never":
        return

    freed_str = f" | Liberati {report.freed_space}" if report.freed_space else ""

    if report.errors:
        msg = f"Completato in {time_str} — {len(report.errors)} errore/i. Apri il log per dettagli."
        notify(msg)
        return

    if report.warnings:
        msg = f"Completato in {time_str}{freed_str} — {len(report.warnings)} avviso/i. Apri mac_update_log.txt per le istruzioni."
        notify(msg)
        return

    if notify_when == "on-warning":
        return

    if report.updated:
        msg = f"Aggiornati {len(report.updated)} componenti in {time_str}{freed_str}"
    else:
        msg = f"Sistema già aggiornato — {time_str}{freed_str}"
    notify(msg)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

SECTION_FUNCS = {
    "brew":   update_homebrew,
    "mas":    update_mas,
    "node":   update_node,
    "python": lambda r, d: update_python(r, d, auto_upgrade=False),
    "go":     update_go,
    "macos":  check_macos,
    "tools":  discover_and_update_tools,
}


def main():
    global PIP_CMD_OVERRIDE

    parser = argparse.ArgumentParser(
        description=f"Mac Maintenance v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Esempi:
  python3 {os.path.basename(__file__)}                            # Completa
  python3 {os.path.basename(__file__)} --quick                    # Salta Go/macOS/tools
  python3 {os.path.basename(__file__)} --dry-run                  # Simula
  python3 {os.path.basename(__file__)} --only=brew,python         # Solo alcune sezioni
  python3 {os.path.basename(__file__)} --auto-upgrade-pip         # Aggiorna pip obsoleti
  python3 {os.path.basename(__file__)} --pip-cmd='python3.14 -m pip'
  python3 {os.path.basename(__file__)} --notify-when=on-warning
  python3 {os.path.basename(__file__)} --show-history
  python3 {os.path.basename(__file__)} --prune-history

Sezioni: {', '.join(ALL_SECTIONS)}

File:
  Log     : {LOG_FILE}
  Tools   : {TOOLS_STATE_FILE}
  History : {HISTORY_FILE}
        """
    )
    parser.add_argument("--quick",     action="store_true",
                        help="Salta Go, macOS check e auto-discovery")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Simula l'esecuzione senza modificare nulla")
    parser.add_argument("--no-notify", action="store_true",
                        help="Deprecato: equivale a --notify-when=never")
    parser.add_argument("--notify-when", choices=["always", "on-warning", "never"],
                        default="always", help="Politica notifica (default: always)")
    parser.add_argument("--only", type=str, default="",
                        help="Esegui solo alcune sezioni (comma-separated)")
    parser.add_argument("--auto-upgrade-pip", action="store_true",
                        help="Aggiorna automaticamente i pacchetti pip obsoleti")
    parser.add_argument("--pip-cmd", type=str, default="",
                        help="Override comando pip (es. 'python3.14 -m pip')")
    parser.add_argument("--show-history", action="store_true",
                        help="Mostra lo storico delle run ed esce")
    parser.add_argument("--prune-history", action="store_true",
                        help="Cancella il file di history ed esce")
    args = parser.parse_args()

    if args.show_history:
        show_history()
        return
    if args.prune_history:
        prune_history()
        return

    if args.pip_cmd.strip():
        PIP_CMD_OVERRIDE = args.pip_cmd.strip()

    notify_when = "never" if args.no_notify else args.notify_when

    if args.only.strip():
        requested = {s.strip().lower() for s in args.only.split(",") if s.strip()}
        unknown = requested - set(ALL_SECTIONS)
        if unknown:
            print(f"⚠ Sezioni sconosciute (ignorate): {', '.join(sorted(unknown))}",
                  file=sys.stderr)
        sections = [s for s in ALL_SECTIONS if s in requested]
    else:
        sections = list(ALL_SECTIONS)

    if args.quick:
        sections = [s for s in sections if s not in ("go", "macos", "tools")]

    report = SessionReport()
    report.sections = list(sections)

    log("=" * 55)
    log(f"=== AVVIO MANUTENZIONE v{VERSION}"
        f"{' [DRY-RUN]' if args.dry_run else ''}"
        f"{' [QUICK]' if args.quick else ''}"
        f"{' [ONLY:' + ','.join(sections) + ']' if args.only.strip() else ''}"
        f"{' [AUTO-PIP]' if args.auto_upgrade_pip else ''}"
        f"{' [PIP-CMD:' + args.pip_cmd + ']' if args.pip_cmd.strip() else ''} ===")
    log(f"Log scritto in: {LOG_FILE}")
    log("=" * 55)

    for name in sections:
        func = SECTION_FUNCS.get(name)
        if not func:
            log(f"Sezione sconosciuta: {name}", level="WARNING")
            continue
        if name == "python":
            update_python(report, args.dry_run, auto_upgrade=args.auto_upgrade_pip)
        else:
            func(report, args.dry_run)

    if args.quick:
        for skipped_name in ("Go", "macOS check", "auto-discovery"):
            report.skipped.append(skipped_name)

    print_summary(report, notify_when=notify_when)

    if not args.dry_run:
        save_history(report)


if __name__ == "__main__":
    main()