"""The sweep: what it refuses to do, and what it makes of what comes back."""
import re
import subprocess

import pytest

import sweep

NEIGH = """\
### hostname
kela-fob-03
### neighbours
192.168.88.1 dev enp1 lladdr 20:97:27:36:55:ec REACHABLE
192.168.88.130 dev enp1 lladdr 8c:1f:64:e7:4c:46 STALE
192.168.88.44 dev enp1  FAILED
192.168.88.45 dev enp1 lladdr 00:00:00:00:00:00 INCOMPLETE
"""


def runner_returning(stdout, returncode=0, stderr=""):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    run.calls = calls
    return run


# -- the two fields ------------------------------------------------------

def test_the_server_field_has_no_default_and_says_why():
    with pytest.raises(sweep.SweepError) as exc:
        sweep.run("", "192.168.88.0/24")
    assert "no default" in str(exc.value)


def test_a_lan_address_in_the_server_field_is_a_warning_not_a_refusal():
    # Legitimate setups exist (a jump host, a MagicDNS name resolving oddly),
    # so this is said loudly rather than blocked.
    _, _, warnings = sweep.check_server("kela@192.168.88.10")
    assert any("bench" in w for w in warnings)


def test_a_tailnet_address_with_a_user_passes_without_comment():
    assert sweep.check_server("kela@100.101.102.103") == (
        "kela@100.101.102.103", "100.101.102.103", [])


def test_a_magicdns_name_is_accepted():
    target, host, _ = sweep.check_server("kela@kela-fob-03.tail1234.ts.net")
    assert target == "kela@kela-fob-03.tail1234.ts.net"
    assert host.endswith("ts.net")


# -- the username, which is the whole of the Tailscale SSH story ---------

def test_the_field_takes_user_at_host():
    assert sweep.check_server("kela@100.1.1.1")[0] == "kela@100.1.1.1"


def test_a_default_user_is_applied_when_the_field_omits_one():
    assert sweep.check_server("100.1.1.1", "kela")[0] == "kela@100.1.1.1"


def test_the_field_wins_over_the_default():
    assert sweep.check_server("root@100.1.1.1", "kela")[0] == "root@100.1.1.1"
    assert sweep.check_server("root@100.1.1.1")[0] == "root@100.1.1.1"


def test_the_default_user_is_the_one_the_tailnet_policy_permits():
    # ssh's own default is the local login name, and the policy refuses it.
    # A default that works beats a default that is technically neutral.
    assert sweep.DEFAULT_SSH_USER == "kela"
    assert sweep.check_server("100.1.1.1")[0] == "kela@100.1.1.1"
    run = runner_returning(NEIGH)
    sweep.run("100.1.1.1", runner=run)
    assert run.calls[0][-2] == "kela@100.1.1.1"


def test_no_user_anywhere_is_warned_about_before_the_attempt_fails():
    # `ssh <ip>` asks to log in as whatever you are called locally, and a
    # tailnet SSH policy refuses that - which closes the connection and looks
    # nothing like an auth problem.
    target, _, warnings = sweep.check_server("100.1.1.1", None)
    assert target == "100.1.1.1"
    assert any("--ssh-user" in w for w in warnings)


@pytest.mark.parametrize("bad", ["a b@host", "-oProxyCommand=x@host", "a;b@host"])
def test_an_unusable_username_is_refused(bad):
    with pytest.raises(sweep.SweepError):
        sweep.check_server(bad)


def test_the_user_reaches_the_ssh_command_line():
    run = runner_returning(NEIGH)
    sweep.run("100.1.1.1", user="kela", runner=run)
    assert run.calls[0][-2] == "kela@100.1.1.1"


