"""Compatibility shim for build frontends that still invoke setup.py.

All package metadata, dependencies, extras, and discovery rules live in
pyproject.toml so there is one authoritative packaging contract.
"""

from setuptools import setup


setup()
