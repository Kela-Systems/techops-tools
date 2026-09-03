"""Per-generation config profiles, and making an export safe to commit.

Two things that only became necessary when the second camera generation arrived:

  * one profile key now names one file PER GENERATION, because the two export
    incompatible formats;
  * an export has to be sanitized before it is committed, because a newer
    camera's export carries the station password in plaintext and both
    generations' exports carry the reference camera's own address.

The second is the one with teeth. The two failures it prevents are both
expensive to discover late: a profile carrying an address moves the camera in
the middle of a run and the run loses it, and a profile carrying the station
password puts it in git history, where it stays.
"""
import json

import pytest

from raythink_base import (
    GEN_REST,
    GEN_RPC2,
    CameraError,
    find_plaintext_passwords,
    sanitize_profile,
)
import raythink_configure as mod

SHARED = "Kelafield123!"

# A trimmed copy of a real export from a newer camera, keeping the shapes that
# matter: the address, the plaintext ONVIF credential, a legitimate password
# that is NOT ours to remove, and some ordinary settings.
V2_EXPORT = {
    "NetworkInfo": {"DefaultInterface": "eth0",
                    "Card": [{"Name": "eth0", "DHCPEnable": True,
                              "IPAddress": "192.168.88.139",
                              "SubnetMask": "255.255.255.0"}]},
    "OnvifUser": {"User": [{"Name": "admin", "Group": 1, "Password": SHARED}]},
    "Gb28281Cfg": {"Enable": False, "Password": "12345678"},
    "NtpInfo": {"Address": "192.168.88.10", "Enable": True},
    "WebInfo": {"Port": 80},
}


# ── resolving a profile for the camera on the bench ──────────────────────────

def settings(profiles):
    return {"profiles": profiles}


def test_a_profile_resolves_to_its_own_generations_file(tmp_path):
    for name in ("old.json", "new.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    s = settings({"lan": {GEN_RPC2: str(tmp_path / "old.json"),
                          GEN_REST: str(tmp_path / "new.json")}})
    assert mod.resolve_profile(s, "lan", GEN_RPC2).name == "old.json"
    assert mod.resolve_profile(s, "lan", GEN_REST).name == "new.json"


def test_a_plain_string_still_means_the_older_generation(tmp_path):
    # A bench that has not met a new camera yet needs no config change.
    (tmp_path / "lan.json").write_text("{}", encoding="utf-8")
    s = settings({"lan": str(tmp_path / "lan.json")})
    assert mod.resolve_profile(s, "lan", GEN_RPC2).name == "lan.json"


def test_a_missing_generation_says_which_one_is_missing(tmp_path):
    # The operator has a camera on the bench and a profile that cannot serve it;
    # "profile not found" would send them looking for the wrong thing.
    (tmp_path / "lan.json").write_text("{}", encoding="utf-8")
    s = settings({"lan": str(tmp_path / "lan.json")})
    with pytest.raises(CameraError) as e:
        mod.resolve_profile(s, "lan", GEN_REST)
    assert "newer" in str(e.value)
    assert "--sanitize-profile" in str(e.value)


def test_a_configured_but_missing_file_is_a_different_error(tmp_path):
    s = settings({"lan": {GEN_REST: str(tmp_path / "nope.json")}})
    with pytest.raises(CameraError) as e:
        mod.resolve_profile(s, "lan", GEN_REST)
    assert "not found" in str(e.value)


def test_an_unknown_profile_lists_the_known_ones():
    with pytest.raises(CameraError) as e:
        mod.resolve_profile(settings({"lan": "x", "cellular": "y"}), "gotcha")
    assert "lan, cellular" in str(e.value)


def test_the_ui_can_ask_which_generations_a_profile_serves(tmp_path):
    # So a profile that cannot serve the camera on the bench is marked up front
    # rather than failing three steps into a run.
    s = settings({"lan": {GEN_RPC2: "a.json", GEN_REST: "b.json"},
                  "cellular": "c.json",
                  "empty": ""})
    assert mod.profile_generations(s, "lan") == [GEN_RPC2, GEN_REST]
    assert mod.profile_generations(s, "cellular") == [GEN_RPC2]
    assert mod.profile_generations(s, "empty") == []


# ── sanitizing an export ─────────────────────────────────────────────────────