def test_a_tailnet_policy_refusal_names_the_username_and_not_a_key():
    # The failure this replaced blamed BatchMode and keys for every non-zero
    # exit, which sent the reader to check a key that was working fine.
    stderr = ('tailscale: tailnet policy does not permit you to SSH as user '
              '"eyal.meridan"\nConnection closed by 100.1.1.1 port 22\n')
    run = runner_returning("", returncode=255, stderr=stderr)
    with pytest.raises(sweep.SweepError) as exc:
        sweep.run("100.1.1.1", runner=run)
    message = str(exc.value)
    assert "username" in message and "eyal.meridan" in message
    assert "--ssh-user" in message
    assert "BatchMode" not in message, "that was the misleading part"


def test_an_unknown_remote_account_is_reported_as_the_account():
    run = runner_returning(
        "", returncode=255,
        stderr='tailscale: failed to look up local user "KelaAdmin"\n')
    with pytest.raises(sweep.SweepError) as exc:
        sweep.run("KelaAdmin@100.1.1.1", runner=run)
    assert "username" in str(exc.value)


@pytest.mark.parametrize("bad", ["a; rm -rf /", "$(whoami)", "a b", "a|b",
                                 "kela@a;b", "kela@$(id)"])
def test_shell_metacharacters_in_the_server_field_are_refused(bad):
    with pytest.raises(sweep.SweepError):
        sweep.check_server(bad)


def test_the_subnet_defaults_to_the_one_nearly_every_site_uses():
    run = runner_returning(NEIGH)
    assert sweep.run("100.1.1.1", runner=run).subnet == "192.168.88.0/24"


def test_a_different_subnet_is_swept_as_given():
    run = runner_returning(NEIGH)
    result = sweep.run("100.1.1.1", "10.20.30.0/26", runner=run)
    assert result.subnet == "10.20.30.0/26"
    assert result.swept == 62


def test_a_fat_fingered_prefix_is_refused_rather_than_queued():
    with pytest.raises(sweep.SweepError) as exc:
        sweep.hosts("10.0.0.0/8")
    assert "cap" in str(exc.value)


def test_a_host_route_still_sweeps_the_one_address():
    addrs, _ = sweep.hosts("192.168.88.10/32")
    assert addrs == ["192.168.88.10"]


@pytest.mark.parametrize("bad", ["", "192.168.88.0", "not a subnet", "192.168.88.0/33"])
def test_nonsense_subnets_are_refused(bad):
    if bad == "192.168.88.0":
        # A bare address is a valid /32 and sweeping just it is a sane thing
        # to ask for, so this one is allowed through.
        assert sweep.hosts(bad)[0] == ["192.168.88.0"]
        return
    with pytest.raises(sweep.SweepError):
        sweep.hosts(bad)


# -- what the remote end is asked to do ----------------------------------

def test_the_remote_script_only_pings_and_reads():
    script = sweep.remote_script(["192.168.88.1", "192.168.88.2"])
    assert "ping -n -c1 -W1" in script
    assert "ip neigh show" in script
    for write in ("ip neigh add", "ip addr", "arp -s", "tee", "sudo", "rm "):
        assert write not in script
    # Every redirection discards output. None of them names a file, so the
    # script cannot leave anything behind on a site server.
    assert set(re.findall(r"\d?>&?\S+", script)) <= {
        ">/dev/null", "2>/dev/null", "2>&1"}


def test_the_script_refuses_anything_that_is_not_an_address():
    # The addresses come from ipaddress.hosts() today. This is the guard for
    # the day someone wires a form field in directly.
    with pytest.raises(sweep.SweepError):
        sweep.remote_script(["192.168.88.1; curl evil"])


def test_ssh_runs_in_batch_mode():
    # A passphrase prompt behind an HTTP request is indistinguishable from a
    # hang, so it must fail instead of waiting.
    run = runner_returning(NEIGH)
    sweep.run("100.1.1.1", runner=run)
    command = run.calls[0]
    assert command[0] == "ssh"
    assert "BatchMode=yes" in command
    assert command[-2] == "kela@100.1.1.1"


# -- reading the answer --------------------------------------------------

def test_ip_neigh_becomes_arp_output_the_existing_parser_reads():
    import oui
    _, arp_text = sweep.parse_output(NEIGH)
    entries = oui.parse_arp(arp_text)
    assert [e.ip for e in entries] == ["192.168.88.1", "192.168.88.130"]
    assert entries[0].mac == "20:97:27:36:55:ec"


