"""
bootstrap.py — download, build, and install Grid-HISQ and its dependencies
into the directory where this script is run.

Usage
-----
    python3 bootstrap.py [OPTIONS]

Run `python3 bootstrap.py --help` for a full list of flags.

Everything is installed under:

    <cwd>/
        deps/
            lib/, include/, bin/   ← installed libraries
            src/                   ← all source / build trees
                gmp/
                mpfr/
                fftw/
                openssl/
                hdf5/
                lime/
                libunwind/
                grid/              ← cloned Grid-HISQ source
                grid-build/        ← out-of-source Grid build
        grid-install/              ← final Grid install prefix
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
# Helpers
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


# ── Terminal title + stage progress ─────────────────────────────────

def _set_terminal_title(title: str) -> None:
    """Set the terminal window/tab title via an OSC escape sequence."""
    # Works in xterm, iTerm2, GNOME Terminal, Windows Terminal, etc.
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()


class StageTracker:
    """Track and display build progress like Spack does.

    Usage::

        stages = StageTracker([
            ("compiler", "Resolving compiler"),
            ("gmp",      "Installing GMP"),
            ...
        ])
        stages.begin("compiler")
        # … do work …
        stages.begin("gmp")
        # … etc …
        stages.finish()
    """

    def __init__(self, steps: list[tuple[str, str]]) -> None:
        # steps: [(key, human-readable label), …]
        self._steps = steps
        self._total = len(steps)
        self._index: dict[str, int] = {key: i for i, (key, _) in enumerate(steps)}

    def begin(self, key: str) -> None:
        """Mark stage *key* as active."""
        idx = self._index[key]
        num = idx + 1
        label = self._steps[idx][1]
        banner = f"[{num}/{self._total}] {label}"
        _set_terminal_title(f"Grid-HISQ bootstrap {banner}")
        print(f"\n\033[1;36m==>\033[0m \033[1m{banner}\033[0m")

    def finish(self) -> None:
        """Mark the entire process as complete."""
        _set_terminal_title("Grid-HISQ bootstrap — done")
        print(f"\n\033[1;32m==>\033[0m \033[1mBootstrap complete "
              f"[{self._total}/{self._total}]\033[0m")


def download(url: str, dest: Path) -> None:
    """Download a URL to dest, verifying we didn't get an HTML error page."""
    run(["curl", "-fL", "-o", str(dest), url])
    # Sanity-check: if the server gave us HTML instead of a tarball, abort early
    # rather than letting tar fail with a confusing message.
    with open(dest, "rb") as f:
        header = f.read(256)
    if b"<html" in header.lower() or b"<!doctype" in header.lower():
        dest.unlink()
        raise RuntimeError(
            f"Download of {url} returned an HTML page, not an archive.\n"
            f"The URL may have changed. Please check it manually."
        )


NJOBS = str(multiprocessing.cpu_count())

# ──────────────────────────────────────────────────────────────────────
# Spack helpers  (always uses a private, local clone — never the user's)
# ──────────────────────────────────────────────────────────────────────

def _local_spack_bin(deps_prefix: Path) -> Path:
    """Return the path to the local Spack binary under *deps_prefix*."""
    return deps_prefix / "src" / "spack" / "bin" / "spack"


def ensure_spack(deps_prefix: Path) -> None:
    """Clone a private Spack into ``deps_prefix/src/spack`` if not already present.

    We never use a system or user Spack installation — every Spack
    command executed by this script goes through the local clone so
    the build is fully self-contained and reproducible.
    """
    spack_bin = _local_spack_bin(deps_prefix)
    if spack_bin.exists():
        print(f"Local Spack already present at {spack_bin}")
        return

    print("\nInstalling a local Spack clone …")
    ensure_dir(deps_prefix / "src")
    run(
        "git clone --depth=1 https://github.com/spack/spack.git",
        cwd=deps_prefix / "src",
    )
    if not spack_bin.exists():
        raise RuntimeError(
            f"Spack clone succeeded but {spack_bin} does not exist."
        )
    print(f"Local Spack installed: {spack_bin}")


def _spack_cmd(deps_prefix: Path) -> list[str]:
    """Return the base command list for invoking the local Spack."""
    return [str(_local_spack_bin(deps_prefix))]


def spack_install(spec: str, deps_prefix: Path, *, jobs: str = NJOBS) -> Path:
    """Install a Spack spec using the local Spack and return its prefix."""
    cmd = _spack_cmd(deps_prefix) + ["install", f"-j{jobs}", spec]
    print(f"\n>>> {' '.join(cmd)}")
    subprocess.check_call(cmd, env=os.environ)
    return spack_prefix(spec, deps_prefix)


def spack_prefix(spec: str, deps_prefix: Path) -> Path:
    """Return the install prefix of an already-installed Spack spec."""
    cmd = _spack_cmd(deps_prefix) + ["location", "-i", spec]
    out = subprocess.check_output(cmd, env=os.environ, text=True).strip()
    return Path(out)


