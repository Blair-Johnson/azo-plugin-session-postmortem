"""Common-scope source plugin, safe when loaded through an installer symlink."""

import hashlib
import importlib
import importlib.util
from pathlib import Path
import sys


def _native_module():
    package_root = Path(__file__).resolve().parents[1] / "azo_session_postmortem"
    # Bind this repository's package explicitly: do not import a different
    # installed version, depend on cwd, or mix environments through sys.path.
    digest = hashlib.sha256(str(package_root).encode("utf-8")).hexdigest()[:16]
    package_name = "_azo_session_postmortem_" + digest
    if package_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package_name, package_root / "__init__.py",
            submodule_search_locations=[str(package_root)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load session-postmortem package at {package_root}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        try:
            spec.loader.exec_module(package)
        except BaseException:
            sys.modules.pop(package_name, None)
            raise
    return importlib.import_module(package_name + ".native")


def register_features(builder, *, config):
    """Load the sibling package and register exactly one native feature."""
    _native_module().register_features(builder, config=config)
