"""Reviewed v3 package byte verification shared by Mac and native endpoints."""
from pathlib import Path
from .package import package_identity


def verify_package(package_root=None):
    return package_identity(Path(package_root) if package_root is not None else Path(__file__).resolve().parents[1])