def spack_link_into(spec: str, deps_prefix: Path, *, jobs: str = NJOBS) -> Path:
    """Install *spec* with the local Spack and symlink artifacts into *deps_prefix*.

    This keeps a single unified prefix for Grid's configure step while
    letting Spack manage the actual build.

    Libraries from both ``lib/`` and ``lib64/`` in the Spack prefix are
    symlinked into ``deps_prefix/lib/`` so that Grid's ``--with-*=PREFIX``
    flags (which always look in ``PREFIX/lib/``) work correctly.
    """
    pfx = spack_install(spec, deps_prefix, jobs=jobs)

    # Map Spack sub-directories to their destination under deps_prefix.
    # Crucially, lib64/ is merged into lib/ so Grid can find everything.
    DIR_MAP: dict[str, str] = {
        "lib":     "lib",
        "lib64":   "lib",       # ← redirect into lib/
        "include": "include",
        "bin":     "bin",
        "share":   "share",
    }

    for src_sub, dst_sub in DIR_MAP.items():
        src_dir = pfx / src_sub
        if not src_dir.exists():
            continue
        dst_dir = ensure_dir(deps_prefix / dst_sub)
        for item in src_dir.iterdir():
            dst = dst_dir / item.name
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(item)
    return pfx


# Mapping from our internal library names to Spack spec strings.
SPACK_SPECS: dict[str, str] = {
    "gmp":        "gmp",
    "mpfr":       "mpfr",
    "fftw":       "fftw ~mpi precision=float,double",
    "openssl":    "openssl",
    "hdf5":       "hdf5 +cxx ~mpi",
    "lime":       "c-lime",
    "libunwind":  "libunwind",
}


def build_or_spack(
    lib_name: str,
    build_fn,
    build_args: tuple,
    deps_prefix: Path,
    *,
    use_spack: bool = False,
    jobs: str = NJOBS,
) -> None:
    """Try *build_fn*; on failure (or if *use_spack*) fall back to Spack."""
    spec = SPACK_SPECS.get(lib_name, lib_name)

    if use_spack:
        print(f"\n--use-spack: installing {lib_name} via Spack ({spec})")
        spack_link_into(spec, deps_prefix, jobs=jobs)
        return

    try:
        build_fn(*build_args)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"\n{'!'*60}")
        print(f"WARNING: source build of {lib_name} failed: {exc}")
        print(f"Falling back to Spack ({spec}) …")
        print(f"{'!'*60}")
        ensure_spack(deps_prefix)
        spack_link_into(spec, deps_prefix, jobs=jobs)


# ──────────────────────────────────────────────────────────────────────
# Compiler resolution  (default: LLVM/Clang)
# ──────────────────────────────────────────────────────────────────────

LLVM_VERSION = "18.1.8"


def _find_system_compiler() -> tuple[str, str]:
    """Return the best available system C/C++ compiler as (cc, cxx)."""
    if which("gcc") and which("g++"):
        return "gcc", "g++"
    if which("cc") and which("c++"):
        # 'cc' is almost always gcc on Linux
        return "gcc", "g++"
    return "gcc", "g++"  # hopeful default


def _install_prebuilt_llvm(deps_prefix: Path) -> Path:
    """Download a prebuilt LLVM/Clang release.  Returns the prefix containing bin/clang."""
    import platform
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "X64"
    elif machine in ("aarch64", "arm64"):
        arch = "ARM64"
    else:
        raise RuntimeError(f"No prebuilt LLVM binary available for {machine}")

    version = LLVM_VERSION
    src = ensure_dir(deps_prefix / "src" / "llvm")

    # Already extracted from a previous run?
    for d in src.iterdir():
        if d.is_dir() and (d / "bin" / "clang").exists():
            return d

    tarball_name = f"LLVM-{version}-Linux-{arch}.tar.xz"
    url = (f"https://github.com/llvm/llvm-project/releases/"
           f"download/llvmorg-{version}/{tarball_name}")
    tarball = src / tarball_name

    if not tarball.exists():
        download(url, tarball)
    run(f"tar xf {tarball_name}", cwd=src)

    # Scan for the extracted directory (name may vary across releases)
    for d in src.iterdir():
        if d.is_dir() and (d / "bin" / "clang").exists():
            return d

    raise RuntimeError(
        "Extracted LLVM tarball but could not find bin/clang in any subdirectory."
    )


def _prepend_to_path(bindir: Path) -> None:
    """Prepend *bindir* to PATH for this process and all children."""
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


def _set_compiler_env(
    cc: str, cxx: str, deps_prefix: Path, *, extra_path: str | None = None,
) -> None:
    """Persist CC/CXX in ``os.environ`` and write ``deps/compiler.env``
    so that Grid-HISQ's ``configure`` can pick up the same compiler."""
    os.environ["CC"] = cc
    os.environ["CXX"] = cxx

    env_file = deps_prefix / "compiler.env"
    with open(env_file, "w") as f:
        f.write("# Auto-generated by bootstrap — do not edit\n")
        if extra_path:
            f.write(f'export PATH="{extra_path}:${{PATH}}"\n')
        f.write(f'export CC="{cc}"\n')
        f.write(f'export CXX="{cxx}"\n')
    print(f"Wrote {env_file}")


