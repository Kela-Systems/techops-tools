#!/usr/bin/env python3
"""Collect a sideload set for extra-debs/ that is safe to `dpkg -i` on the target.

The obvious way to do this — `apt-get install --download-only usbguard` in an
`ubuntu:24.04` container — is wrong, and produced a set that bricked a box. The
container is not the target: it already had polkit installed, so resolving there
downloaded a newer `libpolkit-gobject-1-0` and none of polkit's other binaries.
On the target that landed as a half-upgraded polkit, `polkitd` and
`libpolkit-agent-1-0` kept their `Depends: ... (= <old version>)`, and every
later apt invocation died with `E: Unmet dependencies` — unfixable on a box with
no network.

So resolve against the actual install instead. The ISO's casper manifests list
exactly what a `source: {id: ubuntu-server}` install puts on disk, and the two
rules that follow from having that list are:

  1. Never upgrade to satisfy a dependency the installed version already
     satisfies. A sideload set exists to add what is missing, not to move the
     base system forward.
  2. If an upgrade is genuinely forced, every installed binary package built
     from the same source must come along. Members of one source are routinely
     locked to each other with `=`, which is precisely how the polkit break
     happened.

Rule 2 is the load-bearing one, and it is checked rather than assumed: the run
fails if it cannot be satisfied, so a bad set is never written to extra-debs/.
"""

import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys
import urllib.request

MIRROR = "http://archive.ubuntu.com/ubuntu"
SUITES = ("noble", "noble-updates", "noble-security")
COMPONENTS = ("main", "restricted", "universe", "multiverse")


# --------------------------------------------------------------------------
# dpkg version comparison
# --------------------------------------------------------------------------
# Reimplemented because this has to run on the macOS build host, where there is
# no python-apt and no dpkg. Follows deb-version(7).

def _order(c):
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _cmp_fragment(a, b):
    """Compare one upstream/revision fragment: alternating non-digit and digit runs."""
    i = j = 0
    while i < len(a) or j < len(b):
        # Non-digit run, compared with '~' sorting before the end of a string.
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) and not a[i].isdigit() else 0
            bc = _order(b[j]) if j < len(b) and not b[j].isdigit() else 0
            if ac != bc:
                return -1 if ac < bc else 1
            if i < len(a) and not a[i].isdigit():
                i += 1
            if j < len(b) and not b[j].isdigit():
                j += 1
        # Digit run, compared numerically so leading zeros do not matter.
        ai = i
        while i < len(a) and a[i].isdigit():
            i += 1
        bj = j
        while j < len(b) and b[j].isdigit():
            j += 1
        an = int(a[ai:i] or "0")
        bn = int(b[bj:j] or "0")
        if an != bn:
            return -1 if an < bn else 1
    return 0


def dpkg_cmp(v1, v2):
    if v1 == v2:
        return 0

    def split(v):
        epoch, sep, rest = v.partition(":")
        if not sep or not epoch.isdigit():
            epoch, rest = "0", v
        upstream, sep, revision = rest.rpartition("-")
        if not sep:
            upstream, revision = rest, ""
        return int(epoch), upstream, revision

    e1, u1, r1 = split(v1)
    e2, u2, r2 = split(v2)
    if e1 != e2:
        return -1 if e1 < e2 else 1
    c = _cmp_fragment(u1, u2)
    if c:
        return c
    return _cmp_fragment(r1, r2)


def satisfies(installed, op, wanted):
    """Does version `installed` satisfy the constraint `op wanted`?"""
    if op is None:
        return True
    c = dpkg_cmp(installed, wanted)
    return {
        "<<": c < 0,
        "<=": c <= 0,
        "=": c == 0,
        ">=": c >= 0,
        ">>": c > 0,
    }[op]


# --------------------------------------------------------------------------
# Inputs: the ISO's package manifest and the archive's Packages indices
# --------------------------------------------------------------------------

