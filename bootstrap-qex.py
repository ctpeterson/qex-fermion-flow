"""
bootstrap-qex.py — clone (or reuse), configure, and set up a QEX build
directory, including QMP and QIO, in the directory where this script is run.

Usage
-----
    python3 bootstrap-qex.py [OPTIONS]

Run `python3 bootstrap-qex.py --help` for a full list of flags.

Everything is installed / created under:

    <cwd>/
        deps/
            src/
                travis-build/      ← bootstrap-travis working directory
                    qmp/           ← built QMP prefix
                    qio/           ← built QIO prefix
        qex-build/                 ← out-of-source QEX build directory
                                     (contains Makefile, qexconfig.nims, etc.)

QEX itself is never "installed"; you build executables inside qex-build/ with:

    cd qex-build && make <target>

or

    cd qex-build && nimble make <target>
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
# Helpers  (shared pattern with bootstrap-grid.py / bootstrap-hadrons.py)
# ──────────────────────────────────────────────────────────────────────

def run(cmd: str | list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    """Run a shell command, streaming output, aborting on failure."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    merged_env = {**os.environ, **(env or {})}
    print(f"\n>>> {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=cwd, env=merged_env)


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def which(name: str) -> str | None:
    return shutil.which(name)


def _lib_installed(prefix: Path, libname: str) -> bool:
    for libdir in ("lib", "lib64"):
        for ext in (".a", ".so"):
            if (prefix / libdir / f"{libname}{ext}").exists():
                return True
    return False


# ── Terminal title + stage progress ─────────────────────────────────

def _set_terminal_title(title: str) -> None:
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()


class StageTracker:
    def __init__(self, steps: list[tuple[str, str]]) -> None:
        self._steps = steps
        self._total = len(steps)
        self._index: dict[str, int] = {key: i for i, (key, _) in enumerate(steps)}

    def begin(self, key: str) -> None:
        idx = self._index[key]
        num = idx + 1
        label = self._steps[idx][1]
        banner = f"[{num}/{self._total}] {label}"
        _set_terminal_title(f"QEX bootstrap {banner}")
        print(f"\n\033[1;36m==>\033[0m \033[1m{banner}\033[0m")

    def finish(self) -> None:
        _set_terminal_title("QEX bootstrap — done")
        print(f"\n\033[1;32m==>\033[0m \033[1mBootstrap complete "
              f"[{self._total}/{self._total}]\033[0m")


NJOBS = str(multiprocessing.cpu_count())

# ──────────────────────────────────────────────────────────────────────
# Nim detection / installation
# ──────────────────────────────────────────────────────────────────────

def _find_nim() -> str | None:
    """Search for the Nim executable using QEX's own search order."""
    # 1. PATH
    if which("nim"):
        return which("nim")
    home = Path.home()
    # 2–4. $HOME/bin/nim variants
    for pat in ("nim", "nim-[0-9]*", "nim-*"):
        hits = sorted((home / "bin").glob(pat)) if (home / "bin").exists() else []
        if hits:
            return str(hits[-1])
    # 5–7. $HOME/nim/Nim variants
    for pat in ("Nim", "Nim-[0-9]*", "Nim-*"):
        for d in sorted((home / "nim").glob(pat)) if (home / "nim").exists() else []:
            candidate = d / "bin" / "nim"
            if candidate.exists():
                return str(candidate)
    return None


def ensure_nim(nim_dir: Path, deps_prefix: Path, *, version: str = "stable") -> str:
    """Return path to a nim executable, installing if necessary.

    Checks the standard search locations first; only downloads if needed.
    The pre-built binary goes into *nim_dir*/bin/.
    """
    found = _find_nim()
    if found:
        print(f"Found Nim: {found}")
        return found

    # Use the same logic as bootstrap-grid.py: download a pre-built binary.
    import platform
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture for Nim binary: {machine}")

    nim_version = "2.2.2"
    tarball = f"nim-{nim_version}-linux_{arch}.tar.xz"
    url = f"https://nim-lang.org/download/{tarball}"
    src = ensure_dir(deps_prefix / "src" / "nim")
    tarball_path = src / tarball

    if not tarball_path.exists():
        run(["curl", "-fL", "-o", str(tarball_path), url])

    run(f"tar xf {tarball}", cwd=src)
    nim_extracted = src / f"nim-{nim_version}"

    ensure_dir(nim_dir / "bin")
    for item in (nim_extracted / "bin").iterdir():
        dest = nim_dir / "bin" / item.name
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(item.resolve())

    nim_bin = str(nim_dir / "bin" / "nim")
    print(f"Nim {nim_version} installed: {nim_bin}")
    return nim_bin


# ──────────────────────────────────────────────────────────────────────
# QMP + QIO via QEX's own bootstrap-travis
# ──────────────────────────────────────────────────────────────────────

def run_bootstrap_travis(qex_src: Path, work_dir: Path, *, mpi: bool = True) -> tuple[Path, Path]:
    """Run ``qex/bootstrap-travis`` inside *work_dir*.

    Downloads QMP 2.5.4 and QIO 3.0.0 tarballs (skips if already present)
    and builds them into ``work_dir/qmp`` and ``work_dir/qio``.

    Returns ``(qmp_prefix, qio_prefix)``.
    """
    qmp_prefix = work_dir / "qmp"
    qio_prefix = work_dir / "qio"

    if _lib_installed(qmp_prefix, "libqmp") and _lib_installed(qio_prefix, "libqio"):
        print(f"QMP and QIO already installed under {work_dir}, skipping.")
        return qmp_prefix, qio_prefix

    script = qex_src / "bootstrap-travis"
    if not script.exists():
        raise RuntimeError(
            f"Could not find {script}.\n"
            f"Make sure --qex-src points to a valid QEX source tree."
        )

    ensure_dir(work_dir)
    arg = [] if mpi else ["single"]
    run(["sh", str(script)] + arg, cwd=work_dir)
    return qmp_prefix, qio_prefix


# ──────────────────────────────────────────────────────────────────────
# QEX configure
# ──────────────────────────────────────────────────────────────────────

def configure_qex(args: argparse.Namespace, nim_bin: str,
                  qmp_prefix: Path, qio_prefix: Path) -> None:
    root      = Path.cwd()
    qex_src   = Path(args.qex_src).resolve()
    build_dir = ensure_dir(root / "qex-build")

    if not (qex_src / "configure").exists():
        raise RuntimeError(
            f"Could not find {qex_src / 'configure'}.\n"
            f"Pass --qex-src pointing to a QEX source checkout, or omit it to "
            f"use the bundled ./qex/ directory."
        )

    print(f"\nConfiguring QEX")
    print(f"  source:    {qex_src}")
    print(f"  build dir: {build_dir}")
    print(f"  nim:       {nim_bin}")
    print(f"  qmpdir:    {qmp_prefix}")
    print(f"  qiodir:    {qio_prefix}")

    # ── Assemble configure argument list (key:value pairs) ──────────
    # These are passed to qex/configure, which feeds them to genconfig.nims.
    conf_args: list[str] = [
        f"qmpdir:{qmp_prefix}",
        f"qiodir:{qio_prefix}",
    ]

    # Compilers
    conf_args.append(f"cc:{args.mpicc}")
    conf_args.append(f"cpp:{args.mpicxx}")
    conf_args.append(f"ld:{args.mpicc}")
    conf_args.append(f"ldpp:{args.mpicxx}")

    # ccType — tell Nim which compiler family to generate flags for
    conf_args.append(f"ccType:{args.cc_type}")

    # Backend (cc or cpp)
    conf_args.append(f"ccDef:{args.cc_def}")

    # SIMD
    if args.simd is not None:
        conf_args.append(f"simd:{args.simd}")

    # VLEN
    if args.vlen is not None:
        conf_args.append(f"vlen:{args.vlen}")

    # Optional library dirs
    if args.grid_dir:
        conf_args.append(f"gridDir:{args.grid_dir}")
    if args.chroma_dir:
        conf_args.append(f"chromaDir:{args.chroma_dir}")
    if args.quda_dir:
        conf_args.append(f"qudaDir:{args.quda_dir}")
    if args.primme_dir:
        conf_args.append(f"primmeDir:{args.primme_dir}")

    # Extra cflags / cppflags via cflagsAlways / cppflagsAlways
    base_cflags = "-g"
    base_speed  = "-Ofast -march=native"
    if args.extra_cflags:
        base_cflags += f" {args.extra_cflags}"
    if args.extra_cxxflags:
        base_speed_cpp = base_speed + f" {args.extra_cxxflags}"
    else:
        base_speed_cpp = base_speed
    conf_args.append(f"cflagsAlways:{base_cflags}")
    conf_args.append(f"cppflagsAlways:{base_cflags}")
    conf_args.append(f"cflagsSpeed:{base_speed}")
    conf_args.append(f"cppflagsSpeed:{base_speed_cpp}")
    conf_args.append(f"ldflags:{base_cflags} -ldl")
    conf_args.append(f"ldppflags:{base_cflags} -ldl")

    # Extra nimargs
    if args.extra_nimargs:
        # wrap the whole list as a Nim seq literal
        items = " ".join(f'"{a}"' for a in shlex.split(args.extra_nimargs))
        conf_args.append(f"nimargs:@[{items}]")

    # ── Run configure from inside the build directory ────────────────
    env: dict[str, str] = {"NIM": nim_bin}
    run(
        [str(qex_src / "configure")] + conf_args,
        cwd=build_dir,
        env=env,
    )

    print(f"\n{'='*60}")
    print(f"QEX build directory configured: {build_dir}")
    print(f"")
    print(f"To build a target, e.g. fermionFlowBilinears:")
    print(f"  cd {build_dir}")
    print(f"  make fermionFlowBilinears")
    print(f"")
    print(f"Binaries will appear in {build_dir}/bin/")
    print(f"{'='*60}\n")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bootstrap a QEX build directory with QMP and QIO.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Minimal build — uses ./qex, runs bootstrap-travis for QMP+QIO
  python3 bootstrap-qex.py

  # Use pre-installed QMP/QIO (e.g. the bundled qex/qmp and qex/qio)
  python3 bootstrap-qex.py \
    --with-qmp ./qex/qmp \
    --with-qio ./qex/qio

  # Single-node (no MPI) build
  python3 bootstrap-qex.py --no-mpi

  # C++ backend for Grid-linked builds
  python3 bootstrap-qex.py \
    --cc-def cpp \
    --cc-type clang \
    --simd "SSE,AVX" \
    --grid-dir ./grid-install

  # Point at a non-default QEX source tree
  python3 bootstrap-qex.py --qex-src /path/to/qex

  # Pass extra Nim defines
  python3 bootstrap-qex.py --extra-nimargs="-d:nc=3 -d:defPrec=S"
""",
    )

    # ── QEX source ──────────────────────────────────────────────────
    qex_grp = p.add_argument_group("QEX source location")
    qex_grp.add_argument(
        "--qex-src", metavar="PATH",
        default=str(Path.cwd() / "qex"),
        help="Path to the QEX source tree (default: ./qex)",
    )
    qex_grp.add_argument(
        "--qex-pull", action="store_true",
        help="Run git pull in the QEX source tree before configuring",
    )

    # ── QMP / QIO ─────────────────────────────────────────────────
    qmp_grp = p.add_argument_group("QMP / QIO")
    qmp_grp.add_argument("--with-qmp", metavar="PREFIX",
                          help="Use a pre-installed QMP instead of running bootstrap-travis")
    qmp_grp.add_argument("--with-qio", metavar="PREFIX",
                          help="Use a pre-installed QIO instead of running bootstrap-travis")
    qmp_grp.add_argument("--no-mpi", action="store_true",
                          help="Build QMP/QIO in single-node (non-MPI) mode "
                               "(passes 'single' to bootstrap-travis)")

    # ── Optional library dirs ────────────────────────────────────────
    opt = p.add_argument_group("optional libraries (set gridDir / chromaDir / etc. in qexconfig.nims)")
    opt.add_argument("--grid-dir", metavar="PREFIX",
                     help="Grid install prefix to set gridDir in qexconfig.nims")
    opt.add_argument("--chroma-dir", metavar="PREFIX",
                     help="Chroma install prefix to set chromaDir")
    opt.add_argument("--quda-dir", metavar="PREFIX",
                     help="QUDA install prefix to set qudaDir")
    opt.add_argument("--primme-dir", metavar="PREFIX",
                     help="PRIMME install prefix to set primmeDir")

    # ── Nim ─────────────────────────────────────────────────────────
    nim_grp = p.add_argument_group("Nim")
    nim_grp.add_argument("--nim", metavar="PATH",
                          help="Path to nim executable (default: auto-detect / install)")
    nim_grp.add_argument("--skip-nim", action="store_true",
                          help="Do not auto-install Nim; fail if nim is not on PATH")

    # ── Compilers ───────────────────────────────────────────────────
    comp = p.add_argument_group("compilers")
    comp.add_argument("--mpicc", default="mpicc", metavar="COMPILER",
                      help="MPI C compiler wrapper (default: mpicc)")
    comp.add_argument("--mpicxx", default="mpicxx", metavar="COMPILER",
                      help="MPI C++ compiler wrapper (default: mpicxx)")
    comp.add_argument("--cc-type", default="gcc",
                      choices=["gcc", "clang"],
                      help="Compiler family for Nim-generated flags (default: gcc). "
                           "Use 'clang' if your MPI wrappers call clang/clang++.")
    comp.add_argument("--extra-cflags", metavar="FLAGS",
                      help="Extra flags appended to cflagsAlways")
    comp.add_argument("--extra-cxxflags", metavar="FLAGS",
                      help="Extra flags appended to cppflagsSpeed")

    # ── SIMD / backend ──────────────────────────────────────────────
    simd_grp = p.add_argument_group("SIMD / backend")
    simd_grp.add_argument("--simd", metavar="SSE,AVX[,AVX512]",
                           default=None,
                           help="SIMD intrinsics to enable, comma-separated "
                                "(e.g. 'SSE,AVX' or 'SSE,AVX,AVX512'). "
                                "Empty string disables intrinsics. Default: empty.")
    simd_grp.add_argument("--vlen", type=int, default=None, metavar="N",
                           help="Inner SIMD vector length (default: 8)")
    simd_grp.add_argument("--cc-def", default="cc", choices=["cc", "cpp"],
                           help="Default language backend: 'cc' (C) or 'cpp' (C++). "
                                "Use 'cpp' when linking against Grid or Chroma. "
                                "(default: cc)")

    # ── Extra Nim args ───────────────────────────────────────────────
    p.add_argument("--extra-nimargs", metavar="'FLAGS'",
                   help="Extra arguments added to nimargs in qexconfig.nims "
                        "(e.g. \"-d:nc=3 -d:defPrec=S\")")

    # ── Build control ───────────────────────────────────────────────
    build = p.add_argument_group("build control")
    build.add_argument("--jobs", "-j", default=NJOBS, metavar="N",
                       help=f"Parallel make jobs for QMP/QIO (default: {NJOBS})")

    return p.parse_args()


# ──────────────────────────────────────────────────────────────────────
# Stage list
# ──────────────────────────────────────────────────────────────────────

def _build_stage_list(args: argparse.Namespace) -> list[tuple[str, str]]:
    stages: list[tuple[str, str]] = []
    if not args.skip_nim and not args.nim:
        stages.append(("nim", "Resolving Nim compiler"))
    if not (args.with_qmp and args.with_qio):
        stages.append(("deps", "Building QMP + QIO (bootstrap-travis)"))
    stages.append(("qex", "Configuring QEX build directory"))
    return stages


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    root = Path.cwd()
    deps_prefix = root / "deps"
    ensure_dir(deps_prefix)

    stages = StageTracker(_build_stage_list(args))

    # ── Nim ────────────────────────────────────────────────────────
    if args.nim:
        nim_bin = args.nim
        print(f"Using user-supplied Nim: {nim_bin}")
    elif args.skip_nim:
        nim_bin = _find_nim()
        if not nim_bin:
            raise RuntimeError(
                "--skip-nim was set but no Nim executable could be found on PATH "
                "or in standard locations. Either install Nim or drop --skip-nim."
            )
        print(f"Found Nim: {nim_bin}")
    else:
        stages.begin("nim")
        nim_bin = ensure_nim(
            root / "nim-install", deps_prefix,
        )

    # ── QEX source: optionally pull ────────────────────────────────
    qex_src = Path(args.qex_src).resolve()
    if args.qex_pull and (qex_src / ".git").exists():
        run("git pull", cwd=qex_src)

    # ── QMP / QIO ──────────────────────────────────────────────────
    if args.with_qmp and args.with_qio:
        qmp_prefix = Path(args.with_qmp).resolve()
        qio_prefix = Path(args.with_qio).resolve()
        print(f"Using pre-installed QMP: {qmp_prefix}")
        print(f"Using pre-installed QIO: {qio_prefix}")
    else:
        if args.with_qmp or args.with_qio:
            raise RuntimeError(
                "Supply both --with-qmp and --with-qio, or neither "
                "(bootstrap-travis builds both together)."
            )
        stages.begin("deps")
        travis_work = ensure_dir(deps_prefix / "src" / "travis-build")
        qmp_prefix, qio_prefix = run_bootstrap_travis(
            qex_src, travis_work, mpi=not args.no_mpi,
        )

    # ── Configure QEX ─────────────────────────────────────────────
    stages.begin("qex")
    configure_qex(args, nim_bin, qmp_prefix, qio_prefix)

    stages.finish()


if __name__ == "__main__":
    main()
