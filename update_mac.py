#!/usr/bin/env python3
"""
mac_maintenance_v7.8.py
Manutenzione completa per macOS — con automazione proattiva per Ruby.
Tutti i file di log e stato vengono salvati in ~/Documents/MacMaintenance.
"""

import subprocess
import os
import datetime
import time
import re
import argparse
import shutil
import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------------

VERSION = "7.8"

def _resolve_log_file() -> str:
    """
    Percorso assoluto del log, con priorità a ~/Documents/MacMaintenance.
    Crea la cartella se non esiste.
    """
    try:
        import pwd
        real_home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        real_home = os.path.expanduser("~")

    # Cartella principale per i file di manutenzione
    maint_folder = os.path.join(real_home, "Documents", "MacMaintenance")
    try:
        if not os.path.exists(maint_folder):
            os.makedirs(maint_folder, exist_ok=True)
        if os.path.isdir(maint_folder) and os.access(maint_folder, os.W_OK):
            return os.path.join(maint_folder, "mac_update_log.txt")
    except Exception:
        pass

    # Fallback in ordine
    for folder in [
        os.path.join(real_home, "Documents"),
        os.path.join(real_home, "Desktop"),
        real_home,
    ]:
        if os.path.isdir(folder) and os.access(folder, os.W_OK):
            return os.path.join(folder, "mac_update_log.txt")

    # Ultima spiaggia: stessa cartella dello script
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "mac_update_log.txt")

LOG_FILE = _resolve_log_file()
TOOLS_STATE_FILE = LOG_FILE.replace("mac_update_log.txt", "mac_update_tools.json")

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


# ---------------------------------------------------------------------------
# AMBIENTE
# ---------------------------------------------------------------------------

def build_env() -> dict:
    """Costruisce PATH dinamico per Apple Silicon e Intel."""
    env = os.environ.copy()
    paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    try:
        result = subprocess.run(
            ["python3", "-m", "site", "--user-base"],
            capture_output=True, text=True, timeout=5
        )
        user_base = result.stdout.strip()
        if user_base:
            user_bin = os.path.join(user_base, "bin")
            if os.path.isdir(user_bin):
                paths.insert(0, user_bin)
    except Exception:
        paths.append(os.path.expanduser("~/Library/Python/3.9/bin"))
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


def run(cmd: list, description: str, dry_run: bool = False, timeout: int = 300) -> tuple[str, str, int]:
    """
    Esegue un comando con timeout personalizzabile.
    Restituisce (stdout, stderr, returncode).
    """
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
    err = stderr.lower()
    if re.search(r'\bsudo\b', err) or "password is required" in err:
        return (f"Privilegi mancanti per '{description}'. "
                f"Se è mas, prova: brew upgrade mas oppure usa l'App Store grafico.")
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

    # Formule
    formulae_out, _, _ = run(["brew", "upgrade"], "Homebrew: upgrade formule", dry_run)
    for line in (formulae_out or "").splitlines():
        if "Upgrading" in line:
            # Es: "Upgrading libngtcp2" oppure "Upgrading xz (5.8.2 -> 5.8.3)"
            match = re.search(r'Upgrading\s+(\S+)', line)
            if match:
                report.updated.append(f"formula: {match.group(1)}")

    # Cask
    cask_out, _, _ = run(
        ["brew", "upgrade", "--cask", "--greedy"],
        "Homebrew: upgrade cask", dry_run
    )
    for line in (cask_out or "").splitlines():
        if "Upgrading" in line:
            match = re.search(r'Upgrading\s+(\S+)', line)
            if match:
                report.updated.append(f"cask: {match.group(1)}")

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

    out, err, rc = run(["mas", "upgrade"], "Mac App Store: upgrade", dry_run)

    if rc == 0:
        # Estrai i nomi delle app aggiornate (es. "Upgrading 12345 Some App")
        for line in (out or "").splitlines():
            if "Upgrading" in line or "upgraded" in line:
                match = re.search(r'\d+\s+(.+)$', line)
                if match:
                    app_name = match.group(1).strip()
                    report.updated.append(f"App Store: {app_name}")
        report.updated.append("App Store ✓")
        return

    needs_sudo = err and ("sudo" in err.lower() or "password" in err.lower())

    if needs_sudo:
        log("Rilevato errore sudo su mas — remediation automatica in corso...")
        _, brew_err, brew_rc = run(["brew", "upgrade", "mas"], "Homebrew: upgrade mas", dry_run)

        mas_was_already_updated = brew_rc != 0 or (
            brew_err and "already installed" in brew_err.lower()
        )

        out2, err2, rc2 = run(["mas", "upgrade"], "Mac App Store: upgrade (dopo fix)", dry_run)

        if rc2 == 0:
            for line in (out2 or "").splitlines():
                if "Upgrading" in line or "upgraded" in line:
                    match = re.search(r'\d+\s+(.+)$', line)
                    if match:
                        app_name = match.group(1).strip()
                        report.updated.append(f"App Store: {app_name}")
            report.updated.append("App Store ✓")
            return

        if mas_was_already_updated:
            report.warnings.append(
                "mas è aggiornato (v6.0.1) ma richiede un terminale interattivo. "
                "Questo è normale quando lo script gira fuori dal Terminale. "
                "Aggiorna le app manualmente dall'App Store grafico."
            )
        else:
            report.warnings.append(diagnose_error(err2 or err, "mas upgrade"))
    else:
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