# `+` is legal in a package name (libstdc++6), so the leading marker cannot be
# stripped by excluding it from the name — the diff headers are filtered first.
MANIFEST_LINE = re.compile(r"^\+([^\s:]+?)(?::[^\s:]+)?\s+(\S+)\s*$")


def baseline_from_manifests(text):
    """Parse casper's diff-style manifests into {package: version}.

    Lines look like `+adduser\t3.137ubuntu1`; `+++`/`---` are diff headers and
    the rare `-pkg` line is a package the layer removes.
    """
    installed = {}
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        m = MANIFEST_LINE.match(line)
        if m:
            installed[m.group(1)] = m.group(2)
        elif line.startswith("-"):
            installed.pop(line[1:].split()[0].split(":")[0], None)
    return installed


# Exactly the two layers a `source: {id: ubuntu-server}` install lays down, per
# casper/install-sources.yaml. Named rather than globbed on purpose: the ISO also
# carries ubuntu-server-minimal.ubuntu-server.installer.*.manifest, and those
# list packages that exist only in the installer environment. Counting those as
# installed would let a sideload that depends on one of them pass this check and
# then fail on the box.
CASPER_LAYERS = (
    "ubuntu-server-minimal.manifest",
    "ubuntu-server-minimal.ubuntu-server.manifest",
)