def test_unresolved_neighbours_are_dropped_not_reported_as_devices():
    # FAILED is the kernel saying it asked and got nothing. Keeping it would
    # turn "did not answer" into a device in the site model.
    _, arp_text = sweep.parse_output(NEIGH)
    assert "192.168.88.44" not in arp_text
    assert "192.168.88.45" not in arp_text


def test_arp_output_is_passed_through_for_a_host_without_iproute2():
    text = "### neighbours\n? (192.168.88.7) at aa:bb:cc:dd:ee:ff [ether] on en0\n"
    _, arp_text = sweep.parse_output(text)
    assert "192.168.88.7" in arp_text


def test_neighbours_outside_the_swept_subnet_are_not_site_devices():
    # A site server running k3s carries a neighbour per pod on cni0. Nothing
    # pinged them, they are not on the subnet that was asked about, and 37 of
    # them arriving as vendorless devices buried the 11 real ones.
    import ipaddress
    text = """\
### neighbours
10.11.10.101 dev enp1 lladdr fc:4c:ea:2b:7e:06 REACHABLE
10.42.0.245 dev cni0 lladdr 7a:89:60:9c:57:ac REACHABLE
10.42.0.248 dev cni0 lladdr 6e:9b:67:2f:d5:d0 REACHABLE
"""
    _, arp_text = sweep.parse_output(text, ipaddress.ip_network("10.11.10.0/24"))
    assert "10.11.10.101" in arp_text
    assert "10.42.0" not in arp_text


def test_the_filter_applies_to_arp_format_too():
    import ipaddress
    text = ("### neighbours\n"
            "? (192.168.88.7) at aa:bb:cc:dd:ee:ff [ether] on en0\n"
            "? (10.42.0.9) at aa:bb:cc:dd:ee:01 [ether] on cni0\n")
    _, arp_text = sweep.parse_output(text, ipaddress.ip_network("192.168.88.0/24"))
    assert "192.168.88.7" in arp_text and "10.42.0.9" not in arp_text


def test_a_sweep_reports_only_what_it_swept():
    run = runner_returning(NEIGH + "10.42.0.9 dev cni0 lladdr aa:bb:cc:dd:ee:01 REACHABLE\n")
    result = sweep.run("kela@100.1.1.1", "192.168.88.0/24", runner=run)
    assert result.answered == 2
    assert "10.42.0.9" not in result.arp_text


def test_the_remote_hostname_is_read_so_no_third_field_is_needed():
    assert sweep.parse_output(NEIGH)[0] == "kela-fob-03"


def test_an_empty_subnet_is_a_finding_and_not_an_error():
    run = runner_returning("### hostname\nh\n### neighbours\n")
    result = sweep.run("100.1.1.1", runner=run)
    assert result.answered == 0
    assert any("answered" in w for w in result.warnings)


def test_a_real_key_failure_still_mentions_batch_mode():
    # Where ssh actually says "Permission denied", the key advice is earned.
    run = runner_returning("", returncode=255, stderr="Permission denied (publickey).")
    with pytest.raises(sweep.SweepError) as exc:
        sweep.run("kela@100.1.1.1", runner=run)
    assert "Permission denied" in str(exc.value)
    assert "BatchMode" in str(exc.value)


def test_a_timeout_names_the_subnet_and_the_server():
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    with pytest.raises(sweep.SweepError) as exc:
        sweep.run("100.1.1.1", "192.168.88.0/24", runner=run, timeout=5)
    assert "192.168.88.0/24" in str(exc.value) and "100.1.1.1" in str(exc.value)


def test_no_ssh_at_all_is_reported_plainly():
    def run(command, **kwargs):
        raise FileNotFoundError("ssh")

    with pytest.raises(sweep.SweepError) as exc:
        sweep.run("100.1.1.1", runner=run)
    assert "ssh" in str(exc.value)
