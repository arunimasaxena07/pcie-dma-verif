#!/usr/bin/env python3
"""Session doctor for the PCIe DMA project.

Run it at the start of every work session, before you touch the project. It
catches the setup problems that silently waste an evening: the wrong Ubuntu,
the repo on the Windows side, a venv that isn't active or was copied from
somewhere else, Windows line endings, an upstream file you edited by accident,
work you forgot to push. For every problem it prints the exact command to fix it,
and at the end it tells you which task you're on.

    python3 tools/doctor.py              # check everything
    python3 tools/doctor.py --install    # once: adds the `pcie` command to Ubuntu

After --install, every session is: open "Ubuntu 24.04", type  pcie  and press Enter.

Standard library only, so it works even when the venv is broken.
"""

import glob
import os
import re
import shutil
import subprocess
import sys

REPO_NAME = "pcie-dma-verif"
WANT_DISTRO = "Ubuntu-24.04"
TASK_ORDER = ["1.1", "1.2", "1.3", "1.4", "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4", "3.5",
              "4.1", "4.2", "4.3", "4.4", "5.1", "5.2", "5.3", "6.1", "6.2", "6.3", "6.4"]
PINS = {"cocotb": "1.9.2", "cocotb-bus": "0.3.0", "cocotb-test": "0.3.0",
        "cocotbext-axi": "0.1.28", "cocotbext-pcie": "0.2.16"}
TTY = sys.stdout.isatty()


def c(s, code):
    return f"\033[{code}m{s}\033[0m" if TTY else s


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, what, fix=""):
        self.rows.append((status, what, fix))

    def ok(self, what):
        self.add("OK", what)

    def bad(self, what, fix):
        self.add("FIX", what, fix)

    def warn(self, what, fix=""):
        self.add("WARN", what, fix)

    def section(self, title):
        self.rows.append(("SECTION", title, ""))

    def show(self):
        for status, what, fix in self.rows:
            if status == "SECTION":
                print(c(f"\n{what}", "1"))
                continue
            tag = {"OK": c(" OK ", "32"), "FIX": c("FIX ", "31"), "WARN": c("WARN", "33")}[status]
            print(f"  [{tag}] {what}")
            if fix:
                for line in fix.strip("\n").splitlines():
                    print(f"         {c(line, '36')}")

    @property
    def blocking(self):
        return [r for r in self.rows if r[0] == "FIX"]


