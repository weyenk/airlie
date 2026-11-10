"""Shared test helpers, importable across layers (unit, functional, contract, integration).

A helper that more than one test module needs lives here, not in a test module; a test module
must never import from another test module. Each submodule groups helpers by the subject they
build fakes or fixtures for.
"""
