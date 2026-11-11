#!/usr/bin/env python3
"""Independent checker for the PCIe DMA project tracker.

Run it after you finish a task and before you commit:

    python3 tools/check_task.py 3.3          # check one task
    python3 tools/check_task.py --day 3      # every task on day 3, stops at the first failure
    python3 tools/check_task.py --list       # show all tasks

For each task it re-runs the checkpoint itself instead of trusting your notes,
compares results against the known-good numbers, and checks that nothing was
weakened to make a test pass. Where a task adds a checker, it injects a bug into
a throwaway copy of the upstream RTL and confirms your checker catches it
(mutation testing). Learning tasks end with a short quiz; answers are stored
as hashes, so reading this file won't give them away.

Only the Python standard library is used. Simulations run through .venv.
Results are appended to docs/logs/checks.log, so your commit history shows
the evidence for every ticked task.
"""

import argparse
import datetime
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UP = os.path.join(ROOT, "third_party", "verilog-pcie")
PY = os.path.join(ROOT, ".venv", "bin", "python")
LOG = os.path.join(ROOT, "docs", "logs", "checks.log")
USE_COLOR = sys.stdout.isatty()


def color(s, c):
    return f"\033[{c}m{s}\033[0m" if USE_COLOR else s


# --------------------------------------------------------------------------- helpers

class Result:
    def __init__(self):
        self.items = []          # (status, name, detail)  status: PASS FAIL WARN

    def ok(self, name, detail=""):
        self.items.append(("PASS", name, detail))

    def fail(self, name, detail=""):
        self.items.append(("FAIL", name, detail))

    def warn(self, name, detail=""):
        self.items.append(("WARN", name, detail))

    def check(self, cond, name, detail_fail="", detail_ok=""):
        (self.ok if cond else self.fail)(name, detail_ok if cond else detail_fail)
        return cond

    @property
    def passed(self):
        return not any(s == "FAIL" for s, _, _ in self.items)


def run(cmd, cwd=ROOT, env_extra=None, timeout=1800):
    env = dict(os.environ)
    env.pop("SIM", None)
    env.pop("CHK_MPS", None)
    env.update({"SEED": "1", "N_XFERS": "200", "WAVES": "0"})
    if env_extra:
        env.update(env_extra)
    env["PATH"] = os.path.join(ROOT, ".venv", "bin") + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = os.path.join(ROOT, ".venv")
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, str(e)


def pytest(args, cwd=ROOT, env_extra=None, timeout=1800):
    return run([PY, "-m", "pytest", "-p", "no:cacheprovider", "-o", "log_cli=true",
                "--log-cli-level=INFO"] + args, cwd=cwd, env_extra=env_extra, timeout=timeout)


def summary_line(out):
    m = re.findall(r"=+ (.*?(?:passed|failed|error|skipped).*?) in [\d.]+s", out)
    return m[-1] if m else out.strip().splitlines()[-1][:200] if out.strip() else "(no output)"


def read(path):
    try:
        with open(os.path.join(ROOT, path), errors="replace") as f:
            return f.read()
    except OSError:
        return None


def exists(r, *paths):
    good = True
    for p in paths:
        good &= r.check(os.path.isfile(os.path.join(ROOT, p)), f"file {p} exists", "missing")
    return good


def words(text):
    return len(re.findall(r"[A-Za-z0-9_]+", text or ""))


def tail(out, n=12):
    lines = [l for l in out.splitlines() if l.strip()]
    return "\n".join("        " + l[-160:] for l in lines[-n:])


def parse_summary(path):
    txt = read(path)
    if txt is None:
        return None
    d = {}
    for line in txt.splitlines():
        m = re.match(r"\s*(.+?)\s*:\s*(\d+)\s*$", line)
        if m:
            d[m.group(1)] = int(m.group(2))
    return d


# --------------------------------------------------------------------------- mutation testing

MUTANTS = {
    "data_flip": ("rtl/dma_if_pcie_rd.v",
                  "= ram_wr_cmd_data_reg;",
                  "= ram_wr_cmd_data_reg ^ {{(RAM_SEG_DATA_WIDTH-1){1'b0}}, 1'b1};",
                  "read engine flips bit 0 of every RAM write"),
    "mps_x2": ("rtl/dma_if_pcie_wr.v",
               "max_payload_size_dw_reg <= 11'd32 <<",
               "max_payload_size_dw_reg <= 11'd64 <<",
               "write engine sends MWr payloads twice the programmed MPS"),
    "mrrs_x2": ("rtl/dma_if_pcie_rd.v",
                "max_read_request_size_dw_reg <= 11'd32 <<",
                "max_read_request_size_dw_reg <= 11'd64 <<",
                "read engine sends MRds twice the programmed MRRS"),
}


