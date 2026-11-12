"""Post-build hooks called by `ucc-gen build` (runs after the libraries are installed)."""

import os
import shutil

BINARY_SUFFIXES = (".so", ".dylib", ".pyd", ".dll", ".exe")
REMOVE_PATHS = ("local", "lib/bin", "metadata/local.meta")
SOURCE_ROOT = os.path.dirname(os.path.abspath(__file__))


def _install_documentation(ta_root: str) -> None:
    """UCC regenerates the nav and does not know about the documentation view."""
    src = os.path.join(SOURCE_ROOT, "package", "default", "data", "ui", "views", "documentation.xml")
    dst_dir = os.path.join(ta_root, "default", "data", "ui", "views")
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dst_dir, "documentation.xml"))

    nav_path = os.path.join(ta_root, "default", "data", "ui", "nav", "default.xml")
    with open(nav_path, encoding="utf-8") as handle:
        nav = handle.read()
    if 'name="documentation"' in nav:
        return
    needle = '<view name="configuration" default="true"/>'
    if needle not in nav:
        raise RuntimeError("UCC nav has no configuration entry to attach Documentation to")
    nav = nav.replace(needle, needle + '\n    <view name="documentation"/>', 1)
    with open(nav_path, "w", encoding="utf-8") as handle:
        handle.write(nav)


def cleanup_output_files(output_path: str, ta_name: str) -> None:
    ta_root = os.path.join(output_path, ta_name)

    for rel in REMOVE_PATHS:
        path = os.path.join(ta_root, rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    _install_documentation(ta_root)
    license_src = os.path.join(SOURCE_ROOT, "LICENSE")
    if os.path.isfile(license_src):
        shutil.copy(license_src, os.path.join(ta_root, "LICENSE"))
    # App icons stay in static/. The documentation image must be web-accessible.
    logo_name = "libidibia_coriaria.png"
    logo_src = os.path.join(ta_root, "static", logo_name)
    logo_dir = os.path.join(ta_root, "appserver", "static")
    os.makedirs(logo_dir, exist_ok=True)
    shutil.move(logo_src, os.path.join(logo_dir, logo_name))

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
