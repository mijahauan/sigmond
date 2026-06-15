# Native binaries in sigmond client repos

Some sigmond clients depend on native (C / C++ / Fortran) executables or
libraries that don't ship via apt or PyPI: `dumphfdl` for `hfdl-recorder`,
`mag-usb` for `mag-recorder`, `wsprd` / `jt9` for `wspr-recorder`,
PHaRLAP for `hf-timestd`. This doc defines the convention every client
should use to manage them.

The goal is a **single, predictable layout** that lets `install.sh` do
the right thing on a fresh host without operator intervention, while
keeping the door open for a from-source rebuild on architectures or
distros the prebuilt doesn't cover.

## TL;DR — the contract

Inside each client repo:

```
<repo>/bin/<binary>                  # prebuilt, committed to git
<repo>/bin/<binary>.provenance       # YAML sidecar (see schema below)
<repo>/scripts/build-<binary>.sh     # idempotent from-source build
<repo>/scripts/install.sh            # uses the prebuilt; falls back to build
```

`install.sh` decides between the prebuilt and the from-source build using
the rules in [§ install.sh fallback contract](#installsh-fallback-contract).
The prebuilt is always the fast path; the build script is always the
escape hatch.

## When to ship prebuilt vs build-on-install

Decide per binary on three axes. There's no universal answer — `mag-usb`
and `dumphfdl` correctly land on different sides.

| Axis | Favors prebuilt | Favors build-fresh |
|---|---|---|
| Build cost | > 5 min wall clock, > 3 system packages, or pulls multiple upstream git repos | seconds, standard build-essential |
| Upstream churn | source is pinned / rarely changes | tracks upstream master closely |
| Architecture spread | x86_64 only, on stock Debian | likely to run on ARM / RPi / non-Debian |
| Repo size impact | binary ≲ 10 MB | binary > 50 MB (then use git-lfs or release-asset fetch) |

If two axes favor prebuilt, ship prebuilt **and** keep the build script.
If two axes favor build-fresh, ship the build script only.

Current sigmond clients:

| Client | Binary | Decision | Why |
|---|---|---|---|
| `hfdl-recorder` | `dumphfdl` (+ libacars, liquid-dsp) | **both** (prebuilt + script) | ~15 min build, 3 upstream clones, 10+ apt deps |
| `wspr-recorder` | `wsprd`, `jt9` | **both** (prebuilt + script) | Qt + boost + fftw, ~5 min build |
| `mag-recorder` | `mag-usb` | **both** (prebuilt + script) | small build but likely RPi target (ARM); ship x86_64 prebuilt, build-fresh covers ARM |
| `hf-timestd` | PHaRLAP | **external only** | closed-source (DST), can't redistribute — operator-staged via the client's `scripts/install-pharlap.sh` (or baked into the **controlled** DASI2 golden image as single-licensee internal use). GCC/gfortran-built static libs in 4.7.4 — no Intel Fortran, no MATLAB MCR. |
| `hf-timestd` | pyLAP | **build-on-install (pinned)** | open fork (`mijahauan/PyLap`) built into the venv; pin (`PYLAP_REF`) lives in the client's `scripts/ensure-pylap.sh` — the single idempotent builder run by both `install.sh` and `deploy.toml` `[build].steps`, so clones self-heal raytracing after a venv rebuild. Stand-alone: paths derive from the script's own location, no sigmond required. |

`iri2020` is a pip-installable git dep handled by `uv` — it doesn't fit
the binary `.provenance` contract, but the same *pin the source* principle
applies: it's pinned via `@<sha>` in hf-timestd's `pyproject.toml` and
locked in its `uv.lock`.

## Upstream C projects sigmond builds itself

`ka9q-radio` (radiod, plus the in-tree `fobos` driver) and `ka9q-web`
(plus its `onion` library dependency) are a different case from the
client binaries above: their source repos are **upstream** and not
sigmond-owned (`ka9q/ka9q-radio`, `ka9q/ka9q-web`,
`davidmoreno/onion`), so sigmond cannot commit a prebuilt binary +
provenance sidecar *into* them. sigmond builds them from source on the
host instead (`_install_radiod_native`, `_install_ka9q_web_native` in
`bin/smd`).

The mag-usb principles still apply — only the **location** of the pin and
provenance moves out of the (un-ownable) upstream repo and into sigmond:

| Principle | Client-binary case | Upstream-built case |
|---|---|---|
| Pin the source | `.provenance` `upstream.sha` committed in the client repo | a pinned commit SHA in sigmond (`_ONION_COMMIT`, ka9q-radio pin) — never a bare branch / HEAD |
| Record provenance | `bin/<binary>.provenance` committed to git | written to `/var/lib/sigmond/build-manifest/<component>.toml` after the build |
| Idempotent build | `scripts/build-<binary>.sh` | the `_install_*_native` / `_build_*` helpers (they sha-check before rebuilding) |
| Verify on host | `install.sh` provenance check | surfaced in `smd admin diag` (cf. the `ka9q_python_compat` cross-repo pin rule) |

### Current compliance

| Component | Native dep | Source | Pinned? | Provenance? | Gap to close |
|---|---|---|---|---|---|
| `ka9q-radio` | radiod + `fobos` driver | `ka9q/ka9q-radio` (fobos in-tree) | tracks upstream | no | `fobos` disabled by default; `libfobos` is proprietary/external, so it stays opt-in |
| `ka9q-web` | `onion` | `davidmoreno/onion` | **yes** — `_ONION_COMMIT` pinned | **yes** — `build-manifest/ka9q-web.toml` | ✓ fully migrated (pin + manifest + `smd admin diag` drift check) |

`onion` was the first migration: it is pinned (`_ONION_COMMIT`) and
`_build_ka9q_web_with_onion` writes
`/var/lib/sigmond/build-manifest/ka9q-web.toml` after each build. The
`smd admin diag` now compares each installed build manifest against the current
pins (`_diag_build_manifests` / `_expected_build_pins`) — the
upstream-built analogue of `install.sh`'s provenance check — and warns
with a rebuild hint on drift. The remaining work is applying the same
pin + manifest + diag treatment to any future upstream-built dep. The
principles are identical to mag-usb; only the storage location differs
because we don't own the source repo.

## The `.provenance` sidecar

Every committed binary under `<repo>/bin/` MUST have a matching
`<binary>.provenance` YAML file alongside it. The sidecar lets
`install.sh` answer "is this binary current?" without trial-running it,
and lets future maintainers know which upstream commits to bisect when
something breaks.

### Schema

```yaml
# bin/<binary>.provenance — provenance for a committed native binary
binary: dumphfdl                       # filename in this directory
version: "1.7.0"                       # human-readable version (from --version, or upstream tag)

upstream:                              # one block per upstream repo built into this binary
  - name: dumphfdl
    url:  https://github.com/szpajder/dumphfdl
    ref:  master                       # branch/tag name as built
    sha:  3a4b5c6d7e8f...              # exact commit SHA
  - name: libacars
    url:  https://github.com/szpajder/libacars
    ref:  master
    sha:  1122334455...
  - name: liquid-dsp
    url:  https://github.com/jgaeddert/liquid-dsp
    ref:  master
    sha:  aabbccddeeff...

build:
  host:          bookworm-builder       # short identifier of the build host
  os:            "Debian GNU/Linux 12 (bookworm)"
  kernel:        "6.1.0-47-amd64"
  arch:          x86_64                 # uname -m
  glibc:         "2.36"                 # ldd --version | head -1 | awk '{print $NF}'
  cmake:         "3.25.1"
  gcc:           "12.2.0"
  date:          2026-05-29T14:32:00Z   # ISO-8601 UTC
  builder:       "build-dumphfdl.sh"    # script that produced the binary
  builder_sha:   "abc123def456"         # git SHA of this client repo at build time

runtime:
  needs_apt:                            # runtime apt packages this binary will dlopen
    - libfftw3-single3
    - libsoapysdr0.8
    - libxml2
    - libjansson4
    - libsqlite3-0
  rpath:                                # rpath entries baked into the binary
    - $ORIGIN/../lib
```

### Field rules

- **`upstream`**: list every git repo whose source is compiled into the binary.
  For a single-source build (like `mag-usb`), one entry. For `dumphfdl`,
  three entries.
- **`sha`**: full 40-char commit SHA. Truncated SHAs are ambiguous over time
  as upstream history grows.
- **`build.glibc`**: critical for forward-compat. A binary built on glibc 2.36
  runs on hosts with glibc ≥ 2.36. To maximize portability, **build the
  shipped binary on the oldest Debian release sigmond targets** (currently
  Debian bookworm / glibc 2.36).
- **`build.arch`**: `x86_64` today. When ARM hosts arrive, ship a second
  binary (`bin/<binary>.aarch64`) with its own sidecar; `install.sh` picks
  the right one by `uname -m`.
- **`runtime.needs_apt`**: lets `install.sh` verify shared-library deps
  are present before trusting the prebuilt. Cheaper than running
  `--version` to find out.

### Where the sidecar comes from

The build script writes it. See `hfdl-recorder/scripts/build-dumphfdl.sh`
for the canonical implementation: after a successful build it captures
the upstream SHAs from each cloned source tree, reads glibc / arch /
toolchain versions from the host, and emits `bin/<binary>.provenance`
next to the binary. Operators don't write provenance by hand.

## install.sh fallback contract

Every client whose `install.sh` deals with a native binary must implement
this decision tree. The reference implementation is
`hfdl-recorder/scripts/install.sh:158-168`.

```text
if --no-build flag is set:
    require bin/<binary> to exist; use it; exit if missing.
elif --force-build flag is set:
    run scripts/build-<binary>.sh --force; ignore prebuilt.
elif bin/<binary> exists AND provenance OK for this host:
    use the prebuilt.
else:
    run scripts/build-<binary>.sh.
```

"Provenance OK for this host" means:

1. `bin/<binary>.provenance` exists and parses,
2. `build.arch` matches `uname -m`,
3. host glibc ≥ `build.glibc` (per `ldd --version`),
4. every `runtime.needs_apt` package is installed.

If any check fails, fall through to the build script. The build script
is responsible for installing missing apt deps; `install.sh` should not
duplicate that logic.

## Build-script conventions

`scripts/build-<binary>.sh` MUST:

- Be idempotent. A second run with no source change is a no-op (skip
  via per-source `.installed-rev` stamps; see `build-dumphfdl.sh`).
- Honor `--force` to bypass the stamp.
- Honor `--no-apt` to skip apt-get (for hosts under apt-pinning policy).
- Run as root (uses `apt-get`, writes to `bin/`). Refuse non-root.
- Default upstream URL + ref to known-good values, but honor env-var
  overrides (`<NAME>_URL`, `<NAME>_REF`) for testing newer revs.
- After a successful build, write the `.provenance` sidecar atomically
  (write to `.provenance.tmp`, then `mv`). This protects half-written
  sidecars from a crashed build leaving an incorrect file in git.
- Sanity-check the built binary by invoking `--version` (or equivalent)
  through the rpath. A binary that builds but won't run is a hard error.

`scripts/build-<binary>.sh` SHOULD NOT:

- Install systemd units, service users, or config — that's `install.sh`'s job.
- Modify anything outside `<repo>/bin/`, `<repo>/lib/`, `<repo>/include/`,
  `<repo>/share/`, and the scratch build dir.
- Assume `/opt/git/sigmond/<other-repo>/` exists — clone scratch sources
  into `/var/cache/<client>/build/` and own them there.

## Toolchain choices

Pick the **oldest supported Debian** as the build host for shipped
binaries. Today that's Debian bookworm (glibc 2.36, gcc 12). Building on
trixie or sid produces binaries that won't run on bookworm hosts — and
sigmond has bookworm production deployments.

For cross-arch (aarch64), build natively on an ARM host rather than
cross-compiling. Cross toolchains tend to disagree with cmake's
auto-detection inside upstream projects we don't control.

## Repo size considerations

If shipped binaries across a single client exceed ~50 MB, move them to
git-lfs or a release-asset fetch in `install.sh`. Today no client is
close to that threshold:

- `hfdl-recorder/bin/dumphfdl`: 169 KB (+ 3 other small helpers, ~50 KB each)
- `mag-recorder/bin/mag-usb`: ~80 KB (expected)
- `wspr-recorder/bin/decoders/{wsprd,jt9}`: ~1 MB combined (expected)

The hidden cost is `<repo>/lib/` — `dumphfdl` ships a couple of MB of
`libacars.so` and `libliquid.so` next to the binary. That counts toward
the repo-size budget too. Keep an eye on it when bundling.

## What this convention is NOT

- Not a Python packaging convention — Python wheels and `uv` editable
  installs are unrelated and well-covered elsewhere.
- Not a replacement for system packages when they're sufficient.
  `libfftw3-single3` ships with Debian; we use it via apt, not via bundle.
- Not a release-engineering pipeline. There's no CI that re-builds
  bookworm binaries on every commit; binaries are committed by the
  maintainer when upstream pins move. Provenance is the audit trail.

## See also

- `hfdl-recorder/scripts/build-dumphfdl.sh` — canonical multi-upstream build script.
- `hfdl-recorder/scripts/install.sh` — canonical install fallback logic.
- `mag-recorder/scripts/build-mag-usb.sh` — single-upstream build script.
- `CLIENT-CONTRACT.md` — overall client contract; this doc is the native-binary annex.