def read_baseline(path):
    """Accept either a manifest file or a casper directory.

    A built stick has the ISO extracted onto EFIBOOT, so `update-usb-macos.sh`
    can point at /Volumes/EFIBOOT/casper and verify with no ISO file and no
    network.
    """
    if os.path.isdir(path):
        chunks = []
        for name in CASPER_LAYERS:
            layer = os.path.join(path, name)
            if os.path.exists(layer):
                with open(layer, "r", encoding="utf-8", errors="replace") as f:
                    chunks.append(f.read())
        if not chunks:
            sys.exit("no ubuntu-server layer manifest in %s — expected %s"
                     % (path, " and ".join(CASPER_LAYERS)))
        return "\n".join(chunks)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_manifests(iso):
    """Pull the ubuntu-server layer manifests out of the ISO.

    bsdtar reads ISO9660 directly and is `tar` on macOS; xorriso is the
    fallback and is already a build dependency on the mac host.
    """
    wanted = ["casper/" + name for name in CASPER_LAYERS]
    for tool in ("bsdtar", "tar"):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            out = subprocess.run(
                [exe, "-xOf", iso] + wanted,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if out.stdout.strip():
                return out.stdout.decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            pass
    if shutil.which("xorriso"):
        chunks = []
        for path in wanted:
            out = subprocess.run(
                ["xorriso", "-osirrox", "on", "-indev", iso, "-extract", path, "/dev/stdout"],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            chunks.append(out.stdout.decode("utf-8", "replace"))
        return "\n".join(chunks)
    sys.exit("need bsdtar/tar or xorriso to read the ISO; or pass --baseline")


def fetch_indices(cache, arch):
    """Download and parse every Packages index, newest version of each name wins."""
    index = {}
    provides = {}
    os.makedirs(cache, exist_ok=True)
    for suite in SUITES:
        for comp in COMPONENTS:
            name = "%s_%s_%s_Packages.gz" % (suite, comp, arch)
            path = os.path.join(cache, name)
            if not os.path.exists(path):
                url = "%s/dists/%s/%s/binary-%s/Packages.gz" % (MIRROR, suite, comp, arch)
                sys.stderr.write("  fetching %s/%s\n" % (suite, comp))
                try:
                    with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
                        shutil.copyfileobj(r, f)
                except Exception as exc:
                    sys.exit("could not fetch %s: %s" % (url, exc))
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                for pkg in parse_stanzas(f.read()):
                    name = pkg["Package"]
                    have = index.get(name)
                    if have is None or dpkg_cmp(pkg["Version"], have["Version"]) > 0:
                        index[name] = pkg
    return index, build_provides(index)


def build_provides(records):
    """{virtual name: {provider: provided version or None}}

    Provides can be versioned — `libprotobuf32t64` ships
    `Provides: libprotobuf32 (= 3.21.12-8.2ubuntu0.3)`, and `libusbguard1`
    depends on `libprotobuf32 (>= 3.21.12)`. Dropping the version here makes
    every such dependency look unsatisfiable.
    """
    out = {}
    for name, rec in records.items():
        for alt in parse_relations(rec.get("Provides", "")):
            for virtual, op, ver in alt:
                out.setdefault(virtual, {})[name] = ver if op == "=" else None
    return out


def dep_met(alts, version_of, provides_map):
    """Is one comma-separated dependency clause (its alternatives) satisfied?"""
    for name, op, want in alts:
        have = version_of(name)
        if have is not None and satisfies(have, op, want):
            return True
        for provider, provided in provides_map.get(name, {}).items():
            if version_of(provider) is None:
                continue
            if op is None:
                return True
            if provided is not None and satisfies(provided, op, want):
                return True
    return False


def parse_stanzas(text):
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = {}
        key = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t"):
                if key:
                    fields[key] += "\n" + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if "Package" in fields and "Version" in fields:
            yield fields


REL = re.compile(r"^([^\s(:\[]+)(?::[^\s(\[]+)?\s*(?:\(\s*(<<|<=|=|>=|>>|<|>)\s*([^)]+)\))?")


def parse_relations(field):
    """`a (>= 1), b:any | c` -> [[('a','>=','1')], [('b',None,None),('c',None,None)]]"""
    groups = []
    for clause in field.split(","):
        clause = clause.strip()
        if not clause:
            continue
        alts = []
        for alt in clause.split("|"):
            m = REL.match(alt.strip())
            if not m:
                continue
            op = m.group(2)
            if op == "<":
                op = "<="
            elif op == ">":
                op = ">="
            alts.append((m.group(1), op, m.group(3).strip() if m.group(3) else None))
        if alts:
            groups.append(alts)
    return groups


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def source_of(pkg):
    return pkg.get("Source", pkg["Package"]).split()[0]


def resolve(targets, installed, index, provides, source="the archive"):
    """Select the packages that must be shipped, and say which are upgrades."""
    selected = {}
    problems = []

    def version_in_play(name):
        if name in selected:
            return selected[name]["Version"]
        return installed.get(name)

    queue = list(targets)
    while queue:
        name = queue.pop(0)
        if name in selected:
            continue
        pkg = index.get(name)
        if pkg is None:
            candidates = sorted(provides.get(name, ()))
            if not candidates:
                problems.append("no package named %s in %s" % (name, source))
                continue
            pkg = index[candidates[0]]
            name = pkg["Package"]
            if name in selected:
                continue
        selected[name] = pkg
        relations = parse_relations(pkg.get("Pre-Depends", "")) + \
            parse_relations(pkg.get("Depends", ""))
        for alts in relations:
            if dep_met(alts, version_in_play, provides):
                continue
            # Rule 1: only reach for the archive when the installed version
            # cannot satisfy the constraint at all. Among the alternatives,
            # take one whose available version actually meets the constraint —
            # picking merely by name lands on a candidate that is too old and
            # reports a missing dependency further down instead of here.
            pick = None
            for dep_name, op, want in alts:
                cand = index.get(dep_name)
                if cand is not None and satisfies(cand["Version"], op, want):
                    pick = dep_name
                    break
            if pick is None:
                for dep_name, op, want in alts:
                    for provider, provided in sorted(provides.get(dep_name, {}).items()):
                        if op is None or (provided is not None
                                          and satisfies(provided, op, want)):
                            pick = provider
                            break
                    if pick:
                        break
            if pick is None:
                problems.append("%s depends on %s, which %s cannot satisfy"
                                % (name, " | ".join(
                                    a[0] + (" (%s %s)" % (a[1], a[2]) if a[1] else "")
                                    for a in alts), source))
                continue
            queue.append(pick)

    upgrades = {n: (installed[n], p["Version"])
                for n, p in selected.items()
                if n in installed and dpkg_cmp(p["Version"], installed[n]) != 0}
    return selected, upgrades, problems


def check_lockstep(selected, upgrades, installed, index):
    """Rule 2: an upgraded package drags its installed same-source siblings along.

    Returns the sibling names that are missing from the set. Each one is a
    potential `Depends: ... (= version)` break on the target.
    """
    by_source = {}
    for name, pkg in index.items():
        by_source.setdefault(source_of(pkg), set()).add(name)

    missing = {}
    for name in upgrades:
        src = source_of(index[name])
        for sibling in sorted(by_source.get(src, ())):
            if sibling == name or sibling in selected:
                continue
            if sibling in installed:
                missing.setdefault(src, []).append(sibling)
    return missing


# --------------------------------------------------------------------------
# Verification of a set already sitting in extra-debs/
# --------------------------------------------------------------------------
# Reads the .deb files themselves rather than the archive index, so what gets
# checked is what will actually ship. Needs no network, because the offline
# Linux runbook has none.

def deb_control(path):
    """A .deb's control stanza, or None if the file is not a readable package.

    None rather than an exit: the caller turns it into one reported problem
    among others, which is more use than aborting the whole audit on the first
    odd file in the directory.
    """
    if shutil.which("dpkg-deb"):
        out = subprocess.run(["dpkg-deb", "-f", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return next(parse_stanzas(out.stdout.decode("utf-8", "replace") + "\n\n"))
    tar = shutil.which("bsdtar") or shutil.which("tar")
    if not tar:
        sys.exit("need dpkg-deb or bsdtar to read %s" % path)
    for member in ("control.tar.zst", "control.tar.xz", "control.tar.gz"):
        inner = subprocess.run([tar, "-xOf", path, member],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if inner.returncode != 0 or not inner.stdout:
            continue
        for name in ("./control", "control"):
            ctrl = subprocess.run([tar, "-xOf", "-", name], input=inner.stdout,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if ctrl.returncode == 0 and ctrl.stdout:
                return next(parse_stanzas(ctrl.stdout.decode("utf-8", "replace") + "\n\n"))
    return None


def read_deb_dir(directory):
    """{package: control stanza} for the readable .debs in a directory.

    `._name` files are macOS AppleDouble stubs, written whenever a file with
    extended attributes is copied onto FAT — which the seed partition is. They
    match *.deb without being packages. The builders sweep them, but a stick
    touched on any other Mac grows them back, so skip them here too.
    """
    found, unreadable = {}, []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".deb") or name.startswith("._"):
            continue
        ctrl = deb_control(os.path.join(directory, name))
        if ctrl is None:
            unreadable.append(name)
        else:
            found[ctrl["Package"]] = ctrl
    return found, unreadable


def read_flat_repo(path):
    """Parse a flat apt repo's Packages index, newest version of each name."""
    index = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for pkg in parse_stanzas(f.read()):
            have = index.get(pkg["Package"])
            if have is None or dpkg_cmp(pkg["Version"], have["Version"]) > 0:
                index[pkg["Package"]] = pkg
    return index


def check_conflicts(records, state):
    """Conflicts/Breaks declared by `records` that `state` actually trips."""
    problems = []
    for name, rec in sorted(records.items()):
        for field in ("Conflicts", "Breaks"):
            for alts in parse_relations(rec.get(field, "")):
                for dep, op, want in alts:
                    if dep == name:
                        continue
                    have = state.get(dep)
                    if have is not None and satisfies(have, op, want):
                        problems.append(
                            "%s %s %s%s, which the box will have at %s"
                            % (name, field.lower(), dep,
                               (" (%s %s)" % (op, want)) if op else "", have))
    return problems


def stranded_siblings(upgrades, installed, index):
    """Installed packages an upgrade in this set would likely strand.

    `check_lockstep` reads the sibling relation off a Source field, which works
    against the full Ubuntu index but not here: the bundle's flat repo carries a
    few hundred packages and casper's manifest carries names and versions only —
    no Source, no Depends. Shared version is a sound stand-in, because one
    source's binaries are built and versioned together and a full Ubuntu version
    string like 4.0.1really4.0.1-0ubuntu0.24.04.5 does not collide by accident.

    This is the polkit break in general form: libpolkit-gobject-1-0 moved while
    polkitd, pinned to the old version by `Depends: (= ...)`, stayed — and apt
    then refused every transaction on the box.
    """
    stranded = {}
    for name, (old, new) in sorted(upgrades.items()):
        for other, version in sorted(installed.items()):
            if other == name or version != old:
                continue
            # If the repo carries the sibling at the new version, apt can move
            # it too, and will as soon as a dependency asks.
            alongside = index.get(other)
            if alongside is not None and dpkg_cmp(alongside["Version"], new) == 0:
                continue
            stranded.setdefault(name, []).append(other)
    return stranded


def check_bundle_debs(bundle, installed, shipped):
    """Cross-check the sideload set against the Kela bundle it ships beside.

    Two package installs happen on first boot, in this order: `dpkg -i` of the
    seed partition's debs, then `apt-get install` of 02-kela/debs resolved
    against 02-kela/apt. Verifying the sideloads against the ISO alone says
    nothing about the second, so a bundle that wants a different version of
    something the sideload just pinned would only surface on the box — after
    Ubuntu has installed perfectly well and with the one-shot guard already set.

    Fatal here means the bundle install cannot succeed. The `apt-get install`
    that runs it exits 100 on any of these, which is where activation died.
    """
    problems, notes = [], []
    debs_dir = os.path.join(bundle, "debs")
    index_path = os.path.join(bundle, "apt", "Packages")

    bundle_debs, unreadable = ({}, [])
    if os.path.isdir(debs_dir):
        bundle_debs, unreadable = read_deb_dir(debs_dir)
    # Fatal, not skippable: an unreadable deb here is one the bundle install
    # will choke on, and skipping it would turn the cross-check green by
    # quietly dropping the very package it was asked about.
    problems.extend("02-kela/debs/%s is not a readable .deb — a truncated copy "
                    "will fail the bundle install on the box" % name
                    for name in unreadable)
    if not bundle_debs:
        if not unreadable:
            notes.append("no debs under 02-kela/debs — activate.sh installs Kela itself")
        return problems, notes

    repo = read_flat_repo(index_path) if os.path.exists(index_path) else {}
    notes.append("bundle: %s" % ", ".join(
        "%s %s" % (n, c["Version"]) for n, c in sorted(bundle_debs.items())))
    notes.append("bundle apt repo: %s"
                 % ("%d packages" % len(repo) if repo else "none — the debs are on their own"))

    # The state the bundle install starts from: the ISO plus the sideloads,
    # because the seed partition is installed first.
    state = dict(installed)
    for name, ctrl in shipped.items():
        state[name] = ctrl["Version"]

    # A sideload and the bundle repo both carrying one package is not fatal —
    # apt resolves properly — but it means apt may move a version the sideload
    # just placed, so it is worth naming.
    for name in sorted(set(shipped) & set(repo)):
        cmp_ = dpkg_cmp(repo[name]["Version"], shipped[name]["Version"])
        if cmp_ != 0:
            notes.append(
                "both sets carry %s: sideload %s, bundle repo %s — apt may %s it "
                "during the bundle install"
                % (name, shipped[name]["Version"], repo[name]["Version"],
                   "upgrade" if cmp_ > 0 else "downgrade"))

    available = dict(repo)
    available.update(bundle_debs)
    provides = build_provides(available)
    selected, upgrades, unmet = resolve(
        sorted(bundle_debs), state, available, provides,
        source="the bundle's own repo (02-kela/apt) plus the base install")
    problems.extend(unmet)

    pulled = sorted(n for n in selected if n not in bundle_debs)
    notes.append("apt pulls %d package(s) off the drive: %s"
                 % (len(pulled), ", ".join(pulled) if pulled else "none"))
    if upgrades:
        notes.append("the bundle install would change %d installed package(s): %s"
                     % (len(upgrades), ", ".join(
                         "%s %s->%s" % (n, o, v) for n, (o, v) in sorted(upgrades.items()))))
        for name, siblings in sorted(
                stranded_siblings(upgrades, state, available).items()):
            old, new = upgrades[name]
            problems.append(
                "the bundle install moves %s %s -> %s, but %s stay(s) at %s and the "
                "bundle repo has no %s build of them. Same-version packages are "
                "siblings from one source; if any is pinned to the old version, apt "
                "refuses the whole transaction and the box is stuck."
                % (name, old, new, ", ".join(siblings), old, new))

    final = dict(state)
    for name, ctrl in selected.items():
        final[name] = ctrl["Version"]
    everything = dict(shipped)
    everything.update(selected)
    problems.extend(check_conflicts(everything, final))
    return problems, notes


def verify(directory, installed):
    """Check a sideload directory is safe to `dpkg -i` on top of `installed`.

    The invariant, and the whole reason this exists: a sideload deb may add a
    package but must never change the version of one the ISO already installs.
    Hold that and a partial upgrade is impossible, which is what the polkit
    break was.
    """
    shipped, unreadable = read_deb_dir(directory)
    if not shipped and not unreadable:
        return ["no .deb files in %s" % directory], {}

    problems = ["%s is not a readable .deb — truncated copy, or not a package "
                "at all" % f for f in unreadable]

    state = dict(installed)
    for name, ctrl in shipped.items():
        if name in installed:
            c = dpkg_cmp(ctrl["Version"], installed[name])
            if c > 0:
                problems.append(
                    "%s UPGRADES the base install (%s -> %s). A sideload must not "
                    "move the base system: siblings from the same source stay behind "
                    "and lock apt out of the box for good."
                    % (name, installed[name], ctrl["Version"]))
            elif c < 0:
                problems.append("%s DOWNGRADES the base install (%s -> %s)"
                                % (name, installed[name], ctrl["Version"]))
            else:
                print("  note: %s %s is already installed — a same-version reinstall "
                      "only re-runs its maintainer scripts on a booting box"
                      % (name, ctrl["Version"]))
        state[name] = ctrl["Version"]

    # Only the shipped debs' Provides are known here: casper's manifest lists
    # names and versions, not Provides. That is fine in practice because the
    # collector ships whatever satisfies a virtual dependency, so the provider
    # is in this directory whenever the base install does not already carry the
    # real package.
    virtual = build_provides(shipped)

    for name, ctrl in sorted(shipped.items()):
        relations = parse_relations(ctrl.get("Pre-Depends", "")) + \
            parse_relations(ctrl.get("Depends", ""))
        for alts in relations:
            if dep_met(alts, state.get, virtual):
                continue
            shown = " | ".join(
                d + (" (%s %s)" % (o, w) if o else "") for d, o, w in alts)
            problems.append(
                "%s depends on %s — not satisfied by the ISO's install nor by "
                "anything in this directory" % (name, shown))
    return problems, shipped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="*", default=["usbguard"],
                    help="packages to collect (default: usbguard)")
    ap.add_argument("--iso", help="ubuntu-*-live-server-amd64.iso to read the baseline from")
    ap.add_argument("--baseline",
                    help="casper manifest file, or a casper directory (e.g. the "
                         "EFIBOOT partition of a built stick), instead of --iso")
    ap.add_argument("--out", help="directory to write .deb files into")
    ap.add_argument("--arch", default="amd64")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/kela-extra-debs"))
    ap.add_argument("--check-only", action="store_true",
                    help="resolve and report, download nothing")
    ap.add_argument("--verify", metavar="DIR",
                    help="check the .debs already in DIR instead of resolving; "
                         "reads the debs themselves and needs no network")
    ap.add_argument("--bundle", metavar="DIR",
                    help="with --verify, also cross-check against the Kela bundle "
                         "(the 02-kela directory, or its parent)")
    args = ap.parse_args()
    targets = args.packages or ["usbguard"]

    if args.baseline:
        manifest_text = read_baseline(args.baseline)
    elif args.iso:
        manifest_text = extract_manifests(args.iso)
    else:
        sys.exit("need --iso or --baseline: the whole point is resolving against "
                 "the real install, not against whatever the build host has")

    installed = baseline_from_manifests(manifest_text)
    if not installed:
        sys.exit("baseline is empty — wrong manifest?")
    print("baseline: %d packages from the ubuntu-server install" % len(installed))

    if args.verify:
        print("verifying %s against that baseline" % args.verify)
        problems, shipped = verify(args.verify, installed)
        checked_bundle = False
        if args.bundle:
            bundle = args.bundle
            if os.path.isdir(os.path.join(bundle, "02-kela")):
                bundle = os.path.join(bundle, "02-kela")
            if os.path.isdir(bundle):
                print("cross-checking against the bundle at %s" % bundle)
                extra, notes = check_bundle_debs(bundle, installed, shipped)
                for note in notes:
                    print("  %s" % note)
                problems.extend(extra)
                checked_bundle = True
            else:
                print("  note: no bundle at %s — skipping the cross-check" % bundle)
        if problems:
            print("\nFAILED:")
            for p in problems:
                print("  * %s" % p)
            return 1
        print("\nOK: every dependency is satisfied and nothing in the base "
              "install changes version.")
        if checked_bundle:
            print("OK: the bundle install resolves on top of it, with no version "
                  "collisions and no conflicts.")
        return 0

    print("reading archive indices (cache: %s)" % args.cache)
    index, provides = fetch_indices(args.cache, args.arch)
    print("archive: %d binary packages" % len(index))

    selected, upgrades, problems = resolve(targets, installed, index, provides)

    already = sorted(n for n in selected if n in installed and n not in upgrades)
    fresh = sorted(n for n in selected if n not in installed)

    print("\nresolved %s -> %d packages" % (", ".join(targets), len(selected)))
    print("\n  new on the target (%d):" % len(fresh))
    for n in fresh:
        print("    %-32s %s" % (n, selected[n]["Version"]))
    if already:
        print("\n  already installed at the same version (%d) — dropped, a reinstall"
              "\n  only re-runs maintainer scripts on a live system:" % len(already))
        for n in already:
            print("    %-32s %s" % (n, installed[n]))
    if upgrades:
        print("\n  UPGRADES the base install (%d):" % len(upgrades))
        for n, (old, new) in sorted(upgrades.items()):
            print("    %-32s %s -> %s" % (n, old, new))

    missing = check_lockstep(selected, upgrades, installed, index)
    for src, siblings in sorted(missing.items()):
        problems.append("partial upgrade of source '%s': %s would move while "
                        "installed sibling(s) %s stay behind"
                        % (src, ", ".join(sorted(n for n in upgrades
                                                 if source_of(index[n]) == src)),
                           ", ".join(siblings)))

    if problems:
        print("\nFAILED — not writing a set:")
        for p in problems:
            print("  * %s" % p)
        return 1

    print("\nno partial upgrades, no unresolved dependencies.")

    # Same-version reinstalls are dropped: they add nothing and each one re-runs
    # a maintainer script on a booting system. adduser and dbus were in the set
    # that broke, and dbus's postinst is why the failure was so hard to read.
    ship = sorted(fresh + sorted(upgrades))
    print("shipping %d debs (%d same-version reinstalls dropped)"
          % (len(ship), len(already)))

    if args.check_only or not args.out:
        return 0

    os.makedirs(args.out, exist_ok=True)
    for name in ship:
        pkg = selected[name]
        url = "%s/%s" % (MIRROR, pkg["Filename"])
        dest = os.path.join(args.out, os.path.basename(pkg["Filename"]))
        if os.path.exists(dest):
            continue
        sys.stderr.write("  downloading %s\n" % os.path.basename(dest))
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    print("wrote %d debs to %s" % (len(ship), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
