from gotcha_atp.access import creds, tailnet
from gotcha_atp.access.exec import parse_sections, run_local, run_sections, sections_script


def bash(script, timeout):
    return run_local(["bash", "-c", script], timeout=timeout)


def test_sections_round_trip_through_a_real_shell():
    out = run_sections(bash, {
        "a": "echo hello",
        "noeol": "printf 'no newline'",
        "fails": "echo oops >&2; exit 3",
        "multi": "printf 'x\\ny\\n'",
    })
    assert out["a"].text == "hello" and out["a"].rc == 0
    assert out["noeol"].text == "no newline"
    assert out["fails"].rc == 3 and out["fails"].text == "oops"
    assert out["multi"].text.splitlines() == ["x", "y"]


def test_missing_section_reports_transport_error():
    out = run_sections(lambda s, t: run_local(["bash", "-c", "exit 255"], timeout=t), {"a": "true"})
    assert out["a"].rc == 255


def test_parse_sections_ignores_noise():
    text = "banner\n" + bash(sections_script({"k": "echo v"}), 5).out + "trailer\n"
    assert parse_sections(text)["k"].text == "v"


def test_redactor_masks_secret_and_its_md5():
    r = creds.Redactor(["Hunter2!"])
    assert r("pw=Hunter2! sent") == f"pw={creds.MASK} sent"
    assert r("md5 " + __import__("hashlib").md5(b"Hunter2!").hexdigest()) == f"md5 {creds.MASK}"
    assert r.scrub({"a": ["x Hunter2!"], "b": 3}) == {"a": [f"x {creds.MASK}"], "b": 3}


def test_public_factory_defaults_are_not_masked():
    r = creds.Redactor(["password", "Hunter2!"])
    assert r("wrong kela password, Hunter2!") == f"wrong kela password, {creds.MASK}"


def test_example_config_has_no_password_and_loads(tmp_path):
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    c = creds.load(example)
    assert c.ssh_user == "kela" and c.ssh_password == ""
    assert not c.secrets()
    assert c.device("teltonika").username == "root"


