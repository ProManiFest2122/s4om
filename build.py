# -*- coding: utf-8 -*-
"""Build the .ts4script mod package.

Compiles src/**/*.py to legacy-layout .pyc with Python 3.7 (The Sims 4 runs 3.7,
so the .pyc magic number must match) and zips them with forward-slash paths.

Usage:
    "C:/Program Files/Python37/python.exe" build.py            # build only
    "C:/Program Files/Python37/python.exe" build.py --deploy   # also copy to Mods
    "C:/Program Files/Python37/python.exe" build.py --relay host:port   # set the online server first
"""
import os
import py_compile
import shutil
import sys
import zipfile

NAME = "S4OM"
ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
BUILD = os.path.join(ROOT, "build")
OUT = os.path.join(BUILD, NAME + ".ts4script")
MODS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts", "The Sims 4", "Mods")


RELAY_DEFAULT_PY = os.path.join(SRC, "multimod", "relay_default.py")


def set_default_relay(address):
    """Bake the relay address into the build (rewrites the DEFAULT_RELAY line)."""
    host, _, port = address.partition(":")
    if not host or " " in address or (port and not port.isdigit()):
        sys.exit("--relay wants host or host:port, got {!r}".format(address))
    with open(RELAY_DEFAULT_PY, encoding="utf-8") as f:
        lines = f.read().split("\n")
    lines = ['DEFAULT_RELAY = "{}"'.format(address) if l.startswith("DEFAULT_RELAY") else l for l in lines]
    with open(RELAY_DEFAULT_PY, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print("default relay:", address)


def main():
    if sys.version_info[:2] != (3, 7):
        sys.exit("must build with Python 3.7 (got {}.{})".format(*sys.version_info[:2]))
    if "--relay" in sys.argv:
        i = sys.argv.index("--relay")
        if i + 1 >= len(sys.argv):
            sys.exit("--relay wants host or host:port")
        set_default_relay(sys.argv[i + 1])

    stage = os.path.join(BUILD, "pyc")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)

    count = 0
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, SRC)
            dst = os.path.join(stage, rel[:-3] + ".pyc")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            py_compile.compile(src, cfile=dst, doraise=True)
            count += 1

    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, filenames in os.walk(stage):
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.relpath(full, stage).replace(os.sep, "/"))

    print("compiled {} modules -> {} ({} bytes)".format(count, OUT, os.path.getsize(OUT)))

    if "--deploy" in sys.argv:
        if not os.path.isdir(MODS_DIR):
            sys.exit("Mods folder not found: " + MODS_DIR)
        shutil.copy2(OUT, os.path.join(MODS_DIR, NAME + ".ts4script"))
        print("deployed to", MODS_DIR)


if __name__ == "__main__":
    main()
