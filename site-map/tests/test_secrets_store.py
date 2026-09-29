"""Getting a password without writing it down anywhere.

The point of the command form is that the secret stays in whatever already
holds it - 1Password, the macOS keychain, a vault - and this process holds it
for the length of one survey. Nothing that persists it is involved, so
nothing has to be cleaned up afterwards.
"""
import subprocess

import pytest

import secrets_store as secrets


def runner_for(stdout="", stderr="", returncode=0):
    def run(command):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    return run


def test_a_command_s_output_is_the_secret():
    assert secrets.from_command("x", runner=runner_for("hunter2\n")) == "hunter2"


def test_only_the_first_line_is_taken():
    # `op read` and `security -w` both emit a trailing newline, and a stray
    # one becomes part of the password - which then fails authentication with
    # no clue as to why.
    assert secrets.from_command(
        "x", runner=runner_for("hunter2\n\n")) == "hunter2"


def test_surrounding_whitespace_is_stripped():
    assert secrets.from_command("x", runner=runner_for("  hunter2  \n")) == "hunter2"


def test_a_failing_command_passes_its_own_stderr_through():
    # "item not found" and "not signed in" are the useful messages, and
    # summarising them would hide which one it was.
    with pytest.raises(secrets.SecretError) as exc:
        secrets.from_command(
            "op read x",
            runner=runner_for(stderr="[ERROR] could not find item", returncode=1))
    assert "could not find item" in str(exc.value)


def test_a_command_that_prints_nothing_is_an_error_not_an_empty_password():
    with pytest.raises(secrets.SecretError) as exc:
        secrets.from_command("true", runner=runner_for(""))
    assert "nothing on stdout" in str(exc.value)


def test_an_empty_command_is_refused():
    with pytest.raises(secrets.SecretError):
        secrets.from_command("   ")


def test_the_command_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("KELA_BENCH_PASSWORD", "from-env")
    value, how = secrets.resolve(secrets.ROUTER, "x",
                                 runner=runner_for("from-cmd\n"))
    assert value == "from-cmd" and "--password-cmd" in how


def test_the_environment_is_still_honoured(monkeypatch):
    # A systemd EnvironmentFile that root owns is a reasonable place for
    # this, and better than a flag.
    monkeypatch.setenv("KELA_BENCH_PASSWORD", "from-env")
    value, how = secrets.resolve(secrets.ROUTER)
    assert value == "from-env" and "KELA_BENCH_PASSWORD" in how


def test_a_literal_flag_works_but_says_it_is_the_weak_route(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    value, how = secrets.resolve(secrets.ROUTER, literal="hunter2")
    assert value == "hunter2"
    assert "visible in `ps`" in how


def test_a_missing_password_is_not_an_error(monkeypatch):
    # lint and the diagram work without one; the survey that needs it says so
    # at the point it needs it.
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    assert secrets.resolve(secrets.ROUTER)[0] is None


def test_the_router_and_host_secrets_are_separate(monkeypatch):
    monkeypatch.setenv("KELA_BENCH_PASSWORD", "router")
    monkeypatch.setenv("KELA_HOST_PASSWORD", "station")
    assert secrets.resolve(secrets.ROUTER)[0] == "router"
    assert secrets.resolve(secrets.HOST)[0] == "station"


# -- and nothing anywhere prints one -------------------------------------

def test_describe_names_the_tool_and_never_the_secret(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    line = secrets.describe('op read "op://TechOps/Teltonika/password"',
                            None, "KELA_BENCH_PASSWORD")
    assert "op" in line
    assert "password" not in line.replace("op://TechOps/Teltonika/password", "")


def test_describe_does_not_echo_a_literal_password(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    line = secrets.describe(None, "hunter2", "KELA_BENCH_PASSWORD")
    assert "hunter2" not in line
    assert "prefer a prompt" in line


def test_describe_says_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    assert secrets.describe(None, None, "KELA_BENCH_PASSWORD") == "not configured"


# -- the prompt, for when there is no vault set up yet -------------------

def test_a_prompt_is_used_when_nothing_else_supplies_it(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    monkeypatch.setattr(secrets.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(secrets.getpass, "getpass", lambda p: "typed-in\n")
    value, how = secrets.resolve(secrets.ROUTER, prompt=True)
    assert value == "typed-in"
    assert "prompt" in how


def test_the_prompt_is_the_last_resort(monkeypatch):
    # Anything configured wins, so a serve that already has its secret does
    # not stop to ask.
    monkeypatch.setenv("KELA_BENCH_PASSWORD", "from-env")
    monkeypatch.setattr(secrets.getpass, "getpass",
                        lambda p: pytest.fail("should not have prompted"))
    assert secrets.resolve(secrets.ROUTER, prompt=True)[0] == "from-env"


def test_no_terminal_means_no_prompt(monkeypatch):
    # A systemd unit must fail on the missing secret rather than block
    # forever on a prompt nobody can see.
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    monkeypatch.setattr(secrets.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(secrets.getpass, "getpass",
                        lambda p: pytest.fail("should not have prompted"))
    assert secrets.resolve(secrets.ROUTER, prompt=True)[0] is None


def test_an_empty_answer_is_not_a_password(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    monkeypatch.setattr(secrets.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(secrets.getpass, "getpass", lambda p: "   ")
    assert secrets.resolve(secrets.ROUTER, prompt=True)[0] is None


def test_ctrl_c_at_the_prompt_does_not_crash(monkeypatch):
    monkeypatch.setattr(secrets.sys.stdin, "isatty", lambda: True)
    def interrupted(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(secrets.getpass, "getpass", interrupted)
    assert secrets.prompt_for("x") is None


def test_the_banner_says_typed_without_saying_what(monkeypatch):
    monkeypatch.delenv("KELA_BENCH_PASSWORD", raising=False)
    assert secrets.describe(None, None, "KELA_BENCH_PASSWORD", typed=True) \
        == "typed at the prompt"