def ensure_compiler(
    deps_prefix: Path, *, no_llvm: bool = False, jobs: str = NJOBS,
) -> tuple[str, str]:
    """Resolve C/C++ compilers.  Returns ``(cc, cxx)``.

    Fallback chain (unless *no_llvm*):

    1. User's ``clang`` / ``clang++`` already on ``PATH``
    2. Download a prebuilt LLVM release into ``deps/src/llvm/``
    3. Install LLVM via the local Spack clone
    4. System default compilers (``gcc`` / ``g++``)
    """
    if no_llvm:
        cc, cxx = _find_system_compiler()
        print(f"--no-llvm: using system compilers: {cc} / {cxx}")
        _set_compiler_env(cc, cxx, deps_prefix)
        return cc, cxx

    # 1. User's clang on PATH
    if which("clang") and which("clang++"):
        print(f"Found clang on PATH: {which('clang')}")
        _set_compiler_env("clang", "clang++", deps_prefix)
        return "clang", "clang++"

    # 2. Prebuilt LLVM download
    try:
        print("\nclang not found on PATH — trying prebuilt LLVM download …")
        llvm_prefix = _install_prebuilt_llvm(deps_prefix)
        _prepend_to_path(llvm_prefix / "bin")
        print(f"Prebuilt LLVM ready: {llvm_prefix}")
        _set_compiler_env(
            "clang", "clang++", deps_prefix,
            extra_path=str(llvm_prefix / "bin"),
        )
        return "clang", "clang++"
    except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
        print(f"Prebuilt LLVM install failed: {exc}")

    # 3. Spack
    try:
        print("\nTrying to install LLVM via Spack …")
        ensure_spack(deps_prefix)
        llvm_prefix = spack_install("llvm", deps_prefix, jobs=jobs)
        if (llvm_prefix / "bin" / "clang").exists():
            _prepend_to_path(llvm_prefix / "bin")
            print(f"Spack LLVM ready: {llvm_prefix}")
            _set_compiler_env(
                "clang", "clang++", deps_prefix,
                extra_path=str(llvm_prefix / "bin"),
            )
            return "clang", "clang++"
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Spack LLVM install failed: {exc}")

    # 4. Fallback to system defaults
    cc, cxx = _find_system_compiler()
    print(f"\n{'!'*60}")
    print(f"WARNING: Could not install LLVM/Clang.")
    print(f"Falling back to system compilers: {cc} / {cxx}")
    print(f"{'!'*60}")
    _set_compiler_env(cc, cxx, deps_prefix)
    return cc, cxx


# ────────────────────────────────────────────────────────────────���─────
# Dependency builders
# ──────────────────────────────────────────────────────────────────────

def _lib_installed(prefix: Path, libname: str) -> bool:
    """Return True if *libname* (e.g. ``libgmp``) exists under *prefix*.

    Checks for static (``.a``) and shared (``.so``) libraries in both
    ``lib/`` and ``lib64/`` so that source-built *and* Spack-installed
    libraries are detected equally.
    """
    for libdir in ("lib", "lib64"):
        for ext in (".a", ".so"):
            if (prefix / libdir / f"{libname}{ext}").exists():
                return True
    return False