def mutant_copy(name):
    """Copy the repo (minus build output) to a temp dir and inject one RTL bug."""
    rel, old, new, _ = MUTANTS[name]
    tmp = tempfile.mkdtemp(prefix=f"mutant_{name}_")
    dst = os.path.join(tmp, "repo")
    shutil.copytree(ROOT, dst, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "sim_build", "__pycache__", "*.fst"))
    f = os.path.join(dst, "third_party", "verilog-pcie", rel)
    src = open(f).read()
    if src.count(old) != 1:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"mutation point for {name} not found exactly once in {rel}")
    open(f, "w").write(src.replace(old, new))
    return tmp, dst


def expect_killed(r, mutant, test_args, must_contain, env_extra=None):
    """Run tests against a mutant; PASS means the tests FAILED for the right reason."""
    desc = MUTANTS[mutant][3]
    try:
        tmp, dst = mutant_copy(mutant)
    except Exception as e:
        r.fail(f"mutation '{mutant}' ({desc})", str(e))
        return
    try:
        env = {"N_XFERS": "5"}
        env.update(env_extra or {})
        code, out = pytest(test_args, cwd=dst, env_extra=env)
        caught = code != 0 and must_contain in out
        detail = (f"tests failed with '{must_contain}' as they should" if caught else
                  f"exit {code}; expected a failure containing '{must_contain}'\n{tail(out)}")
        r.check(caught, f"mutation '{mutant}' is caught: {desc}", detail, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- quiz

def h(ans):
    return hashlib.sha256(ans.encode()).hexdigest()[:16]


def normalize(ans, kind):
    a = ans.strip().lower().replace(" ", "").replace("_", "")
    if kind == "int":
        a = re.sub(r"(bytes|byte|b|gb/s|gbps)$", "", a) if not a.startswith("0x") else a
        try:
            return str(int(a, 0))
        except ValueError:
            return a
    if kind == "pct":
        try:
            v = float(a.rstrip("%"))
            return str(round(v * 100 if v <= 1 else v))
        except ValueError:
            return a
    return a[:1]          # multiple choice: first letter


def quiz(r, questions):
    """questions: list of (prompt, kind, sha256-prefix-of-normalized-answer)."""
    if not sys.stdin.isatty():
        r.warn("quiz", "skipped (not an interactive terminal); run this task in a terminal to take it")
        return
    print(color("\n  Quiz: answer from memory, no notes.", "1"))
    right = 0
    for i, (q, kind, ans_hash) in enumerate(questions, 1):
        hint = {"int": " (number; 0x... ok)", "pct": " (percent, nearest whole number)", "mc": " (letter)"}[kind]
        print(f"\n  Q{i}. {q}{hint}")
        given = input("  > ")
        good = h(normalize(given, kind)) == ans_hash
        right += good
        print("     " + (color("correct", "32") if good else color("not quite: revisit this before ticking", "31")))
    r.check(right == len(questions), f"quiz {right}/{len(questions)} correct", "every answer must be right")


# --------------------------------------------------------------------------- hygiene: runs before every task

def hygiene(r):
    r.check(not ROOT.startswith("/mnt/"), "repo lives in the Linux filesystem, not /mnt/c",
            f"repo is at {ROOT}; move it under ~/ (Windows-mounted paths are slow and break symlinks)")
    code, out = run("git config --get core.autocrlf")
    r.check(out.strip() not in ("true",), "git core.autocrlf is not 'true'",
            "run: git config --global core.autocrlf input")
    if os.path.isdir(os.path.join(UP, ".git")) or os.path.isfile(os.path.join(UP, ".git")):
        code, out = run(["git", "-C", UP, "status", "--porcelain", "--untracked-files=no"])
        r.check(code == 0 and out.strip() == "", "upstream submodule is unmodified",
                "you edited upstream files:\n" + tail(out))
    hits = []
    for pat in ("tb/**/*.py", "sv/**/*", "perf/**/*.py", "pynq/**/*.py", "pynq/**/*.v"):
        for f in glob.glob(os.path.join(ROOT, pat), recursive=True):
            if "sim_build" in f or not os.path.isfile(f):
                continue
            txt = open(f, errors="replace").read()
            rel = os.path.relpath(f, ROOT)
            if "SABOTAGE" in txt:
                hits.append(f"{rel}: SABOTAGE line left in")
            if re.search(r"pytest\.mark\.(skip|xfail)\b|pytest\.skip\(", txt) and not rel.endswith("test_checker_negative.py"):
                hits.append(f"{rel}: skip/xfail added")
            if re.search(r'getenv\("N_XFERS",\s*"(?!200")', txt):
                hits.append(f"{rel}: N_XFERS default changed from 200")
    r.check(not hits, "no sabotage left, no skipped tests, no shrunken test sizes", "\n        ".join(hits))


# --------------------------------------------------------------------------- tasks

def t11(r):
    code, out = run("iverilog -V 2>&1 | head -1")
    r.check(re.search(r"version 1[1-9]", out) is not None, "Icarus Verilog 11+ installed", out.strip() or "not found")
    code, out = run("verilator --version")
    r.check(re.search(r"Verilator 5\.", out) is not None, "Verilator 5.x installed", out.strip() or "not found")
    if not r.check(os.path.isfile(PY), ".venv exists at repo root", "create it: python3 -m venv .venv"):
        return
    code, out = run([PY, "-c", "import cocotb, cocotbext.pcie, cocotbext.axi, cocotb_test; print(cocotb.__version__)"])
    r.check(code == 0 and out.strip().startswith("1."), "cocotb 1.x and extensions import in .venv",
            out.strip()[-300:], f"cocotb {out.strip()}")
    req = read("requirements.txt") or ""
    r.check("cocotb==1." in req and "cocotbext-pcie" in req, "requirements.txt pins cocotb 1.x and cocotbext-pcie")
    gi = read(".gitignore") or ""
    r.check("sim_build" in gi and ".venv" in gi, ".gitignore excludes sim_build/ and .venv/")
    ini = read("pytest.ini") or ""
    r.check("third_party" in ini, "pytest.ini skips third_party")
    gm = read(".gitmodules") or ""
    r.check("alexforencich/verilog-pcie" in gm, ".gitmodules points at alexforencich/verilog-pcie")
    r.check(os.path.isfile(os.path.join(UP, "rtl", "dma_if_pcie_rd.v")), "submodule checked out (rtl/ present)",
            "run: git submodule update --init")
    r.check(os.path.islink(os.path.join(UP, "tb", "dma_if_pcie_rd", "pcie_if.py")),
            "upstream symlinks intact", "tb/dma_if_pcie_rd/pcie_if.py is not a symlink: clone inside WSL, not Windows")
    code, out = run("git remote get-url origin")
    (r.ok if code == 0 else r.warn)("GitHub remote 'origin' set", out.strip())


def t12(r):
    tests = ["tb/dma_if_pcie_rd/test_dma_if_pcie_rd.py::test_dma_if_pcie_rd[64-0]",
             "tb/dma_if_pcie_wr/test_dma_if_pcie_wr.py::test_dma_if_pcie_wr[64-0]",
             "tb/pcie_axil_master/test_pcie_axil_master.py::test_pcie_axil_master[64-32]"]
    code, out = pytest(["-n", "auto"] + tests, cwd=UP)
    r.check(code == 0 and "3 passed" in out, "re-ran upstream quick set: 3 passed", summary_line(out), summary_line(out))
    log = read("docs/logs/day1_baseline.txt")
    if r.check(log is not None, "docs/logs/day1_baseline.txt exists", "save the full run with | tee"):
        m = re.search(r"(\d+) passed", log)
        n = int(m.group(1)) if m else 0
        bad = re.search(r"\d+ (failed|error)", log)
        r.check(n >= 68 and not bad, "full upstream run: 68 passed, none failed",
                f"log shows {n} passed{', and ' + bad.group(0) if bad else ''}", f"{n} passed")


def t13(r):
    p = os.path.join(ROOT, "docs", "img", "first_tlp.png")
    good = os.path.isfile(p) and open(p, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    r.check(good, "docs/img/first_tlp.png is a real PNG", "missing or not a PNG")
    quiz(r, [
        ("An MRd header has DW0 = 0x00000040 and DW1 = 0x01002AFF. How many bytes does it request?", "int", "51e8ea280b44e169"),
        ("Same MRd: what is its Tag, in decimal?", "int", "73475cb40a568e8d"),
        ("Same MRd: (a) 3DW header, 32-bit address  (b) 4DW header, 64-bit address", "mc", "ca978112ca1bbdca"),
        ("A CplD has DW1 = 0x00000080 and Length = 32 DW, Lower Address 0. Is it the last completion "
         "for its request? (y/n)", "mc", "a1fce4363854ff88"),
    ])


def t14(r):
    txt = read("docs/01_pcie_notes.md")
    if not r.check(txt is not None, "docs/01_pcie_notes.md exists"):
        return
    heads = re.findall(r"^##\s+\S", txt, re.M)
    r.check(len(heads) >= 6, f"notes have {len(heads)} sections (need 6+)", "use the outline from the tracker")
    r.check(words(txt) >= 400, f"notes are {words(txt)} words (need 400+, in your own words)")
    quiz(r, [
        ("A 600 B read starts at 0x0FC0 with MRRS = 512. How many MRd TLPs?", "int", "4e07408562bedb8b"),
        ("Same read: what is the address of the last MRd? ", "int", "d6882ad218ce73f7"),
        ("Same read: how many bytes does the last MRd request?", "int", "c2356069e9d1e79c"),
        ("Which TLP type gets no completion? (a) MRd  (b) MWr  (c) CfgRd", "mc", "3e23e8160039594a"),
        ("Who sets a device's Max Payload Size? (a) the device itself at reset  (b) system software "
         "writing Device Control  (c) the root complex hardware automatically on each TLP", "mc", "3e23e8160039594a"),
    ])


def t21(r):
    txt = read("docs/02_read_path.md")
    if not r.check(txt is not None, "docs/02_read_path.md exists"):
        return
    refs = re.findall(r"\b(?:L|line\s*)\d{3,4}\b", txt, re.I)
    r.check(len(refs) >= 4, f"cites {len(refs)} RTL line numbers (need 4+)", "write them as L663, L1005 ...")
    for key in ("byte_count", "tag", "4k|4 kb|0xfff|12'hfff|1000"):
        r.check(re.search(key, txt, re.I) is not None, f"notes cover '{key.split('|')[0]}'")
    quiz(r, [
        ("A 256 B read's tag table holds RAM end address 0x1100. A CplD for it arrives with Byte Count 128. "
         "At what RAM address does its data start?", "int", "a24cc8ec4a0372f4"),
        ("Why can completions for one request arrive in any order and still land correctly? "
         "(a) the DUT reorders them in a FIFO  (b) each CplD's Byte Count says how far from the end its data goes  "
         "(c) the root complex never splits completions", "mc", "3e23e8160039594a"),
    ])


def t22(r):
    docs = [f for f in glob.glob(os.path.join(ROOT, "docs", "*.md")) if "max_payload" in open(f).read()]
    r.check(bool(docs), "a docs/*.md file covers the write path (mentions max_payload_size)",
            "add your write-path notes, e.g. docs/03_write_path.md")
    quiz(r, [
        ("Device Control MPS field = 2. What is the max payload in bytes?", "int", "94f8607915dff25f"),
        ("With MPS = 256, how many MWr TLPs does a 1000 B write starting at 0x1F80 need?", "int", "ef2d127de37b942b"),
    ])


def t23(r):
    docs = [f for f in glob.glob(os.path.join(ROOT, "docs", "*.md"))
            if "RootComplex" in open(f).read() and "PcieIfDevice" in open(f).read()]
    r.check(bool(docs), "a docs/*.md file explains RootComplex and PcieIfDevice")
    quiz(r, [
        ("In the cocotb testbench, which model owns host memory? (a) PcieIfDevice  (b) RootComplex  "
         "(c) PsdpRamWrite", "mc", "3e23e8160039594a"),
        ("Which call makes the host scan the bus and assign BARs? (a) rc.enumerate()  (b) dev.enable_device()  "
         "(c) mem_pool.alloc_region()", "mc", "ca978112ca1bbdca"),
    ])


def t24(r):
    txt = read("README.md")
    if not r.check(txt is not None, "README.md exists"):
        return
    r.check("```mermaid" in txt, "README has a Mermaid diagram")
    m = re.search(r"```mermaid.*?```(.*?)\n## ", txt, re.S)
    below = m.group(1) if m else ""
    r.check("TODO(you)" not in below and words(below) >= 30, "diagram is explained in your own words (30+ words)",
            "replace the TODO under the diagram")


def ext_summary(cfg, name, sim="icarus"):
    return parse_summary(f"tb/dma_ext/sim_build/{sim}/{cfg}/tlp_summary_{name}.txt")


def t31(r):
    exists(r, "tb/dma_ext/conftest.py", "tb/dma_ext/common.py", "tb/dma_ext/test_rd_ext.py")
    code, out = pytest(["tb/dma_ext/test_rd_ext.py::test_dma_rd_ext[512]"], env_extra={"N_XFERS": "20"})
    r.check(code == 0 and "1 passed" in out, "read tests pass for MRRS 512 (20 transfers)", summary_line(out) + tail(out, 6))
    s = ext_summary("test_dma_rd_ext-512", "smoke")
    r.check(s is not None and s.get("MRd sent", 0) >= 1 and s.get("rule violations", 1) == 0,
            "smoke test wrote a TLP summary with MRds and 0 violations")


def t32(r):
    code, out = pytest(["tb/dma_ext/test_rd_ext.py::test_dma_rd_ext[512]"])
    r.check(code == 0, "200 random reads pass (SEED=1)", summary_line(out) + tail(out, 6))
    expect_killed(r, "data_flip", ["tb/dma_ext/test_rd_ext.py::test_dma_rd_ext[512]"], "mismatch")


def t33(r):
    exists(r, "tb/dma_ext/test_wr_ext.py")
    code, out = pytest(["-n", "auto", "tb/dma_ext/test_rd_ext.py", "tb/dma_ext/test_wr_ext.py"])
    r.check(code == 0 and "6 passed" in out, "all 6 MPS/MRRS configurations pass", summary_line(out))
    got = []
    for m in (128, 256, 512):
        s = ext_summary(f"test_dma_rd_ext-{m}", "random_reads")
        got.append(s.get("MRd sent") if s else None)
    r.check(got == [6959, 3576, 1883], "MRd counts for MRRS 128/256/512 = 6959/3576/1883",
            f"got {got}: different numbers mean the request splitting or your stimulus differs")


def t34(r):
    bins = ["MRd sent", "CplD received", "distinct tags used", "requests ending on 4 KB boundary", "multi-CplD requests"]
    s = ext_summary("test_dma_rd_ext-512", "random_reads")
    if r.check(s is not None, "read summary present (run 3.3 first)"):
        zero = [b for b in bins if s.get(b, 0) == 0]
        r.check(not zero, "every coverage bin is nonzero", f"empty bins: {zero}")
    w = ext_summary("test_dma_wr_ext-512", "random_writes")
    r.check(w is not None and w.get("MWr sent", 0) > 0, "write summary has MWrs")
    expect_killed(r, "mps_x2", ["tb/dma_ext/test_wr_ext.py::test_dma_wr_ext[256]"], "exceeds MPS")
    expect_killed(r, "mrrs_x2", ["tb/dma_ext/test_rd_ext.py::test_dma_rd_ext[256]"], "exceeds MRRS")


def t35(r):
    code, out = pytest(["tb/dma_ext/test_rd_ext.py::test_dma_rd_ext[512]"])
    r.check(code == 0, "read tests pass, including concurrent reads", summary_line(out))
    s = ext_summary("test_dma_rd_ext-512", "concurrent_reads")
    if r.check(s is not None, "concurrent_reads summary present"):
        r.check(s.get("max tags in flight") == 31, f"max tags in flight = {s.get('max tags in flight')} (expect 31)",
                "throttling or descriptor queueing differs from the walkthrough")
        r.check(s.get("rule violations") == 0, "0 rule violations")


SV_PROBE = r"""
module probe;
  import tlp_pkg::*;
  req_hdr_t h;
  initial begin
    $display("BITS %0d %0d %0d", $bits(dw0_t), $bits(req_hdr_t), $bits(cpl_hdr_t));
    h = '0; h.dw0.fmt_type = MRD_32; h.dw0.length = 10'd1; h.first_be = 4'b0110;
    $display("BC1 %0d", req_byte_count(h));
    h.dw0.length = 10'd2; h.first_be = 4'b1110; h.last_be = 4'b0111;
    $display("BC2 %0d", req_byte_count(h));
    h.dw0.length = 10'd0; h.first_be = 4'b1111; h.last_be = 4'b1111;
    $display("BC3 %0d", req_byte_count(h));
    h.addr_raw = {30'h0000_0400, 2'b00, 32'd0};
    $display("ADDR %0h HDR %0d", req_addr(h), hdr_dw(h.dw0));
    h.dw0.fmt_type = MWR_64; h.addr_raw = 64'h1_0000_0040;
    $display("ADDR64 %0h HDR %0d POSTED %0d", req_addr(h), hdr_dw(h.dw0), is_posted(h.dw0));
    $finish;
  end
endmodule
"""


def t41(r):
    if not exists(r, "sv/tlp_pkg.sv"):
        return
    code, out = run("verilator --lint-only -Wall sv/tlp_pkg.sv")
    r.check(code == 0 and "%" not in out, "verilator -Wall lint is silent", tail(out))
    tmp = tempfile.mkdtemp(prefix="pkgprobe_")
    try:
        open(os.path.join(tmp, "probe.sv"), "w").write(SV_PROBE)
        code, out = run(["verilator", "--binary", "-Wno-fatal", "-Wno-lint", "--top-module", "probe",
                         os.path.join(ROOT, "sv", "tlp_pkg.sv"), os.path.join(tmp, "probe.sv"),
                         "--Mdir", os.path.join(tmp, "obj")], timeout=600)
        if not r.check(code == 0, "unit probe of tlp_pkg compiles", tail(out)):
            return
        code, out = run([os.path.join(tmp, "obj", "Vprobe")])
        r.check("BITS 32 128 128" in out, "struct widths: dw0_t 32, req_hdr_t 128, cpl_hdr_t 128", tail(out, 3))
        r.check("BC1 2" in out, "req_byte_count: 1 DW, first BE 0110 -> 2 bytes", tail(out, 6))
        r.check("BC2 6" in out, "req_byte_count: 2 DW, BEs 1110/0111 -> 6 bytes", tail(out, 6))
        r.check("BC3 4096" in out, "req_byte_count: Length 0 means 1024 DW -> 4096 bytes", tail(out, 6))
        r.check("ADDR 1000 HDR 3" in out, "req_addr / hdr_dw for a 3DW request", tail(out, 6))
        r.check("ADDR64 100000040 HDR 4 POSTED 1" in out, "req_addr / hdr_dw / is_posted for a 4DW MWr", tail(out, 6))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t42(r):
    if not exists(r, "sv/tlp_checker.sv"):
        return
    code, out = run("verilator --lint-only -Wall sv/tlp_pkg.sv sv/tlp_checker.sv --top-module tlp_checker")
    r.check(code == 0 and "%" not in out, "verilator -Wall lint is silent", tail(out))
    src = read("sv/tlp_checker.sv")
    for lbl in ("R1_MPS", "R2_MRRS", "R3_4KB", "R4_TAG", "R5_CPL_BC"):
        r.check(re.search(rf"\b{lbl}\s*:\s*assert", src) is not None, f"labeled assertion {lbl} present")
    body = re.sub(r"//.*", "", src)
    r.check(re.search(r"\boutput\b", body) is None, "checker is passive (no output ports)")


def t43(r):
    if not exists(r, "sv/bind_rd.sv", "sv/bind_wr.sv", "sv/chk_defines.svh"):
        return
    code, out = pytest(["-n", "auto", "tb/dma_ext/test_rd_ext.py", "tb/dma_ext/test_wr_ext.py"],
                       env_extra={"SIM": "verilator"})
    r.check(code == 0 and "6 passed" in out and "%Error" not in out,
            "all 6 configurations pass on Verilator with the checker bound", summary_line(out) + tail(out, 6))
    expect_killed(r, "mrrs_x2", ["tb/dma_ext/test_rd_ext.py::test_dma_rd_ext[256]"], "[R2_MRRS]",
                  env_extra={"SIM": "verilator"})


def t44(r):
    if not exists(r, "tb/dma_ext/test_checker_negative.py"):
        return
    code, out = pytest(["tb/dma_ext/test_checker_negative.py", "-v"], env_extra={"SIM": "verilator"})
    r.check(code == 0 and "PASSED" in out, "negative test passes (sabotaged build failed on R1_MPS)",
            summary_line(out) + tail(out, 6))
    expect_killed(r, "mps_x2", ["tb/dma_ext/test_wr_ext.py::test_dma_wr_ext[256]"], "[R1_MPS]",
                  env_extra={"SIM": "verilator"})


def t51(r):
    if not exists(r, "perf/perf_bench.py", "perf/test_perf.py", "perf/conftest.py"):
        return
    code, out = pytest(["perf", "-n", "auto"])
    r.check(code == 0 and "2 passed" in out, "perf sweep runs", summary_line(out))
    csv = read("perf/results.csv") or ""
    rows = [l.split(",") for l in csv.strip().splitlines()[1:]]
    r.check(len(rows) == 14, f"results.csv has {len(rows)} rows (expect 14)")
    try:
        col = csv.splitlines()[0].split(",").index("gen3x8_effective_gbps")
        get = {(x[0], int(x[2])): float(x[col]) for x in rows}
        wr = [get[("wr", s)] for s in (256, 512, 1024, 2048, 4096)]
        rd = [get[("rd", s)] for s in (256, 512, 1024, 2048, 4096)]
        r.check(all(abs(v - 57.61) < 0.05 for v in wr), "writes at >=256 B: 57.61 Gb/s", f"got {wr}")
        r.check(all(abs(v - 54.50) < 0.05 for v in rd), "reads at >=256 B: 54.5 Gb/s", f"got {rd}")
        r.check(abs(get[("wr", 64)] - 45.83) < 0.05, "writes at 64 B: 45.83 Gb/s", f"got {get[('wr', 64)]}")
    except Exception as e:
        r.fail("results.csv has the expected columns", str(e))


def t52(r):
    quiz(r, [
        ("Write efficiency for 64 B MWrs with 24 B overhead each?", "pct", "96061e92f58e4bdc"),
        ("If MPS were raised to 512 B, what would write efficiency be at large sizes?", "pct", "7b1a278f5abe8e9d"),
        ("If the completer's MPS were 256 B, what would read efficiency be at large sizes "
         "(20 B overhead per CplD)?", "pct", "6e4001871c0cf27c"),
        ("Gen3 x8 raw bandwidth per direction, in whole Gb/s (8 GT/s, 128b/130b)?", "int", "da4ea2a5506f2693"),
    ])


def t53(r):
    for p in ("perf/throughput.png", "perf/latency.png"):
        f = os.path.join(ROOT, p)
        r.check(os.path.isfile(f) and open(f, "rb").read(8) == b"\x89PNG\r\n\x1a\n", f"{p} generated")
    txt = read("README.md") or ""
    m = re.search(r"## Performance(.*?)\n## ", txt, re.S)
    sec = m.group(1) if m else ""
    r.check("TODO(you)" not in sec and words(sec) >= 60, "README Performance section written (60+ words)")


def t61(r):
    if not exists(r, "pynq/rtl/pynq_dma_top.v", "pynq/tb/test_pynq_dma_top.py"):
        return
    code, out = pytest(["pynq/tb"])
    sizes = re.findall(r"loopback\s+(\d+) B ok", out)
    r.check(code == 0 and sizes == ["1", "7", "64", "256", "1000", "4096", "16384"],
            "PYNQ top-level loopback ok for 1 B to 16 KB", f"got {sizes}\n{tail(out, 6)}")


def t62(r):
    for p, minsize in (("pynq/overlay/pcie_dma.bit", 1_000_000), ("pynq/overlay/pcie_dma.hwh", 10_000)):
        f = os.path.join(ROOT, p)
        r.check(os.path.isfile(f) and os.path.getsize(f) > minsize, f"{p} present")
    hwh = read("pynq/overlay/pcie_dma.hwh") or ""
    r.check("pynq_dma_top" in hwh, "hwh contains the pynq_dma_top block")
    rpt = read("docs/logs/timing_summary.rpt")
    if r.check(rpt is not None, "docs/logs/timing_summary.rpt saved",
               "in Vivado: Open Implemented Design, then report_timing_summary -file <repo>/docs/logs/timing_summary.rpt"):
        m = re.search(r"WNS\(ns\).*?\n[\s-]+\n\s*(-?\d+\.\d+)", rpt, re.S)
        wns = float(m.group(1)) if m else None
        r.check(wns is not None and wns >= 0, f"timing met (WNS = {wns} ns)", "WNS negative or not found")


def t63(r):
    log = read("docs/logs/board_loopback.txt")
    if not r.check(log is not None, "docs/logs/board_loopback.txt saved",
                   "on the board: sudo python3 dma_loopback.py | tee board_loopback.txt, then copy it back"):
        return
    rows = re.findall(r"^\s*(\d+)\s+(True|False)", log, re.M)
    sizes = {int(s) for s, ok in rows if ok == "True"}
    r.check({256, 1024, 4096, 16384} <= sizes and all(ok == "True" for _, ok in rows),
            "board loopback matches for 256 B, 1 KB, 4 KB, 16 KB", f"rows: {rows}")


def t64(r):
    txt = read("README.md") or ""
    r.check("TODO(you)" not in txt, "no TODO(you) left in README")
    code, out = run("git tag --list v1.0")
    r.check(out.strip() == "v1.0", "tag v1.0 exists")


TASKS = {
    "1.1": ("Scaffold the repo and Python environment", t11, "chore: scaffold repo, add verilog-pcie submodule and Python env"),
    "1.2": ("Run the upstream regression", t12, "test: record passing upstream dma_if_pcie baseline"),
    "1.3": ("Dump and read one waveform", t13, "docs: add first annotated MRd/CplD waveform"),
    "1.4": ("Study TLPs and ordering", t14, "docs: PCIe TLP format and ordering notes"),
    "2.1": ("Walk the read path", t21, "docs: annotate dma_if_pcie_rd read path"),
    "2.2": ("Walk the write path", t22, "docs: annotate dma_if_pcie_wr write path"),
    "2.3": ("Take apart the testbench", t23, "docs: notes on upstream cocotb testbench structure"),
    "2.4": ("README with block diagram", t24, "docs: add README with DMA datapath block diagram"),
    "3.1": ("Stand up your own test folder", t31, "test: add dma_ext testbench running against upstream RTL"),
    "3.2": ("Random transfer sweep with scoreboard", t32, "test: randomized read sweep with byte-level scoreboard"),
    "3.3": ("Write path and MPS/MRRS sweep", t33, "test: add write path and parametrize MPS/MRRS across 128-512 B"),
    "3.4": ("TLP monitor and coverage summary", t34, "test: add TLP monitor with transaction coverage summary"),
    "3.5": ("Completion ordering, many tags in flight", t35, "test: verify completion ordering and tag reuse under concurrent reads"),
    "4.1": ("Write tlp_pkg.sv", t41, "feat(sv): add TLP header package with typed fields"),
    "4.2": ("Write tlp_checker.sv", t42, "feat(sv): add TLP protocol checker for MPS, MRRS, 4KB, tag and CplD rules"),
    "4.3": ("Bind it and run under Verilator", t43, "test: bind TLP checker and run regression on Verilator"),
    "4.4": ("Prove the checker fires", t44, "test: add negative test confirming checker catches MPS violation"),
    "5.1": ("Throughput and latency sweep", t51, "perf: add throughput and latency sweep 64B-4KB"),
    "5.2": ("Check the numbers by hand", t52, "docs: perf derivations"),
    "5.3": ("Plots and write-up", t53, "docs: add perf plots and analysis to README"),
    "6.1": ("Simulate the PYNQ top level", t61, "feat(pynq): top level with dma_if_axi, BRAM and AXI-Lite registers, plus sim"),
    "6.2": ("Vivado block design", t62, "feat(pynq): Vivado block design for PYNQ-Z2"),
    "6.3": ("Run it on the board", t63, "feat(pynq): board script for DDR-to-DDR DMA loopback"),
    "6.4": ("Record results and wrap up", t64, "docs: PYNQ results and project summary"),
}

INTERVIEW = {
    "1.1": ["Why keep upstream as a submodule instead of copying it in?"],
    "1.2": ["Why does upstream test PCIe offsets 4092-4095 specifically?"],
    "1.3": ["Walk me through the header fields of the MRd you decoded."],
    "1.4": ["Why must a read never cross a 4 KB boundary?", "What is the difference between MPS and MRRS?"],
    "2.1": ["How does the read engine place out-of-order completions into RAM?", "What limits how many reads it keeps in flight?"],
    "2.2": ["Why does a DMA write report done before the data reaches host memory?"],
    "2.3": ["What does the root complex model give you that a plain memory model wouldn't?"],
    "2.4": ["Draw the read datapath from descriptor to RAM write."],
    "3.1": ["How does your monitor observe TLPs without touching the DUT?"],
    "3.2": ["How do you know your scoreboard can actually fail?", "How would you reproduce a random failure?"],
    "3.3": ["Why does MRd count halve when MRRS doubles, and why does that matter?"],
    "3.4": ["What coverage would you add next?"],
    "3.5": ["Why did only one tag stay in flight until you throttled completions?"],
    "4.1": ["What happens if a packed struct cast doesn't match the vector width?"],
    "4.2": ["Why immediate assertions in always_ff rather than concurrent SVA here?", "How does the checker know when a tag is free again?"],
    "4.3": ["What does bind do, and why use it for assertion IP?"],
    "4.4": ["What is mutation testing and what did it prove about your checker?"],
    "5.1": ["Why is DUT throughput flat while link efficiency is not?"],
    "5.2": ["Why do reads level off lower than writes in your results?"],
    "5.3": ["What would you change to raise read efficiency?"],
    "6.1": ["What changes between dma_if_pcie and dma_if_axi, and what stays the same?"],
    "6.2": ["Why HP0 and not the ACP port?", "What sets the 100 MHz limit here?"],
    "6.3": ["Why do you need flush() and invalidate() around the DMA?"],
    "6.4": ["What would you do next with another two weeks?"],
}


def print_result(tid, r):
    title, _, commit = TASKS[tid]
    print(color(f"\nTask {tid}  {title}", "1"))
    for status, name, detail in r.items:
        c = {"PASS": "32", "FAIL": "31", "WARN": "33"}[status]
        print(f"  [{color(status, c)}] {name}")
        if detail and status != "PASS":
            print("        " + detail.replace("\n", "\n        ") if not detail.startswith("        ") else detail)
    nfail = sum(1 for s, _, _ in r.items if s == "FAIL")
    if r.passed:
        print(color(f"VERDICT: PASS ({len(r.items)} checks)", "1;32"))
        print(f"  Commit:  git add -A && git commit -m \"{commit}\"")
        print("  Answer out loud before you move on:")
        for q in INTERVIEW.get(tid, []):
            print(f"    - {q}")
    else:
        print(color(f"VERDICT: FAIL ({nfail} of {len(r.items)} checks failed). Fix these before ticking or committing.", "1;31"))


def log_result(tid, r):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    code, sha = run("git rev-parse --short HEAD")
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    n = len(r.items)
    nf = sum(1 for s, _, _ in r.items if s == "FAIL")
    with open(LOG, "a") as f:
        f.write(f"{stamp}  task {tid}  {'PASS' if r.passed else 'FAIL'}  {n - nf}/{n} checks  "
                f"HEAD {sha.strip() if code == 0 else '-'}\n")


def check(tid):
    r = Result()
    hygiene(r)
    try:
        TASKS[tid][1](r)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        r.fail("checker ran without crashing", f"{type(e).__name__}: {e}")
    print_result(tid, r)
    log_result(tid, r)
    return r.passed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("task", nargs="?", help="task id, e.g. 3.3")
    ap.add_argument("--day", type=int, help="check every task on this day, stopping at the first failure")
    ap.add_argument("--list", action="store_true", help="list tasks")
    a = ap.parse_args()
    if a.list:
        for k, (t, _, _) in TASKS.items():
            print(f"  {k}  {t}")
        return 0
    ids = [k for k in TASKS if a.day and k.startswith(f"{a.day}.")] if a.day else [a.task] if a.task else []
    if not ids or any(i not in TASKS for i in ids):
        ap.print_help()
        return 2
    for i in ids:
        if not check(i):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