def test_shared_device_password_fills_families(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[ssh]\npassword = "s3cret"\n[devices]\npassword = "shared"\n[devices.camera]\npassword = "cam"\n')
    c = creds.load(p)
    assert c.device("magos").password == "shared"
    assert c.device("camera").password == "cam"
    assert "s3cret" not in repr(c) and "shared" not in str(c.devices)
    assert c.public()["ssh_password_set"] is True and "s3cret" not in str(c.public())


STATUS = {
    "BackendState": "Running", "Self": {"Online": True},
    "Peer": {
        "a": {"DNSName": "kela-gotcha-07-operator.tail1.ts.net.", "Online": True, "TailscaleIPs": ["100.84.12.9", "fd7a::1"]},
        "b": {"DNSName": "kela-gotcha-07.tail1.ts.net.", "Online": True, "TailscaleIPs": ["100.84.12.10"]},
        "c": {"DNSName": "kela-gotcha-05-operator.tail1.ts.net.", "Online": False, "TailscaleIPs": ["100.84.12.11"]},
        "d": {"DNSName": "kela-gotcha-06-operator.tail1.ts.net.", "Online": False, "TailscaleIPs": []},
        "e": {"DNSName": "kela-gotcha-06.tail1.ts.net.", "Online": True, "TailscaleIPs": ["100.84.12.12"]},
    },
}


def test_units_from_tailnet():
    units = tailnet.units(STATUS)
    assert [u.site for u in units] == ["kela-gotcha-07", "kela-gotcha-05", "kela-gotcha-06"]
    assert units[0].operator.ip == "100.84.12.9"
    assert units[0].state == "server online · operator online"
    assert tailnet.unit(STATUS, "kela-gotcha-06-operator").state == "server online · operator offline"


REV3 = {
    "BackendState": "Running", "Self": {"Online": True},
    "Peer": {
        "s": {"DNSName": "gotcha-rev3-dev.t.ts.net.", "Online": True, "TailscaleIPs": ["100.108.157.43"]},
        "o": {"DNSName": "gotcha-rev3-dev-dell.t.ts.net.", "Online": True, "TailscaleIPs": ["100.113.128.32"]},
        "p": {"DNSName": "gotcha-rev3-prototype.t.ts.net.", "Online": False, "TailscaleIPs": ["100.80.204.22"]},
        "q": {"DNSName": "gotcha-rev3-prototype-operator.t.ts.net.", "Online": False, "TailscaleIPs": ["100.68.206.121"]},
        "r": {"DNSName": "rut-gotcha-rev3-prototype.t.ts.net.", "Online": False, "TailscaleIPs": ["100.1.1.1"]},
    },
}


def test_operator_is_never_guessed_from_peer_names():
    # gotcha-rev3-dev-dell sits next to the server but is NOT its operator.
    assert [u.site for u in tailnet.units(REV3)] == ["gotcha-rev3-prototype"]
    from gotcha_atp.stages.s0 import unit_for
    peers = tailnet.unit(REV3, "gotcha-rev3-dev")
    assert peers.operator is None and peers.state == "server online · no operator peer"
    u = unit_for(peers)
    assert u.operator_peer is False and u.server_addr == "100.108.157.43"


def test_explicit_operator():
    from gotcha_atp.stages.s0 import unit_for
    peers = tailnet.unit(REV3, "gotcha-rev3-dev", "gotcha-rev3-dev-dell")
    assert peers.nonstandard_operator
    u = unit_for(peers)
    assert u.operator_host == "gotcha-rev3-dev-dell" and u.operator_peer
    assert u.standard_operator_host == "gotcha-rev3-dev-operator"
    assert tailnet.unit(REV3, "gotcha-rev3-dev", "nope").operator is None


def _fake_session(monkeypatch, outcomes):
    """A Session whose ssh masters succeed/fail per destination, without ssh."""
    from gotcha_atp.access import session as sm
    calls = []

    def master(self, ctl, dest, who, extra):
        calls.append((dest, extra))
        err = outcomes.get(dest)
        if err:
            raise err
    monkeypatch.setattr(sm.Session, "_master", master)
    monkeypatch.setattr(sm.Session, "_rtt_ms", lambda self: 12.0)
    monkeypatch.setattr(sm.shutil, "which", lambda name: "/usr/bin/ssh")
    return calls


def test_server_first_over_the_tailnet(monkeypatch):
    from gotcha_atp.access import session as sm
    calls = _fake_session(monkeypatch, {})
    s = sm.Session(sm.Unit("kela-gotcha-07", operator_addr="100.1.1.1", server_addr="100.1.1.2"))
    info = s.open()
    assert info["server_path"] == "tailnet" and info["operator_ok"]
    assert calls[0][0] == "kela@100.1.1.2" and "-D" in calls[0][1]      # server carries SOCKS
    assert calls[1][0] == "kela@100.1.1.1" and "-D" not in calls[1][1]  # operator: own checks only
    assert "kela@100.1.1.2" in s.proxy_command("192.168.88.1")
    assert s._target("server")[1] == "kela@100.1.1.2"
    s.close()


def test_backup_through_the_operator(monkeypatch):
    from gotcha_atp.access import session as sm
    calls = _fake_session(monkeypatch, {"kela@100.1.1.2": sm.LoginRefused("refused")})
    s = sm.Session(sm.Unit("kela-gotcha-07", operator_addr="100.1.1.1", server_addr="100.1.1.2"))
    info = s.open()
    assert info["server_path"] == "via-operator" and info["server_note"] == "refused"
    assert "-D" in calls[1][1]                                            # operator carries SOCKS
    assert calls[2][0] == "kela@192.168.88.10"
    assert "kela@100.1.1.1" in s.proxy_command("192.168.88.1")
    s.close()


def test_no_operator_peer_still_reaches_the_server(monkeypatch):
    from gotcha_atp.access import session as sm
    import pytest
    calls = _fake_session(monkeypatch, {})
    s = sm.Session(sm.Unit("gotcha-rev3-dev", server_addr="100.1.1.2", operator_peer=False))
    info = s.open()
    assert info["server_path"] == "tailnet" and not info["operator_ok"] and info["operator"] is None
    assert len(calls) == 1
    with pytest.raises(sm.OperatorUnavailable):
        s.exec("operator", "true")
    s.close()


def test_server_unreachable_both_ways(monkeypatch):
    from gotcha_atp.access import session as sm
    import pytest
    _fake_session(monkeypatch, {"kela@100.1.1.2": sm.AccessError("timed out"),
                                "kela@100.1.1.1": sm.AccessError("operator timed out")})
    s = sm.Session(sm.Unit("kela-gotcha-07", operator_addr="100.1.1.1", server_addr="100.1.1.2"))
    with pytest.raises(sm.AccessError, match="timed out; the backup route"):
        s.open()
    s.close()
