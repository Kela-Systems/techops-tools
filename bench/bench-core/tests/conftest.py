"""Shared fixtures for the bench-core suite.

Holds one thing at the moment: the shared bench password. It is no longer a
constant in `bench_core`, because the value that lived there shipped in every
*.example.json, was copied onto every station, and so became the live password
on deployed hardware while sitting in git. Tests supply a fake through the
environment, the same channel a CI run would use.
"""
import pytest


@pytest.fixture(autouse=True)
def _shared_bench_password(monkeypatch):
    monkeypatch.setenv("KELA_NEW_PASSWORD", "test-shared-pw")