def build_gmp(prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "gmp")
    if _lib_installed(prefix, "libgmp"):
        print("GMP already installed, skipping.")
        return
    tarball = "gmp-6.3.0.tar.xz"
    url = f"https://gmplib.org/download/gmp/{tarball}"
    if not (src / tarball).exists():
        download(url, src / tarball)
    run(f"tar xf {tarball}", cwd=src)
    build_dir = src / "gmp-6.3.0"
    run(f"./configure --prefix={prefix} --enable-cxx", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install", cwd=build_dir)


def build_mpfr(prefix: Path, gmp_prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "mpfr")
    if _lib_installed(prefix, "libmpfr"):
        print("MPFR already installed, skipping.")
        return
    tarball = "mpfr-4.2.1.tar.xz"
    url = f"https://ftp.gnu.org/gnu/mpfr/{tarball}"
    if not (src / tarball).exists():
        download(url, src / tarball)
    run(f"tar xf {tarball}", cwd=src)
    build_dir = src / "mpfr-4.2.1"
    run(f"./configure --prefix={prefix} --with-gmp={gmp_prefix}", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install", cwd=build_dir)


def build_fftw(prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "fftw")
    if _lib_installed(prefix, "libfftw3"):
        print("FFTW already installed, skipping.")
        return
    tarball = "fftw-3.3.10.tar.gz"
    url = f"https://www.fftw.org/{tarball}"
    if not (src / tarball).exists():
        download(url, src / tarball)
    run(f"tar xf {tarball}", cwd=src)
    build_dir = src / "fftw-3.3.10"
    # Double precision
    run(f"./configure --prefix={prefix}", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install", cwd=build_dir)
    run("make clean", cwd=build_dir)
    # Single precision (Grid needs both)
    run(f"./configure --prefix={prefix} --enable-float", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install", cwd=build_dir)


def build_openssl(prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "openssl")
    if _lib_installed(prefix, "libssl"):
        print("OpenSSL already installed, skipping.")
        return
    tarball = "openssl-3.2.1.tar.gz"
    url = f"https://github.com/openssl/openssl/releases/download/openssl-3.2.1/{tarball}"
    if not (src / tarball).exists():
        download(url, src / tarball)
    run(f"tar xf {tarball}", cwd=src)
    build_dir = src / "openssl-3.2.1"
    run(f"./config --prefix={prefix} --openssldir={prefix / 'ssl'}", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install_sw", cwd=build_dir)


def build_hdf5(prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "hdf5")
    if _lib_installed(prefix, "libhdf5"):
        print("HDF5 already installed, skipping.")
        return
    tag = "hdf5_1.14.6"
    tarball = f"{tag}.tar.gz"
    url = f"https://github.com/HDFGroup/hdf5/archive/refs/tags/{tarball}"
    if not (src / tarball).exists():
        download(url, src / tarball)
    run(f"tar xf {tarball}", cwd=src)
    build_dir = src / f"hdf5-{tag}"
    run(f"./configure --prefix={prefix} --enable-cxx --enable-build-mode=production", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install", cwd=build_dir)


def build_lime(prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "lime")
    if _lib_installed(prefix, "liblime"):
        print("LIME (c-lime) already installed, skipping.")
        return
    clone_dir = src / "c-lime"
    if not clone_dir.exists():
        run(f"git clone https://github.com/usqcd-software/c-lime.git", cwd=src)
    run("./autogen.sh", cwd=clone_dir)
    run(f"./configure --prefix={prefix}", cwd=clone_dir)
    run(f"make -j{jobs}", cwd=clone_dir)
    run("make install", cwd=clone_dir)


def build_libunwind(prefix: Path, jobs: str) -> None:
    src = ensure_dir(prefix / "src" / "libunwind")
    if _lib_installed(prefix, "libunwind"):
        print("libunwind already installed, skipping.")
        return
    tarball = "libunwind-1.8.1.tar.gz"
    url = f"https://github.com/libunwind/libunwind/releases/download/v1.8.1/{tarball}"
    if not (src / tarball).exists():
        download(url, src / tarball)
    run(f"tar xf {tarball}", cwd=src)
    build_dir = src / "libunwind-1.8.1"
    run(f"./configure --prefix={prefix}", cwd=build_dir)
    run(f"make -j{jobs}", cwd=build_dir)
    run("make install", cwd=build_dir)


# ──────────────────────────────────────────────────────────────────────
# Grid builder
# ──────────────────────────────────────────────────────────────────────

def build_grid(args: argparse.Namespace, deps_prefix: Path) -> None:
    root = Path.cwd()
    grid_src = deps_prefix / "src" / "grid"
    grid_build = deps_prefix / "src" / "grid-build"
    grid_install = root / "grid-install"

    # Clone if needed — uses ctpeterson/Grid-HISQ instead of paboyle/Grid
    if not grid_src.exists():
        run(f"git clone --branch {args.grid_branch} https://github.com/ctpeterson/Grid-HISQ.git {grid_src}")
    elif args.grid_pull: run("git pull", cwd=grid_src)

    # Patch
    config_path = grid_src / 'configure.ac'
    tmp_path = grid_src / 'tmp.ac'
    with open(config_path, 'r') as config_file, open(tmp_path, 'w+') as temp_file:
        for line in config_file.readlines():
            if 'ac_SETDEVICE=${enable_SETDEVICE}]' in line:
                ln = 'AC_ARG_ENABLE([setdevice],[AS_HELP_STRING([--enable-setdevice | '
                ln += '--disable-setdevice],[Set GPU to rank in node with cudaSetDevice '
                ln += 'or similar])],[ac_setdevice=${enable_setdevice}],[ac_setdevice=no])'
                temp_file.write(ln)
            else: temp_file.write(line)
    run(f'mv {tmp_path} {config_path}')
    print('Applied patches to configure.ac')
    
    # Bootstrap
    run("./bootstrap.sh", cwd=grid_src)

    # Build out-of-source
    ensure_dir(grid_build)

    # ── Assemble configure command ──────────────────────────────────
    configure_cmd: list[str] = [str(grid_src / "configure")]

    configure_cmd.append(f"--prefix={grid_install}")

    # ── SIMD ────────────────────────────────────────────────────────
    configure_cmd.append(f"--enable-simd={args.simd}")
    if args.gen_simd_width is not None:
        configure_cmd.append(f"--enable-gen-simd-width={args.gen_simd_width}")
    if args.gen_scalar:
        configure_cmd.append("--enable-gen-scalar=yes")

    # ── Communications ──────────────────────────────────────────────
    configure_cmd.append(f"--enable-comms={args.comms}")

    # ── Accelerator (GPU) ───────────────────────────────────────────
    configure_cmd.append(f"--enable-accelerator={args.accelerator}")
    if args.unified is not None:
        configure_cmd.append(f"--enable-unified={'yes' if args.unified else 'no'}")
    if args.accelerator_aware_mpi is not None:
        val = "yes" if args.accelerator_aware_mpi else "no"
        configure_cmd.append(f"--enable-accelerator-aware-mpi={val}")
    if args.setdevice:
        configure_cmd.append("--enable-setdevice")

    # ── Shared memory ──────────────────────────────────────────────
    if args.shm is not None:
        configure_cmd.append(f"--enable-shm={args.shm}")
    if args.shmpath is not None:
        configure_cmd.append(f"--enable-shmpath={args.shmpath}")
    if args.shm_force_mpi:
        configure_cmd.append("--enable-shm-force-mpi")
    if args.shm_fast_path:
        configure_cmd.append("--enable-shm-fast-path")

    # ── Allocator ───────────────────────────────────────────────────
    if args.alloc_align is not None:
        configure_cmd.append(f"--enable-alloc-align={args.alloc_align}")
    if args.alloc_cache is not None:
        val = "yes" if args.alloc_cache else "no"
        configure_cmd.append(f"--enable-alloc-cache={val}")

    # ── Gauge group / fermions ──────────────────────────────────────
    configure_cmd.append(f"--enable-Nc={args.nc}")
    if args.sp:
        configure_cmd.append("--enable-Sp=yes")
    if args.fermion_reps is not None:
        val = "yes" if args.fermion_reps else "no"
        configure_cmd.append(f"--enable-fermion-reps={val}")
    if args.gparity is not None:
        val = "yes" if args.gparity else "no"
        configure_cmd.append(f"--enable-gparity={val}")
    if args.zmobius is not None:
        val = "yes" if args.zmobius else "no"
        configure_cmd.append(f"--enable-zmobius={val}")

    # ── RNG ─────────────────────────────────────────────────────────
    configure_cmd.append(f"--enable-rng={args.rng}")

    # ── Debug / optimisation ────────────────────────────────────────
    if args.debug:
        configure_cmd.append("--enable-debug=yes")

    # ── Timers ──────────────────────────────────────────────────────
    if args.no_timers:
        configure_cmd.append("--enable-timers=no")

    # ── FP16 ────────────────────────────────────────────────────────
    if args.sfw_fp16 is not None:
        val = "yes" if args.sfw_fp16 else "no"
        configure_cmd.append(f"--enable-sfw-fp16={val}")

    # ── Tracing ─────────────────────────────────────────────────────
    if args.tracing is not None:
        configure_cmd.append(f"--enable-tracing={args.tracing}")

    # ── Reduction ───────────────────────────────────────────────────
    if args.reduction is not None:
        configure_cmd.append(f"--enable-reduction={args.reduction}")

    # ── Checksums / logging ─────────────────────────────────────────
    if args.checksum_comms:
        configure_cmd.append("--enable-checksum-comms=yes")
    if args.log_views:
        configure_cmd.append("--enable-log-views=yes")

    # ── Chroma regression ───────────────────────────────────────────
    if args.chroma:
        configure_cmd.append("--enable-chroma")

    # ── Dependency paths ────────────────────────────────────────────
    # Always point at the deps prefix we just built into
    configure_cmd.append(f"--with-gmp={deps_prefix}")
    configure_cmd.append(f"--with-mpfr={deps_prefix}")

    if args.with_fftw or _lib_installed(deps_prefix, "libfftw3"):
        configure_cmd.append(f"--with-fftw={args.with_fftw or deps_prefix}")
    if args.with_hdf5 or _lib_installed(deps_prefix, "libhdf5"):
        configure_cmd.append(f"--with-hdf5={args.with_hdf5 or deps_prefix}")
    if args.with_lime or _lib_installed(deps_prefix, "liblime"):
        configure_cmd.append(f"--with-lime={args.with_lime or deps_prefix}")
    if args.with_openssl or _lib_installed(deps_prefix, "libssl"):
        configure_cmd.append(f"--with-openssl={args.with_openssl or deps_prefix}")
    if args.with_unwind or _lib_installed(deps_prefix, "libunwind"):
        configure_cmd.append(f"--with-unwind={args.with_unwind or deps_prefix}")

    # ── LAPACK / MKL / IPP ──────────────────────────────────────────
    if args.lapack is not None:
        configure_cmd.append(f"--enable-lapack={args.lapack}")
    if args.mkl is not None:
        configure_cmd.append(f"--enable-mkl={args.mkl}")
    if args.ipp is not None:
        configure_cmd.append(f"--enable-ipp={args.ipp}")

    # ── Extra --with-* overrides (user-supplied prefix paths) ───────
    if args.with_gmp:
        # Override the deps-prefix default
        configure_cmd = [c for c in configure_cmd if not c.startswith("--with-gmp=")]
        configure_cmd.append(f"--with-gmp={args.with_gmp}")
    if args.with_mpfr:
        configure_cmd = [c for c in configure_cmd if not c.startswith("--with-mpfr=")]
        configure_cmd.append(f"--with-mpfr={args.with_mpfr}")

    # ── Compiler overrides ──────────────────────────────────────────
    env: dict[str, str] = {}
    if args.cxx: configure_cmd.append(f"CXX={args.cxx}")
    if args.cc: configure_cmd.append(f"CC={args.cc}")
    if args.mpicxx: configure_cmd.append(f"MPICXX={args.mpicxx}")
    if args.cxxflags: configure_cmd.append(f'CXXFLAGS={args.cxxflags}')
    if args.ldflags: configure_cmd.append(f'LDFLAGS={args.ldflags}')

    # ── Extra raw flags passed through verbatim ─────────────────────
    if args.extra_configure_flags:
        configure_cmd.extend(shlex.split(args.extra_configure_flags))

    # ── Run configure ───────────────────────────────────────────────
    run(configure_cmd, cwd=grid_build, env=env if env else None)

    # ── Build & install ─────────────────────────────────────────────
    run(f"make -j{args.jobs}", cwd=grid_build)
    if args.run_tests:
        run(f"make -j{args.jobs} tests", cwd=grid_build)
        run("make check", cwd=grid_build)
    run("make install", cwd=grid_build)

    print(f"\n{'='*60}")
    print(f"Grid-HISQ installed to: {grid_install}")
    print(f"Set GRID_PREFIX={grid_install} for config.nims")
    print(f"{'='*60}\n")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bootstrap Grid-HISQ and all dependencies into the current directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Minimal CPU-only build (GEN SIMD, no MPI)
  python3 bootstrap.py

  # AVX2 + MPI auto-detection
  python3 bootstrap.py --simd AVX2 --comms mpi-auto

  # CUDA GPU build
  python3 bootstrap.py --simd GPU --comms mpi-auto --accelerator cuda --cxx nvcc

  # AMD GPU (HIP) build
  python3 bootstrap.py --simd GPU --comms mpi-auto --accelerator hip --cxx hipcc

  # Intel GPU (SYCL) build
  python3 bootstrap.py --simd GPU --comms mpi-auto --accelerator sycl --cxx dpcpp

  # Knights Landing
  python3 bootstrap.py --simd KNL --comms mpi3-auto --mkl yes --cxx icpc --mpicxx mpiicpc

  # A64FX (Fugaku / ARM SVE)
  python3 bootstrap.py --simd A64FX --comms mpi-auto

  # Skip dependencies (already installed system-wide)
  python3 bootstrap.py --skip-deps --with-gmp /usr --with-mpfr /usr

  # Install all dependencies via Spack instead of building from source
  python3 bootstrap.py --use-spack

  # Use the system default compiler (GCC) instead of LLVM/Clang
  python3 bootstrap.py --no-llvm

  # Pass extra flags directly to Grid's configure
  python3 bootstrap.py --extra-configure-flags="--disable-fermion-reps --enable-Nc=4"
""",
    )

    # ── Dependency control ──────────────────────────────────────────
    dep = p.add_argument_group("dependency control")
    dep.add_argument("--skip-deps", action="store_true",
                     help="Do not build any dependencies; assume they are installed elsewhere.")
    dep.add_argument("--use-spack", action="store_true",
                     help="Install all dependencies via Spack instead of building from source. "
                          "Requires `spack` on PATH. Without this flag, Spack is only used "
                          "as a fallback when a source build fails.")
    dep.add_argument("--no-llvm", action="store_true",
                     help="Do not attempt to install or use LLVM/Clang; use the default "
                          "system compiler (typically GCC) instead.")
    dep.add_argument("--skip-gmp", action="store_true", help="Skip building GMP")
    dep.add_argument("--skip-mpfr", action="store_true", help="Skip building MPFR")
    dep.add_argument("--skip-fftw", action="store_true", help="Skip building FFTW")
    dep.add_argument("--skip-openssl", action="store_true", help="Skip building OpenSSL")
    dep.add_argument("--skip-hdf5", action="store_true", help="Skip building HDF5")
    dep.add_argument("--skip-lime", action="store_true", help="Skip building c-lime")
    dep.add_argument("--skip-libunwind", action="store_true", help="Skip building libunwind")

    # ── Dependency prefix overrides (use pre-installed) ─────────────
    paths = p.add_argument_group("dependency prefix overrides (skip build, use existing)")
    paths.add_argument("--with-gmp", metavar="PREFIX",
                       help="Use GMP from this prefix instead of building it")
    paths.add_argument("--with-mpfr", metavar="PREFIX",
                       help="Use MPFR from this prefix instead of building it")
    paths.add_argument("--with-fftw", metavar="PREFIX",
                       help="Use FFTW from this prefix instead of building it")
    paths.add_argument("--with-openssl", metavar="PREFIX",
                       help="Use OpenSSL from this prefix instead of building it")
    paths.add_argument("--with-hdf5", metavar="PREFIX",
                       help="Use HDF5 from this prefix instead of building it")
    paths.add_argument("--with-lime", metavar="PREFIX",
                       help="Use LIME (c-lime) from this prefix instead of building it")
    paths.add_argument("--with-unwind", metavar="PREFIX",
                       help="Use libunwind from this prefix instead of building it")

    # ── SIMD ────────────────────────────────────────────────────────
    simd = p.add_argument_group("SIMD target")
    simd.add_argument("--simd", default="GEN",
                      choices=["GEN", "SSE4", "AVX", "AVXFMA", "AVXFMA4", "AVX2",
                               "AVX512", "SKL", "KNL", "KNC",
                               "NEONv8", "A64FX", "QPX", "BGQ",
                               "GPU", "GPU-RRII"],
                      help="SIMD instruction set (default: GEN)")
    simd.add_argument("--gen-simd-width", type=int, metavar="BYTES",
                      help="Width in bytes of generic SIMD vectors (default: 64)")
    simd.add_argument("--gen-scalar", action="store_true",
                      help="Use generic scalar (non-SIMD) implementation")

    # ── Communications ──────────────────────────────────────────────
    comm = p.add_argument_group("communications")
    comm.add_argument("--comms", default="mpi-auto",
                      choices=["none", "mpi", "mpi-auto", "mpi3", "mpi3-auto"],
                      help="Communication backend (default: mpi-auto)")

    # ── Accelerator (GPU) ───────────────────────────────────────────
    gpu = p.add_argument_group("accelerator / GPU")
    gpu.add_argument("--accelerator", default="none",
                     choices=["none", "cuda", "sycl", "hip"],
                     help="GPU acceleration backend (default: none)")
    gpu.add_argument("--unified", default=None, action=argparse.BooleanOptionalAction,
                     help="Unified virtual address space for accelerator loops (default: yes)")
    gpu.add_argument("--accelerator-aware-mpi", default=None,
                     action=argparse.BooleanOptionalAction,
                     help="Allow MPI transfers from device memory (default: yes)")
    gpu.add_argument("--setdevice", action="store_true",
                     help="Set GPU device to node-local rank via cudaSetDevice / hipSetDevice")

    # ── Shared memory ──────────────────────────────────────────────
    shm = p.add_argument_group("shared memory")
    shm.add_argument("--shm", default=None,
                     choices=["shmopen", "shmget", "hugetlbfs", "shmnone", "nvlink", "no", "none"],
                     help="SHM allocation technique")
    shm.add_argument("--shmpath", metavar="PATH",
                     help="Mmap base path for hugetlbfs (default: /var/lib/hugetlbfs/…)")
    shm.add_argument("--shm-force-mpi", action="store_true",
                     help="Force MPI within shared memory nodes")
    shm.add_argument("--shm-fast-path", action="store_true",
                     help="Allow kernels to remote-copy over intranode links")

    # ── Allocator ───────────────────────────────────────────────────
    alloc = p.add_argument_group("allocator")
    alloc.add_argument("--alloc-align", choices=["2MB", "4k"],
                       help="Alignment of Grid allocator (default: 2MB)")
    alloc.add_argument("--alloc-cache", default=None, action=argparse.BooleanOptionalAction,
                       help="Cache pool of recent frees for reuse (default: yes)")

    # ── Gauge group & fermion reps ──────────────────────────────────
    phys = p.add_argument_group("gauge group / fermion content")
    phys.add_argument("--nc", default="3", choices=["2", "3", "4", "5", "8"],
                      help="Number of colours Nc (default: 3)")
    phys.add_argument("--sp", action="store_true",
                      help="Use symplectic gauge group Sp(2N) instead of SU(N)")
    phys.add_argument("--fermion-reps", default=None, action=argparse.BooleanOptionalAction,
                      help="Enable extra fermion representations (default: yes)")
    phys.add_argument("--gparity", default=None, action=argparse.BooleanOptionalAction,
                      help="Enable G-parity boundary conditions (default: yes)")
    phys.add_argument("--zmobius", default=None, action=argparse.BooleanOptionalAction,
                      help="Enable Zmöbius fermion actions (default: yes)")

    # ── RNG ─────────────────────────────────────────────────────────
    rng = p.add_argument_group("random number generator")
    rng.add_argument("--rng", default="sitmo",
                     choices=["sitmo", "ranlux48", "mt19937"],
                     help="RNG implementation (default: sitmo)")

    # ── Intel libraries ─────────────────────────────────────────────
    intel = p.add_argument_group("Intel libraries")
    intel.add_argument("--mkl", metavar="yes|no|PREFIX",
                       help="Use Intel MKL for LAPACK and FFTW (default: no)")
    intel.add_argument("--ipp", metavar="yes|no|PREFIX",
                       help="Use Intel IPP for fast CRC32C (default: no)")

    # ── LAPACK ──────────────────────────────────────────────────────
    la = p.add_argument_group("LAPACK")
    la.add_argument("--lapack", metavar="yes|no|PREFIX",
                    help="Enable LAPACK for Lanczos eigensolver (default: no)")

    # ── FP16 ────────────────────────────────────────────────────────
    fp = p.add_argument_group("precision")
    fp.add_argument("--sfw-fp16", default=None, action=argparse.BooleanOptionalAction,
                    help="Software FP16 conversion for comms (default: yes)")

    # ── Tracing ─────────────────────────────────────────────────────
    trace = p.add_argument_group("tracing / profiling")
    trace.add_argument("--tracing", default=None,
                       choices=["none", "nvtx", "roctx", "timer"],
                       help="Profiling tracing backend (default: none)")

    # ── Reduction ───────────────────────────────────────────────────
    red = p.add_argument_group("reduction")
    red.add_argument("--reduction", default=None, choices=["mpi", "grid"],
                     help="Internal reduction implementation (default: grid)")

    # ── Diagnostics ─────────────────────────────────────────────────
    diag = p.add_argument_group("diagnostics")
    diag.add_argument("--checksum-comms", action="store_true",
                      help="Checksum all MPI communication")
    diag.add_argument("--log-views", action="store_true",
                      help="Log all view open/close events")
    diag.add_argument("--debug", action="store_true",
                      help="Build with -g instead of -O3")

    # ── Timers ──────────────────────────────────────────────────────
    tim = p.add_argument_group("timers")
    tim.add_argument("--no-timers", action="store_true",
                     help="Disable high-resolution timers")

    # ── Chroma ──────────────────────────────────────────────────────
    ch = p.add_argument_group("chroma")
    ch.add_argument("--chroma", action="store_true",
                    help="Build with Chroma regression test support")

    # ── Compiler overrides ──────────────────────────────────────────
    comp = p.add_argument_group("compiler overrides")
    comp.add_argument("--cxx", metavar="COMPILER", help="C++ compiler (e.g. g++, icpc, nvcc, hipcc, dpcpp)")
    comp.add_argument("--cc", metavar="COMPILER", help="C compiler")
    comp.add_argument("--mpicxx", metavar="COMPILER", help="MPI C++ wrapper (e.g. mpicxx, mpiicpc)")
    comp.add_argument("--cxxflags", metavar="FLAGS", help="Extra CXXFLAGS")
    comp.add_argument("--ldflags", metavar="FLAGS", help="Extra LDFLAGS")

    # ── Git / build control ─────────────────────────────────────────
    build = p.add_argument_group("build control")
    build.add_argument("--jobs", "-j", default=NJOBS, metavar="N",
                       help=f"Parallel make jobs (default: {NJOBS})")
    build.add_argument("--grid-branch", default="develop", metavar="BRANCH",
                       help="Grid-HISQ git branch to clone (default: develop)")
    build.add_argument("--grid-pull", action="store_true",
                       help="Run git pull in an existing grid-src checkout")
    build.add_argument("--run-tests", action="store_true",
                       help="Build and run Grid's test suite after compilation")

    # ── Escape hatch ────────────────────────────────────────────────
    p.add_argument("--extra-configure-flags", metavar="'FLAGS'",
                   help="Raw string appended verbatim to the configure invocation")

    return p.parse_args()


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def _build_stage_list(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return the ordered list of ``(key, label)`` stages for this run."""
    stages: list[tuple[str, str]] = []

    stages.append(("compiler", "Resolving compiler"))

    if not args.skip_deps:
        # Each dependency that will actually be processed gets a stage.
        if not args.skip_gmp and not args.with_gmp:
            stages.append(("gmp", "Installing GMP"))
        if not args.skip_mpfr and not args.with_mpfr:
            stages.append(("mpfr", "Installing MPFR"))
        if not args.skip_fftw and not args.with_fftw:
            stages.append(("fftw", "Installing FFTW"))
        if not args.skip_openssl and not args.with_openssl:
            stages.append(("openssl", "Installing OpenSSL"))
        if not args.skip_hdf5 and not args.with_hdf5:
            stages.append(("hdf5", "Installing HDF5"))
        if not args.skip_lime and not args.with_lime:
            stages.append(("lime", "Installing c-lime"))
        if not args.skip_libunwind and not args.with_unwind:
            stages.append(("libunwind", "Installing libunwind"))

    stages.append(("grid", "Building Grid-HISQ"))
    return stages


def main() -> None:
    args = parse_args()
    root = Path.cwd()
    deps_prefix = root / "deps"
    ensure_dir(deps_prefix)

    stages = StageTracker(_build_stage_list(args))

    # ── Resolve compiler ───────────────────────────────────────────
    stages.begin("compiler")
    cc, cxx = ensure_compiler(
        deps_prefix, no_llvm=args.no_llvm, jobs=args.jobs,
    )
    print(f"Using CC={cc}  CXX={cxx}")

    # ── Build dependencies ──────────────────────────────────────────
    if not args.skip_deps:
        use_spack = args.use_spack
        if use_spack:
            ensure_spack(deps_prefix)

        # GMP  (required)
        if not args.skip_gmp and not args.with_gmp:
            stages.begin("gmp")
            build_or_spack(
                "gmp", build_gmp, (deps_prefix, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )

        # MPFR (required)
        if not args.skip_mpfr and not args.with_mpfr:
            stages.begin("mpfr")
            gmp_pfx = Path(args.with_gmp) if args.with_gmp else deps_prefix
            build_or_spack(
                "mpfr", build_mpfr, (deps_prefix, gmp_pfx, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )

        # FFTW (optional but recommended)
        if not args.skip_fftw and not args.with_fftw:
            stages.begin("fftw")
            build_or_spack(
                "fftw", build_fftw, (deps_prefix, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )

        # OpenSSL (required by Grid for SHA256)
        if not args.skip_openssl and not args.with_openssl:
            stages.begin("openssl")
            build_or_spack(
                "openssl", build_openssl, (deps_prefix, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )

        # HDF5 (optional)
        if not args.skip_hdf5 and not args.with_hdf5:
            stages.begin("hdf5")
            build_or_spack(
                "hdf5", build_hdf5, (deps_prefix, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )

        # LIME / c-lime (optional, for ILDG/SciDAC file format)
        if not args.skip_lime and not args.with_lime:
            stages.begin("lime")
            build_or_spack(
                "lime", build_lime, (deps_prefix, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )

        # libunwind (optional, for backtraces)
        if not args.skip_libunwind and not args.with_unwind:
            stages.begin("libunwind")
            build_or_spack(
                "libunwind", build_libunwind, (deps_prefix, args.jobs),
                deps_prefix, use_spack=use_spack, jobs=args.jobs,
            )
    else:
        print("--skip-deps: skipping all dependency builds")

    # ── Build Grid-HISQ ─────────────────────────────────────────────
    stages.begin("grid")
    build_grid(args, deps_prefix)

    stages.finish()


if __name__ == "__main__":
    main()
