"""Post-build hooks called by `ucc-gen build` (runs after the libraries are installed)."""

import os
import shutil

BINARY_SUFFIXES = (".so", ".dylib", ".pyd", ".dll", ".exe")
REMOVE_PATHS = ("local", "lib/bin", "metadata/local.meta")


def cleanup_output_files(output_path: str, ta_name: str) -> None:
    ta_root = os.path.join(output_path, ta_name)

    for rel in REMOVE_PATHS:
        path = os.path.join(ta_root, rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    binaries = []
    for root, dirs, files in os.walk(ta_root):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"))
            dirs.remove("__pycache__")
        for name in files:
            if name.endswith(BINARY_SUFFIXES):
                binaries.append(os.path.relpath(os.path.join(root, name), ta_root))
    if binaries:
        raise RuntimeError(
            "Platform-specific binaries found in the add-on; exclude them in package/lib/exclude.txt "
            "or build the library without binaries: " + ", ".join(sorted(binaries))
        )