def sh(cmd, cwd=None, timeout=60):
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, timeout=timeout)
        out = p.stdout + p.stderr
        if b"\x00" in out:                                   # wsl.exe prints UTF-16
            out = out.decode("utf-16-le", errors="ignore")
        else:
            out = out.decode(errors="replace")
        return p.returncode, out.replace("\r", "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 127, str(e)


def windows_user():
    for d in sorted(glob.glob("/mnt/c/Users/*")):
        name = os.path.basename(d)
        if name not in ("Public", "Default", "Default User", "All Users", "desktop.ini") and os.path.isdir(d + "/Downloads"):
            return name
    return None


# ----------------------------------------------------------------- 1. which Linux

def check_linux(r):
    r.section("1. Which Linux am I in?")
    try:
        version = os.environ.get("DOCTOR_TEST_PROC_VERSION") or open("/proc/version").read()
    except OSError:
        version = ""
    version = version.lower()
    in_wsl = "microsoft" in version
    ctx = {"wsl": in_wsl, "distro": None, "right": True, "others": []}
    if not in_wsl:
        r.ok("not WSL (native Linux or Codespaces); skipping the Windows checks")
        return ctx
    os_rel = {}
    try:
        for line in open(os.environ.get("DOCTOR_TEST_OS_RELEASE", "/etc/os-release")):
            k, _, v = line.strip().partition("=")
            os_rel[k] = v.strip('"')
    except OSError:
        pass
    distro = os.environ.get("WSL_DISTRO_NAME", "?")
    ctx["distro"] = distro
    ctx["right"] = os_rel.get("VERSION_ID") == "24.04" and distro == WANT_DISTRO
    if ctx["right"]:
        r.ok(f"Ubuntu 24.04 (WSL name: {distro})")
    else:
        r.bad(f"you're in the Linux called '{distro}' ({os_rel.get('PRETTY_NAME', 'unknown version')}), "
              f"not '{WANT_DISTRO}', which has the tested tools",
              f"type exit, then open \"Ubuntu 24.04\" from the Start menu and run this again.\n"
              f"if your repo only exists in '{distro}', section 2 shows how to move it.")

    code, out = sh(["wsl.exe", "-l", "-v"])
    if code == 0 and out.strip():
        rows = [l for l in out.splitlines()[1:] if l.strip()]
        names = [l.replace("*", "").split()[0] for l in rows if l.replace("*", "").split()]
        default = next((l.replace("*", "").split()[0] for l in rows if l.strip().startswith("*")), None)
        others = [n for n in names if n != distro and not n.startswith("docker")]
        ctx["others"] = others
        extra = [n for n in names if n != WANT_DISTRO and not n.startswith("docker")]
        if not others:
            r.ok("only one Linux installed")
        elif ctx["right"]:
            ctx["extra"] = extra          # cleanup advice is given after the repo check
        else:
            r.warn(f"other Linux installs exist: {', '.join(others)}",
                   f"don't delete anything yet. Move your work into {WANT_DISTRO} first (section 2)")
        if default and default != distro:
            r.warn(f"the default Linux is {default}, so typing 'wsl' in PowerShell opens the wrong one",
                   f"in PowerShell: wsl --set-default {WANT_DISTRO}")
    return ctx


# ----------------------------------------------------------------- 2. repo

def is_repo(path):
    return os.path.isfile(os.path.join(path, ".gitmodules")) and \
        "verilog-pcie" in open(os.path.join(path, ".gitmodules")).read()


def find_repo():
    d = os.getcwd()
    while d != "/":
        if is_repo(d):
            return d
        d = os.path.dirname(d)
    home = os.path.expanduser(f"~/{REPO_NAME}")
    return home if os.path.isdir(home) and is_repo(home) else None


def search_elsewhere():
    hits = []
    for base, depth in (("/home", 2), ("/mnt/c/Users", 4)):
        if not os.path.isdir(base):
            continue
        code, out = sh(["find", base, "-maxdepth", str(depth), "-type", "d", "-name", REPO_NAME,
                        "-not", "-path", "*/AppData/*"], timeout=90)
        hits += [l for l in out.splitlines() if l.startswith("/")]
    return hits


def move_cmds(src_distro, win_user):
    tgz = f"/mnt/c/Users/{win_user or '<your-windows-user>'}/{REPO_NAME}.tgz"
    return (f"move it into {WANT_DISTRO} (run these in {WANT_DISTRO}):\n"
            f"  wsl.exe -d {src_distro} --cd ~ -- tar --exclude=.venv --exclude=sim_build -czf {tgz} {REPO_NAME}\n"
            f"  cd ~ && tar -xzf {tgz} && cd ~/{REPO_NAME}\n"
            f"  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt\n"
            f"  git submodule update --init")


def find_in_other_distros(others):
    found = []
    for d in others:
        code, out = sh(["wsl.exe", "-d", d, "--cd", "~", "--", "bash", "-c", f"ls -d ~/{REPO_NAME} 2>/dev/null"], timeout=60)
        if code == 0 and REPO_NAME in out:
            found.append(d)
    return found


def check_repo(r, ctx):
    r.section("2. Is the repo in the right place?")
    repo = find_repo()
    if repo and ctx["wsl"] and not ctx["right"]:
        r.bad(f"your repo is in '{ctx['distro']}', but the tested tools are in '{WANT_DISTRO}'",
              move_cmds(ctx["distro"], windows_user()))
        return repo
    if not repo and ctx["wsl"] and ctx["others"]:
        where = find_in_other_distros(ctx["others"])
        if where:
            r.bad(f"no repo here, but it exists in the Linux called '{where[0]}'",
                  move_cmds(where[0], windows_user()))
            return None
    if not repo:
        hits = search_elsewhere()
        if hits:
            win = [h for h in hits if h.startswith("/mnt/")]
            fix = f"found it at: {hits[0]}\n"
            if win:
                fix += ("it is on the Windows side; copy it into Ubuntu:\n"
                        f"  cp -r \"{win[0]}\" ~/\n"
                        f"  cd ~/{REPO_NAME} && rm -rf .venv && python3 -m venv .venv\n"
                        "  source .venv/bin/activate && pip install -r requirements.txt")
            else:
                fix += f"  cd \"{hits[0]}\""
            r.bad(f"no repo at ~/{REPO_NAME}", fix)
        else:
            r.bad(f"no repo at ~/{REPO_NAME}, and none found in /home or your Windows folders",
                  "the repo was never created here (or lives in another Linux install; see section 1).\n"
                  "redo the repo-creation step of task 1.1, starting with:  cd ~")
        return None
    r.ok(f"repo found: {repo}")
    if repo.startswith("/mnt/"):
        r.bad("repo is on the Windows side (/mnt/...): slow, and it breaks the upstream symlinks",
              f"cp -r \"{repo}\" ~/  then work in ~/{REPO_NAME} and rebuild .venv there")
    real_cwd = os.path.realpath(os.getcwd())
    if not (real_cwd + "/").startswith(os.path.realpath(repo) + "/"):
        r.warn(f"you're in {os.getcwd()}, not inside the repo", f"cd {repo}")
    code, _ = sh(["git", "rev-parse", "--git-dir"], cwd=repo)
    if code != 0:
        r.bad("the folder exists but isn't a git repo (the .git folder is missing)",
              "it was copied without .git; re-copy it including hidden files, or clone it from GitHub")
        return repo

    up = os.path.join(repo, "third_party", "verilog-pcie")
    if not os.path.isfile(os.path.join(up, "rtl", "dma_if_pcie_rd.v")):
        r.bad("upstream submodule is empty", f"cd {repo} && git submodule update --init")
    else:
        link = os.path.join(up, "tb", "dma_if_pcie_rd", "pcie_if.py")
        if os.path.islink(link):
            r.ok("upstream submodule present, symlinks intact")
        else:
            r.bad("upstream symlinks are broken (repo was checked out or copied through Windows)",
                  f"cd {repo} && rm -rf third_party/verilog-pcie && git submodule update --init --force")
        code, out = sh(["git", "-C", up, "status", "--porcelain", "--untracked-files=no"])
        changed = [l for l in out.splitlines() if l.strip()]
        if code == 0 and changed:
            r.bad(f"you edited {len(changed)} upstream file(s): {', '.join(l[3:] for l in changed[:3])}",
                  f"undo with: git -C third_party/verilog-pcie checkout -- .")
        elif code == 0:
            r.ok("upstream files unmodified")

    code, out = sh(["git", "ls-files", "-z"], cwd=repo)
    crlf = []
    for f in out.split("\x00"):
        if not f or f.startswith("third_party") or not re.search(r"\.(py|sv|svh|v|txt|ini|md|tcl|sh)$|^\.git", f):
            continue
        p = os.path.join(repo, f)
        try:
            if b"\r\n" in open(p, "rb").read():
                crlf.append(f)
        except OSError:
            pass
    for f in glob.glob(os.path.join(repo, "tools", "*.py")) + glob.glob(os.path.join(repo, "*.txt")):
        rel = os.path.relpath(f, repo)
        if rel not in crlf and b"\r\n" in open(f, "rb").read():
            crlf.append(rel)
    if crlf:
        r.bad(f"Windows line endings in {len(crlf)} file(s): {', '.join(crlf[:4])}",
              "usually from editing or downloading on the Windows side. Fix them:\n"
              + "\n".join(f"  sed -i 's/\\r$//' {f}" for f in crlf[:6]))
    else:
        r.ok("no Windows line endings in your files")
    return repo


# ----------------------------------------------------------------- 3. tools

def check_tools(r):
    r.section("3. Are the tools installed?")
    code, out = sh("iverilog -V 2>&1 | head -1")
    m = re.search(r"version (\d+)", out)
    if m and int(m.group(1)) >= 11:
        r.ok(f"Icarus Verilog {m.group(1)}")
    else:
        r.bad("Icarus Verilog missing", "sudo apt update && sudo apt install -y iverilog")
    code, out = sh(["verilator", "--version"])
    m = re.search(r"Verilator (\d+)\.(\d+)", out)
    if m and int(m.group(1)) >= 5:
        r.ok(f"Verilator {m.group(1)}.{m.group(2)}")
    elif m:
        r.bad(f"Verilator {m.group(1)}.{m.group(2)} is too old (Day 4 needs 5.x)",
              "you're probably not in Ubuntu 24.04; see section 1")
    else:
        r.bad("Verilator missing", "sudo apt install -y verilator")
    if shutil.which("gtkwave"):
        r.ok("GTKWave installed")
    else:
        r.warn("GTKWave missing (needed for task 1.3)", "sudo apt install -y gtkwave")
    for py in ("python3",):
        path = shutil.which(py) or ""
        if "conda" in path or "anaconda" in path:
            r.bad(f"python3 is Anaconda's ({path}), which clashes with the venv",
                  "run: conda deactivate   (and: conda config --set auto_activate_base false)")
    if os.environ.get("CONDA_PREFIX"):
        r.warn("a conda environment is active", "conda deactivate")


# ----------------------------------------------------------------- 4. python

def check_python(r, repo):
    r.section("4. Is the Python environment healthy?")
    venv = os.path.join(repo, ".venv")
    py = os.path.join(venv, "bin", "python")
    if not os.path.exists(py):
        r.bad(".venv missing or broken", f"cd {repo} && rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt")
        return
    code, out = sh([py, "-c", "import sys; print(sys.prefix)"])
    if code != 0 or os.path.realpath(out.strip()) != os.path.realpath(venv):
        r.bad(".venv is broken (it was copied from another place or another Linux)",
              f"cd {repo} && rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt")
        return
    r.ok(".venv works")
    active = os.environ.get("VIRTUAL_ENV", "")
    if os.path.realpath(active) == os.path.realpath(venv):
        r.ok(".venv is active in this terminal")
    else:
        r.warn(".venv is not active in this terminal (commands like pytest would use the wrong Python)",
               f"source {os.path.relpath(venv) if os.getcwd().startswith(repo) else venv}/bin/activate")
    code, out = sh([py, "-m", "pip", "freeze", "--disable-pip-version-check"])
    have = {}
    for line in out.splitlines():
        if "==" in line:
            k, v = line.split("==", 1)
            have[k.lower().replace("_", "-")] = v
    wrong = []
    for pkg, ver in PINS.items():
        got = have.get(pkg)
        if got != ver:
            wrong.append(f"{pkg} {got or 'missing'} (want {ver})")
    if wrong:
        r.bad("Python packages don't match requirements.txt: " + "; ".join(wrong),
              f"cd {repo} && source .venv/bin/activate && pip install -r requirements.txt")
    else:
        r.ok("cocotb and extensions at the tested versions")


# ----------------------------------------------------------------- 5. git

def check_git(r, repo):
    r.section("5. Git and GitHub")
    _, name = sh(["git", "config", "--global", "user.name"])
    _, email = sh(["git", "config", "--global", "user.email"])
    if name.strip() and email.strip():
        r.ok(f"git identity: {name.strip()} <{email.strip()}>")
    else:
        r.bad("git doesn't know who you are, so commits will fail",
              'git config --global user.name "Arunima"\n'
              'git config --global user.email "<your noreply address from github.com/settings/emails>"')
    _, crlf = sh(["git", "config", "--get", "core.autocrlf"])
    if crlf.strip() == "true":
        r.bad("git is set to convert line endings (breaks scripts)", "git config --global core.autocrlf input")
    elif not crlf.strip():
        r.warn("git line-ending setting not set", "git config --global core.autocrlf input")
    else:
        r.ok(f"git core.autocrlf = {crlf.strip()}")
    code, url = sh(["git", "remote", "get-url", "origin"], cwd=repo)
    if code != 0:
        r.warn("no GitHub remote yet (fine before the end of task 1.1)",
               "git remote add origin https://github.com/<your-username>/pcie-dma-verif.git")
    else:
        r.ok(f"GitHub remote: {url.strip()}")
        code, out = sh(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=repo)
        if code == 0 and out.strip().isdigit() and int(out.strip()) > 0:
            r.warn(f"{out.strip()} commit(s) not pushed to GitHub yet", "git push")
        elif code != 0:
            r.warn("this branch isn't linked to GitHub yet", "git push -u origin main")
    _, out = sh(["git", "status", "--porcelain"], cwd=repo)
    pending = [l for l in out.splitlines() if l.strip()]
    if pending:
        r.warn(f"{len(pending)} file(s) changed but not committed. That's fine mid-task; "
               "commit once the task checker passes",
               "git status        # to see them")


# ----------------------------------------------------------------- 6. progress

def check_progress(r, repo):
    r.section("6. Where am I?")
    if not os.path.isfile(os.path.join(repo, "tools", "check_task.py")):
        r.bad("tools/check_task.py missing", "copy it from your Downloads folder (see task 1.1)")
    log = os.path.join(repo, "docs", "logs", "checks.log")
    passed = set()
    if os.path.isfile(log):
        for line in open(log, errors="replace"):
            m = re.search(r"task (\d\.\d)\s+PASS", line)
            if m:
                passed.add(m.group(1))
    nxt = next((t for t in TASK_ORDER if t not in passed), None)
    done = len([t for t in TASK_ORDER if t in passed])
    if nxt:
        r.ok(f"{done} of {len(TASK_ORDER)} tasks passed the checker. Next task: {nxt}")
        skipped = [t for t in TASK_ORDER[TASK_ORDER.index(nxt) + 1:] if t in passed]
        if skipped:
            r.warn(f"later tasks passed while {nxt} hasn't: {', '.join(skipped)}",
                   f"python3 tools/check_task.py {nxt}")
    else:
        r.ok("all 24 tasks passed. Project complete")
    return nxt


def check_disk(r):
    try:
        free = shutil.disk_usage(os.path.expanduser("~")).free
    except OSError:
        return
    if free < 3 * 1024 ** 3:
        r.warn(f"only {free // 1024 ** 3} GB free; Verilator builds need a few GB",
               "rm -rf ~/pcie-dma-verif/tb/dma_ext/sim_build ~/pcie-dma-verif/perf/sim_build")


# ----------------------------------------------------------------- install

BASHRC_MARK = "# >>> pcie-dma-verif helper >>>"
BASHRC_BLOCK = f"""
{BASHRC_MARK}
pcie() {{
  if [ -d ~/{REPO_NAME} ]; then
    cd ~/{REPO_NAME}
    [ -f .venv/bin/activate ] && source .venv/bin/activate
  fi
  python3 ~/.local/bin/pcie-doctor
}}
# <<< pcie-dma-verif helper <<<
"""


def install():
    dst = os.path.expanduser("~/.local/bin/pcie-doctor")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(os.path.abspath(__file__), dst)
    os.chmod(dst, 0o755)
    rc = os.path.expanduser("~/.bashrc")
    text = open(rc).read() if os.path.isfile(rc) else ""
    if BASHRC_MARK not in text:
        with open(rc, "a") as f:
            f.write(BASHRC_BLOCK)
    print("Installed.\n")
    print("  Now run:  source ~/.bashrc")
    print("  From then on, every session: open \"Ubuntu 24.04\", type  pcie  and press Enter.")
    print("  It goes to the repo, turns on the venv and runs these checks.")


def main():
    if "--install" in sys.argv:
        install()
        return 0
    r = Report()
    ctx = check_linux(r)
    repo = check_repo(r, ctx)
    if ctx.get("extra") and repo and not repo.startswith("/mnt/"):
        r.warn(f"your repo is safely in {WANT_DISTRO}, and other Linux installs still exist: "
               f"{', '.join(ctx['extra'])}. Easy to open the wrong one by mistake",
               "when you're sure nothing else you need is in them, in PowerShell:\n"
               f"  wsl --set-default {WANT_DISTRO}\n"
               + "\n".join(f"  wsl --unregister {n}      # deletes {n} and everything in it" for n in ctx["extra"]))
    check_tools(r)
    nxt = None
    if repo:
        check_python(r, repo)
        check_git(r, repo)
        nxt = check_progress(r, repo)
    check_disk(r)
    r.show()
    blockers = r.blocking
    print()
    if blockers:
        print(c(f"NOT READY: fix the {len(blockers)} item(s) marked FIX above, top to bottom, then run this again.", "1;31"))
        return 1
    print(c("READY.", "1;32") + (f" Next: open task {nxt} in the tracker. When done:  python3 tools/check_task.py {nxt}" if nxt else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
