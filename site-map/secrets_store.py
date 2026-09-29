#!/usr/bin/env python3
"""Get a password without it ever being typed into this repo, a shell history,
a process argument or a config file.

The wrong ways, in order of how wrong:

    a field on the web page      crosses the network on every survey, sits in
                                 browser memory, and the router password
                                 opens every router in the fleet
    --password on the CLI        visible to every user on the box in `ps`,
                                 and recorded in shell history
    an env var set inline        same shell history problem; readable in
                                 /proc/<pid>/environ for the owner
    a file in the repo           one `git add -A` from being published

The right way is to name a *command* that produces the secret, and run it at
the moment it is needed. The secret then lives wherever it already lives - a
keychain, a password manager, a vault - and this process holds it for the
length of one survey. Nothing that writes it down is involved, so nothing has
to be cleaned up afterwards.

    # Nothing set up yet? It asks, and nothing records what you type.
    sitemap.py serve
    #   shared Teltonika/router password (not echoed): ...

    # 1Password (installed here)
    sitemap.py serve --password-cmd 'op read "op://TechOps/Teltonika/password"'

    # macOS Keychain, once per machine:
    security add-generic-password -a "$USER" -s kela-router -w
    sitemap.py serve --password-cmd 'security find-generic-password -s kela-router -w'

An env var is still honoured as a fallback, because a systemd unit with an
`EnvironmentFile` that root owns is a reasonable place for this, and it is
better than a flag. The command is preferred where both are set.
"""
from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import sys

# What it is, and where to look for it.
ROUTER = ("KELA_BENCH_PASSWORD", "the shared Teltonika/router password")
HOST = ("KELA_HOST_PASSWORD", "the operator-station password")


class SecretError(Exception):
    pass


def prompt_for(what: str) -> str | None:
    """Ask at the terminal. The best option when there is no vault yet.

    Nothing records it: not the shell history, not `ps`, not a file. Returns
    None when there is no terminal to ask at, so a systemd unit fails on the
    missing secret rather than blocking forever on a prompt no one can see.
    """
    if not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(f"{what} (not echoed): ").strip() or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def from_command(command: str, *, timeout: float = 60.0, runner=None) -> str:
    """Run `command` and take its first line of stdout as the secret.

    First line only: `op read` and `security -w` both emit a trailing
    newline, and a stray one silently becomes part of the password - which
    then fails authentication with no clue as to why.

    Run through a shell, because the useful invocations are quoted
    (`op read "op://..."`) and asking the caller to pre-split them would be a
    worse interface than the shell they already know.
    """
    if not command.strip():
        raise SecretError("empty --password-cmd")
    try:
        done = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout,
        ) if runner is None else runner(command)
    except subprocess.TimeoutExpired:
        raise SecretError(
            f"the password command did not finish in {timeout:.0f}s. If it "
            f"prompts for a touch or a master password, run it once by hand "
            f"first so the agent is unlocked."
        )
    except OSError as exc:
        raise SecretError(f"cannot run the password command: {exc}")

    if done.returncode != 0:
        # The command's own stderr is the useful part - "item not found",
        # "not signed in" - so it is passed through rather than summarised.
        detail = (done.stderr or "").strip().splitlines()
        raise SecretError(
            f"the password command failed: "
            + (detail[-1] if detail else f"exit {done.returncode}")
        )

    secret = (done.stdout or "").splitlines()
    value = secret[0].strip() if secret else ""
    if not value:
        raise SecretError("the password command produced nothing on stdout")
    return value


def resolve(which: tuple, command: str | None = None, *,
            literal: str | None = None, prompt: bool = False,
            runner=None) -> tuple:
    """(secret or None, how it was obtained). Never raises for a missing one.

    A missing password is not an error here: `lint` and the diagram work
    without one, and the survey that needs it says so at the point it needs
    it.
    """
    env_name, what = which
    if command:
        return from_command(command, runner=runner), f"--password-cmd ({what})"
    if literal:
        # Supported, but say plainly that it was the weak route.
        return literal, (
            f"a --password flag ({what}), which is visible in `ps` to every "
            f"user on this machine - prefer --password-cmd"
        )
    value = os.environ.get(env_name)
    if value:
        return value, f"${env_name} ({what})"
    if prompt:
        typed = prompt_for(what)
        if typed:
            return typed, f"typed at the prompt ({what})"
    return None, f"not set ({what})"


def describe(command: str | None, literal: str | None, env_name: str,
             typed: bool = False) -> str:
    """One line for the startup banner. Never prints any part of a secret."""
    if command:
        return f"from `{shlex.split(command)[0]}` on demand"
    if literal:
        return "from a --password flag (visible in `ps`; prefer a prompt)"
    if os.environ.get(env_name):
        return f"from ${env_name}"
    if typed:
        return "typed at the prompt"
    return "not configured"