def test_the_reference_cameras_address_is_dropped():
    cleaned, notes = sanitize_profile(V2_EXPORT, secrets=(SHARED,))
    assert "NetworkInfo" not in cleaned
    assert any("NetworkInfo" in n for n in notes)


def test_the_plaintext_onvif_credential_is_dropped():
    cleaned, _ = sanitize_profile(V2_EXPORT, secrets=(SHARED,))
    assert "OnvifUser" not in cleaned
    assert SHARED not in json.dumps(cleaned)


def test_the_station_password_is_blanked_wherever_else_it_appears():
    # Matched on the VALUE, not the key name: a secret is a secret wherever the
    # vendor decided to put it, and the newer export has 125 sections.
    export = {"Weird": {"SomeField": SHARED, "Nested": [{"x": SHARED}]}}
    cleaned, notes = sanitize_profile(export, secrets=(SHARED,))
    assert cleaned == {"Weird": {"SomeField": "", "Nested": [{"x": ""}]}}
    assert any("station password" in n for n in notes)


def test_a_legitimate_password_is_left_alone():
    # The narrowness is the point. A GB28181 SIP password may be exactly what
    # the site wants, and a sanitiser that blanked every password-looking field
    # would silently change what the bench ships.
    cleaned, _ = sanitize_profile(V2_EXPORT, secrets=(SHARED,))
    assert cleaned["Gb28281Cfg"]["Password"] == "12345678"


def test_everything_else_survives_untouched():
    cleaned, _ = sanitize_profile(V2_EXPORT, secrets=(SHARED,))
    assert cleaned["NtpInfo"] == V2_EXPORT["NtpInfo"]
    assert cleaned["WebInfo"] == V2_EXPORT["WebInfo"]


def test_the_committed_legacy_profiles_pass_through_unchanged():
    # The three profiles this bench already ships carry factory placeholders
    # ("none", "admin") that are NOT secrets. Sanitising must be a no-op on
    # them, or every existing legacy run starts configuring cameras differently.
    for name in ("lan", "cellular", "gotcha"):
        path = mod.CONFIG_DIR / "profiles" / f"{name}.json"
        if not path.is_file():
            pytest.skip(f"{name} profile is not present in this checkout")
        data = json.loads(path.read_text(encoding="utf-8"))
        cleaned, notes = sanitize_profile(data, secrets=(SHARED,))
        assert cleaned == data, name
        assert notes == [], name


def test_the_committed_profiles_do_not_carry_the_station_password():
    # The guard that actually keeps a secret out of git: enforced here rather
    # than at runtime, because by the time a run imports a profile the commit
    # has already happened.
    for path in (mod.CONFIG_DIR / "profiles").rglob("*.json"):
        assert SHARED not in path.read_text(encoding="utf-8"), path


def test_remaining_password_fields_are_reported_for_review():
    # Reported, never acted on — the call is a person's, but it has to be an
    # informed one, which it is not if nothing mentions the field exists.
    cleaned, _ = sanitize_profile(V2_EXPORT, secrets=(SHARED,))
    assert find_plaintext_passwords(cleaned) == [("Gb28281Cfg.Password", "12345678")]


def test_an_empty_password_field_is_not_reported():
    assert find_plaintext_passwords({"A": {"Password": ""}}) == []


# ── the CLI mode ─────────────────────────────────────────────────────────────

def test_the_cli_writes_a_committable_profile(tmp_path, capsys):
    src = tmp_path / "export.json"
    src.write_text(json.dumps(V2_EXPORT), encoding="utf-8")
    dest = tmp_path / "out" / "lan.json"

    assert mod.sanitize_profile_file(src, dest, {"new_password": SHARED}) == 0

    written = json.loads(dest.read_text(encoding="utf-8"))
    assert "NetworkInfo" not in written and "OnvifUser" not in written
    assert SHARED not in dest.read_text(encoding="utf-8")

    out = capsys.readouterr().out
    assert "dropped 'NetworkInfo'" in out
    assert "Gb28281Cfg.Password" in out      # named for review, not removed


def test_the_cli_refuses_something_that_is_not_a_profile(tmp_path, capsys):
    src = tmp_path / "export.json"
    src.write_text("[1, 2, 3]", encoding="utf-8")
    assert mod.sanitize_profile_file(src, tmp_path / "out.json",
                                     {"new_password": SHARED}) == 1
