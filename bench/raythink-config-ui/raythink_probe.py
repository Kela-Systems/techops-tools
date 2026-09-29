#!/usr/bin/env python3
"""
Ask a camera what it is and why a login was refused, changing nothing.

`configure` aborts on the first failed step, which is right for a run but leaves
an operator with one line and no way to tell a wrong password from an
unsupported firmware from a camera that is not the generation the probe thought.
This prints the whole conversation instead: both detection probes with their raw
replies, then a login attempt per candidate password with the device's own Code
and Detail for each.

It is read-only by construction — the only writes it could make are logins, and
the client is put in read-only mode besides.

    python3 raythink_probe.py                     # the factory address
    python3 raythink_probe.py --host 192.168.88.31

Passwords are never printed, in plaintext or encrypted: this API carries them in
the query string under a fixed, publicly-known key, so a ciphertext on screen or
in a scrollback is a password on screen.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent

import requests

from bench_core import load_settings, shared_new_password
from raythink_base import (
    DEFAULT_HOST,
    DEFAULT_INITIAL_PASSWORD,
    DEFAULT_SCHEME,
    DEFAULT_USERNAME,
    GENERATION_LABELS,
    GEN_REST,
    GEN_RPC2,
    CameraError,
)
from raythink_client import detect_generation, generation_from_firmware, open_camera


def show(label: str, value) -> None:
    print(f"  {label:<22}: {value}")


def probe_http(host: str, scheme: str, timeout: float) -> None:
    """The two detection probes, with whatever came back. Unauthenticated, so
    neither can contribute to the lockout the older cameras enforce."""
    print("\n--- detection probes (no credentials sent) ---")

    try:
        r = requests.post(f"{scheme}://{host}/v1/token", timeout=timeout, verify=False)
        body = r.text[:200]
        try:
            body = json.dumps(r.json())[:200]
        except ValueError:
            pass
        show("REST POST /v1/token", f"HTTP {r.status_code}  {body}")
    except requests.exceptions.RequestException as e:
        show("REST POST /v1/token", f"no answer ({type(e).__name__}: {e})")

    try:
        r = requests.post(
            f"{scheme}://{host}/RPC2_Login",
            data=json.dumps({"method": "global.login",
                             "params": {"userName": DEFAULT_USERNAME, "password": "",
                                        "clientType": "Web3.0", "loginType": "Direct"},
                             "id": 1, "session": 0}),
            headers={"Content-Type": "application/json"}, timeout=timeout, verify=False)
        body = r.text[:200]
        try:
            body = json.dumps(r.json())[:200]
        except ValueError:
            pass
        show("RPC2 /RPC2_Login", f"HTTP {r.status_code}  {body}")
    except requests.exceptions.RequestException as e:
        show("RPC2 /RPC2_Login", f"no answer ({type(e).__name__}: {e})")


def try_logins(host: str, scheme: str, generation: str, candidates: list[tuple[str, str]],
               timeout: int, settings: dict) -> str:
    """Each candidate password in turn, reporting the device's own reason for
    every refusal. Returns the label that worked, or ""."""
    print("\n--- login attempts ---")
    worked = ""
    for label, password in candidates:
        if not password:
            continue
        client = open_camera(host, scheme=scheme, generation=generation, timeout=timeout)
        client.set_read_only()
        try:
            client.login(password)
            print(f"  {label:<22}: OK")
            worked = worked or label
            try:
                identity = client.get_identity()
                print("\n--- identity ---")
                for key in ("serial", "model", "firmware", "mac"):
                    show(key, identity.get(key, "?"))
                claimed = generation_from_firmware(identity.get("firmware", ""))
                show("firmware says", GENERATION_LABELS.get(claimed,
                                                            "neither convention"))
                if claimed and claimed != generation:
                    print("  !! the firmware convention and the API that answered "
                          "DISAGREE — raythink_client needs updating")
            except CameraError as e:
                show("identity", f"could not be read: {e}")
            if generation == GEN_REST:
                inspect_onvif(client, settings)
                inspect_profile(client, settings)
            break
        except CameraError as e:
            print(f"  {label:<22}: {e}")
        finally:
            client.close()
    return worked


def inspect_onvif(client, settings: dict) -> None:
    """What the device actually reports for its ONVIF users.

    `changepwd` returns Code 200 and the read-back then disagrees, so one of
    three things is true and they need different fixes: the user we address does
    not exist (the change went nowhere), the device returns the password in a
    form other than the plaintext we sent (the comparison is wrong, not the
    change), or it genuinely did not take.

    The password itself is never printed — only its length and which known value
    it MATCHES, which answers the question without putting a credential on a
    screen or in a scrollback.
    """
    from raythink_rest import encrypt_password

    print("\n--- ONVIF users (GET /v1/netapp/onvif/user) ---")
    try:
        users = client._request("GET", "/v1/netapp/onvif/user") or []
    except CameraError as e:
        show("read failed", e)
        return

    def forms(label: str, pw: str) -> dict:
        """Every encoding the value could plausibly be in. The user list returns
        24 characters where the config export returns the same password in
        plaintext, so the two endpoints disagree and we have to find out how."""
        md5, sha1, sha256 = (h(pw.encode()).digest()
                             for h in (hashlib.md5, hashlib.sha1, hashlib.sha256))
        b64 = lambda b: base64.b64encode(b).decode()
        return {
            f"{label} plaintext": pw,
            f"AES({label})": encrypt_password(pw),
            f"AES({label}) url-encoded": quote(encrypt_password(pw), safe=""),
            f"b64(md5({label}))": b64(md5),
            f"md5({label}) hex": md5.hex(),
            f"b64(sha1({label}))": b64(sha1),
            f"b64(sha256({label}))": b64(sha256),
            f"b64({label})": b64(pw.encode()),
        }

    candidates: dict[str, str] = {}
    for label, pw in (("factory", settings.get("initial_password",
                                               DEFAULT_INITIAL_PASSWORD)),
                      ("shared", shared_new_password(settings))):
        if pw:
            candidates.update(forms(label, pw))

    if not users:
        show("users", "NONE — the device reports no ONVIF users at all")
        return
    for user in users:
        if not isinstance(user, dict):
            continue
        pw = user.get("Password")
        matches = [n for n, v in candidates.items() if v and pw == v]
        print(f"  name={user.get('Name')!r} group={user.get('Group')!r} "
              f"password: {len(pw) if isinstance(pw, str) else type(pw).__name__} chars, "
              f"matches {' + '.join(matches) if matches else 'NONE of the known forms'}")

    # The export reports the same credential in plaintext, so it settles whether
    # the change took even when the user list's encoding is unrecognised.
    try:
        onvif = (client.export_config().get("OnvifUser") or {}).get("User") or []
    except CameraError as e:
        show("cross-check", f"the export could not be read ({e})")
        return
    for user in onvif:
        pw = user.get("Password") if isinstance(user, dict) else None
        matches = [n for n, v in candidates.items() if v and pw == v]
        print(f"  export says: name={user.get('Name')!r} "
              f"password: {len(pw) if isinstance(pw, str) else type(pw).__name__} chars, "
              f"matches {' + '.join(matches) if matches else 'NONE of the known forms'}")


def onvif_write_test(host: str, scheme: str, timeout: int, settings: dict,
                     password: str) -> None:
    """Which wire form `changepwd` actually wants, established by writing.

    Reading has taken this as far as it goes: the device reports an ONVIF
    password matching NEITHER known password in ANY encoding, through either
    endpoint, even though the run's `changepwd` answered Code 200. So the change
    is landing SOMETHING, and nothing we can read says what.

    The likely reason is that this API encrypts every other password it carries,
    and `changepwd` is no exception — the vendor doc's plaintext example being
    simply wrong, as it is about the two endpoints it omits entirely. Sending
    plaintext would then have the device decrypt it into rubbish and store that,
    which is exactly the shape of what we see.

    So: set it, read it back through the export (which reports this credential in
    plaintext), and see which form comes back as the password we asked for. The
    camera is left on the intended password whichever way it goes.
    """
    from raythink_rest import encrypt_password

    print("\n--- ONVIF write test (this one CHANGES the camera) ---")
    target = shared_new_password(settings)
    client = open_camera(host, scheme=scheme, generation=GEN_REST, timeout=timeout)
    try:
        client.login(password)

        def stored() -> str:
            """What the LIVE user list holds, described against known values.

            The export is deliberately not the source here: after a config import
            it answers from the imported file, which is precisely the confusion
            being untangled."""
            users = client._onvif_users_live()
            me = next((u for u in users if u.get("Name") == client.username), None)
            held = (me or {}).get("Password") or ""
            if held == encrypt_password(target):
                return "the target"
            if not held or held == encrypt_password(""):
                return "EMPTY"
            return "something else"

        show("before", stored())
        for label, wire in (("AES-encrypted", encrypt_password(target)),
                            ("plaintext", target)):
            client._request("PUT", "/v1/netapp/onvif/changepwd",
                            body={"Name": client.username, "NewPassword": wire})
            now = stored()
            print(f"  sent {label:<14}: the device now holds {now}")
            if now != "the target":
                continue

            # The question this run exists to answer. The pipeline's LAST step
            # reboots the camera onto its DHCP address, and the verification that
            # follows has been reporting an ONVIF account with no password — so
            # whether this survives a restart decides whether setting it live is
            # enough or it has to be carried in the imported config instead.
            print("  rebooting to see whether that survives a restart ...")
            client.reboot()
            if not client.wait_reachable(180):
                show("after reboot", "the camera did not come back within 180s")
                return
            client.relogin([password], settle=3)
            print(f"  after the reboot : the device holds {stored()}")
            return
        print("  Neither form took. `changepwd` answers 200 and changes nothing we\n"
              "  can see, so this needs the vendor rather than more guessing.")
    except CameraError as e:
        show("write test failed", e)
    finally:
        client.close()


def inspect_profile(client, settings: dict) -> None:
    """The camera's own export against the profile we try to upload.

    The import is refused with "file is incomplete", and the obvious candidate is
    that the sanitiser removes sections the device requires to be present. This
    names exactly which ones the profile is missing relative to what this camera
    itself exports, which either confirms that or rules it out.
    """
    from raythink_configure import resolve_profile
    from raythink_base import GEN_REST as _REST

    print("\n--- profile vs the camera's own export ---")
    try:
        live = client.export_config()
    except CameraError as e:
        show("export failed", e)
        return
    show("camera exports", f"{len(live)} sections")

    for name in list((settings.get("profiles") or {})):
        try:
            path = resolve_profile(settings, name, _REST)
        except CameraError:
            continue
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
        missing = [k for k in live if k not in profile]
        extra = [k for k in profile if k not in live]
        print(f"  profile '{name}': {len(profile)} sections"
              f" | missing vs the camera: {', '.join(missing) or 'none'}"
              f" | not on the camera: {', '.join(extra) or 'none'}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report what a Raythink camera is and why a login failed. "
                    "Changes nothing.")
    p.add_argument("--host", default="", help=f"camera address (default: the "
                                              f"configured one, else {DEFAULT_HOST})")
    p.add_argument("--config", default=str(BASE_DIR / "config" / "raythink.config.json"),
                   help="shared settings JSON (default: config/raythink.config.json)")
    p.add_argument("--timeout", type=float, default=5.0, help="per-request seconds")
    p.add_argument("--onvif-write-test", action="store_true",
                   help="CHANGES THE CAMERA: set the ONVIF password both ways to "
                        "find out which form the device accepts (newer cameras only)")
    args = p.parse_args()

    try:
        settings = load_settings(args.config)
    except OSError as e:
        print(f"(no config read: {e} — falling back to built-in defaults)")
        settings = {}

    host = args.host or settings.get("host", DEFAULT_HOST)
    scheme = settings.get("scheme", DEFAULT_SCHEME)

    print(f"=== Raythink camera at {scheme}://{host} ===")
    probe_http(host, scheme, args.timeout)

    generation = detect_generation(host, scheme, args.timeout)
    print("\n--- what the probes concluded ---")
    show("generation", GENERATION_LABELS.get(generation,
                                             "NOT a Raythink camera (neither API answered)"))
    if generation is None:
        print("\nNothing here speaks either API. Check the cable and the power, and\n"
              "that this PC holds an address on the camera's subnet (the factory\n"
              f"address {DEFAULT_HOST} needs the PC on 192.168.1.x).")
        return 1

    worked = try_logins(
        host, scheme, generation,
        [("shared (new_password)", shared_new_password(settings)),
         ("factory (initial_password)", settings.get("initial_password",
                                                     DEFAULT_INITIAL_PASSWORD))],
        int(args.timeout), settings)

    if worked and args.onvif_write_test and generation == GEN_REST:
        onvif_write_test(host, scheme, int(args.timeout), settings,
                         dict([("shared (new_password)",
                                shared_new_password(settings)),
                               ("factory (initial_password)",
                                settings.get("initial_password",
                                             DEFAULT_INITIAL_PASSWORD))])[worked])

    print("\n--- summary ---")
    if worked:
        show("logs in with", worked)
        print("\nLogin is fine, so a failing run is failing somewhere else — send the\n"
              "step log from logs/ rather than this.")
        return 0

    show("logs in with", "NOTHING — both candidate passwords were refused")
    print("\nThe device's reason is printed against each attempt above. If it is a\n"
          "wrong password, the camera is on neither the factory nor the shared one\n"
          "and somebody has to say what it IS; if it is a locked account, wait\n"
          "~5 minutes before retrying, and do not keep running the tool at it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