def update_python(report: SessionReport, dry_run: bool):
    if not cmd_exists("python3"):
        log("python3 non trovato — saltato.", level="WARNING")
        report.skipped.append("Python/pip")
        return

    pip_cache_out, _, _ = run(
        ["python3", "-m", "pip", "cache", "purge"],
        "Pip: pulizia cache", dry_run
    )
    m = re.search(r'\((\d+(?:\.\d+)?)\s*(\w+)\)', pip_cache_out or "")
    if m:
        report.add_freed(m.group(1), m.group(2))

    run(["python3", "-m", "pip", "install", "--upgrade", "pip"],
        "Pip: aggiorna pip", dry_run)

    out, _, _ = run(
        ["python3", "-m", "pip", "list", "--outdated", "--format=columns"],
        "Pip: pacchetti obsoleti (solo info)", dry_run
    )

    if out and out.strip():
        lines = out.splitlines()
        packages = [l for l in lines[1:] if l.strip()] if len(lines) > 1 else []
        if packages:
            pkg_names = [p.split()[0].lower() for p in packages if p.split()]
            if "ocrmypdf" in pkg_names:
                report.warnings.append(
                    "ocrmypdf ha dipendenze mancanti o obsolete. "
                    "Esegui: pip install --upgrade ocrmypdf"
                )
            else:
                # Costruisci la lista dei pacchetti obsoleti
                outdated_list = ', '.join([p.split()[0] for p in packages])
                upgrade_cmd = "pip install --upgrade " + ' '.join([p.split()[0] for p in packages])
                report.warnings.append(
                    f"{len(packages)} pacchetto/i pip obsoleto/i: {outdated_list}. "
                    f"Aggiorna manualmente con: {upgrade_cmd}"
                )
        else:
            report.warnings.append("Pip: nessun pacchetto obsoleto rilevato.")

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
# AUTO-DISCOVERY TOOL (con gestione proattiva Ruby)
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
        "timeout": 60,            # timeout ridotto per evitare blocchi
        "pre_check": "ensure_ruby_via_brew"
    },
    "cargo": {
        "label": "Rust/cargo",
        "update_cmds": [["rustup", "update"]],
        "timeout": 300,
    },
    "composer": {
        "label": "PHP/Composer",
        "update_cmds": [["composer", "self-update"], ["composer", "global", "update"]],
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
    # Ottieni il percorso di ruby
    ruby_path = shutil.which("ruby", path=CUSTOM_ENV["PATH"])
    if ruby_path and "/homebrew/" in ruby_path:
        # Già quello di Homebrew
        return True

    if not cmd_exists("brew"):
        return False

    log("Ruby di sistema rilevato. Installazione di ruby via Homebrew per evitare permessi...")
    _, err, rc = run(["brew", "install", "ruby"], "Homebrew: install ruby", dry_run)
    if rc != 0:
        log(f"Installazione ruby fallita: {err}", level="WARNING")
        return False

    # Ricostruisci l'ambiente per avere il nuovo ruby nel PATH
    CUSTOM_ENV = build_env()
    return True


def discover_and_update_tools(report: SessionReport, dry_run: bool):
    log("▶ Auto-discovery: scansione tool installati...")

    state = load_tools_state()
    previously_known = set(state.get("known_tools", []))
    currently_found = set()
    newly_discovered = []
    updated_tools = []   # lista di stringhe per il report

    for cmd, info in KNOWN_DISCOVERABLE_TOOLS.items():
        if not cmd_exists(cmd):
            continue

        currently_found.add(cmd)

        if cmd not in previously_known:
            newly_discovered.append(info["label"])
            log(f"🆕 Nuovo tool rilevato: {info['label']} ({cmd})")

        # Esegui pre‑check se definito
        pre_check = info.get("pre_check")
        if pre_check:
            pre_func = globals().get(pre_check)
            if pre_func:
                pre_func(report, dry_run)

        log(f"▶ Auto-update: {info['label']}")
        all_ok = True
        perm_error = False
        timeout = info.get("timeout", 300)

        for update_cmd in info["update_cmds"]:
            if not cmd_exists(update_cmd[0]):
                continue
            _, stderr, rc = run(update_cmd, f"{info['label']}: {' '.join(update_cmd)}",
                                dry_run, timeout=timeout)
            if rc != 0:
                all_ok = False
            if stderr and ("filepermissionerror" in stderr.lower() or
                           "don't have write permissions" in stderr.lower()):
                perm_error = True
                all_ok = False

        if perm_error and "permission_note" in info:
            report.warnings.append(f"{info['label']}: {info['permission_note']}")
        elif all_ok:
            # Tool aggiornato con successo
            updated_tools.append(f"{info['label']} ✓")
            # Aggiungi anche al report principale con dettaglio
            report.updated.append(f"auto: {info['label']} ✓")
        else:
            # Tool non aggiornato (timeout o errore)
            log(f"Auto-update: {info['label']} non aggiornato (timeout o errore)", level="WARNING")

    state["known_tools"] = sorted(currently_found)
    save_tools_state(state)

    # Aggiungi i tool aggiornati (già aggiunti singolarmente, ma li teniamo anche qui per compatibilità)
    if updated_tools:
        report.updated.extend(updated_tools)

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

def print_summary(report: SessionReport, notify_enabled: bool = True):
    elapsed = int(time.time() - report.start_time)
    mins, secs = divmod(elapsed, 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    lines = [
        "=" * 55,
        f"  SOMMARIO MANUTENZIONE v{VERSION} — {time_str}",
        "=" * 55,
    ]
    if report.updated:
        lines.append(f"  ✅ Aggiornati  : {', '.join(report.updated)}")
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

    if report.warnings:
        lines.append("")
        lines.append("  📋 PROSSIMI PASSI:")
        lines.append("     Risolvi i warning indicati sopra, poi esegui nuovamente")
        lines.append("     lo script per verificare che non siano più presenti.")
        lines.append(f"     → python3 {os.path.abspath(__file__)}")
        lines.append("")

    log("\n".join(lines))

    freed_str = f" | Liberati {report.freed_space}" if report.freed_space else ""

    if report.errors:
        msg = f"Completato in {time_str} — {len(report.errors)} errore/i. Apri il log per dettagli."
    elif report.warnings:
        msg = f"Completato in {time_str}{freed_str} — {len(report.warnings)} avviso/i. Apri mac_update_log.txt per le istruzioni."
    elif report.updated:
        msg = f"Aggiornati {len(report.updated)} componenti in {time_str}{freed_str}"
    else:
        msg = f"Sistema già aggiornato — {time_str}{freed_str}"

    if notify_enabled:
        notify(msg)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=f"Mac Maintenance v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python3 mac_maintenance_v7.8.py              # Manutenzione completa
  python3 mac_maintenance_v7.8.py --quick      # Salta Go e macOS check
  python3 mac_maintenance_v7.8.py --dry-run    # Simula senza eseguire
  python3 mac_maintenance_v7.8.py --no-notify  # Senza notifica finale

Note:
  Il log viene scritto in ~/Documents/MacMaintenance/mac_update_log.txt
  Lo stato dei tool scoperti è in ~/Documents/MacMaintenance/mac_update_tools.json
        """
    )
    parser.add_argument("--quick",     action="store_true",
                        help="Salta aggiornamenti lenti (Go, macOS check)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Simula l'esecuzione senza modificare nulla")
    parser.add_argument("--no-notify", action="store_true",
                        help="Disabilita notifica finale macOS")
    args = parser.parse_args()

    report = SessionReport()

    log("=" * 55)
    log(f"=== AVVIO MANUTENZIONE v{VERSION}"
        f"{' [DRY-RUN]' if args.dry_run else ''}"
        f"{' [QUICK]' if args.quick else ''} ===")
    log(f"Log scritto in: {LOG_FILE}")
    log("=" * 55)

    update_homebrew(report, args.dry_run)
    update_mas(report, args.dry_run)
    update_node(report, args.dry_run)
    update_python(report, args.dry_run)

    if not args.quick:
        update_go(report, args.dry_run)
        check_macos(report, args.dry_run)
        discover_and_update_tools(report, args.dry_run)
    else:
        log("Modalità --quick: saltati Go, macOS check e auto-discovery.")
        report.skipped += ["Go", "macOS check", "auto-discovery"]

    print_summary(report, notify_enabled=not args.no_notify)


if __name__ == "__main__":
    main()