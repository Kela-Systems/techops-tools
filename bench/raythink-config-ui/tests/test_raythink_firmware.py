"""The firmware image: what it is, and whether it belongs on this camera.

The zip fixtures here are real zips built in a tmp_path rather than mocks of
`zipfile`, because the thing under test IS the reading of a vendor bundle — a
mock would only assert that we call the functions we call.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench_core import make_step_runner
import raythink_configure as cfg
from raythink_base import CameraError, fw_at_least
from raythink_firmware import check_model, plan_upgrade, read_models

MODEL = "XX-MVP-PC4-V100"
# Real strings from real cameras, and deliberately awkward: OLDER sorts HIGHER
# than FLOOR numerically while being four months older by date. Comparison is by
# date, so these are the pair that keeps it honest.
FLOOR = "B1.0.22.29.16, 2026-09-03"
OLDER = "B1.2.01.01.15, 2026-05-14"
NEWER = "B1.2.03.00.01, 2026-11-02"


def bundle(tmp_path, *, models=(MODEL, "XX-MVP-COMMON-V100"), name="fw.zip",
           manifest=True) -> str:
    """A stand-in for the vendor's zip, carrying the two manifest entries the
    real one does."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        if manifest:
            z.writestr("install.txt", "dtb.img\nkernel.img\nrootfs.img\n")
        z.writestr("check.img", "\n".join(models) + "\n")
        z.writestr("kernel.img", b"\x00" * 16)
    return str(path)


# ── reading the bundle's own manifest ────────────────────────────────────────

def test_the_models_are_read_from_the_bundle(tmp_path):
    assert read_models(bundle(tmp_path)) == [MODEL, "XX-MVP-COMMON-V100"]


def test_a_missing_image_is_named(tmp_path):
    with pytest.raises(CameraError) as e:
        read_models(str(tmp_path / "nope.zip"))
    assert "not found" in str(e.value)


def test_a_zip_that_is_not_a_firmware_bundle_is_refused(tmp_path):
    # Somebody pointing zip_path at the wrong file should hear THAT, rather
    # than "no models found", which reads as a compatibility problem.
    with pytest.raises(CameraError) as e:
        read_models(bundle(tmp_path, manifest=False))
    assert "does not look like a Raythink firmware bundle" in str(e.value)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    plain = tmp_path / "fw.zip"
    plain.write_text("this is not a zip")
    with pytest.raises(CameraError) as e:
        read_models(str(plain))
    assert "not a readable zip" in str(e.value)


# ── the model interlock ──────────────────────────────────────────────────────

def test_an_image_for_this_model_passes(tmp_path):
    check_model(bundle(tmp_path), MODEL)


def test_an_image_for_another_model_is_refused(tmp_path):
    # The one check that matters most: the wrong image does not fail the step,
    # it bricks the camera.
    with pytest.raises(CameraError) as e:
        check_model(bundle(tmp_path, models=("XX-MVP-OTHER-V200",)), MODEL)
    assert "bricks" in str(e.value)
    assert MODEL in str(e.value)


def test_a_camera_that_reports_no_model_is_flashed_anyway(tmp_path):
    # A model we could not read is a gap in OUR reading, not evidence of a
    # mismatch, and the operator picked this image deliberately.
    check_model(bundle(tmp_path), "")


# ── the floor decision ───────────────────────────────────────────────────────

def test_a_camera_below_the_floor_is_flashed(tmp_path):
    flash, note = plan_upgrade(OLDER, FLOOR, bundle(tmp_path), MODEL)
    assert flash is True
    assert "before the floor's 2026-09-03" in note


def test_a_camera_at_the_floor_is_left_alone(tmp_path):
    flash, note = plan_upgrade(FLOOR, FLOOR, bundle(tmp_path), MODEL)
    assert flash is False
    assert "at the floor" in note


def test_a_newer_camera_is_not_downgraded(tmp_path):
    # A floor, not a pin. A camera somebody upgraded on purpose is not rolled
    # back by a provisioning run.
    flash, note = plan_upgrade(NEWER, FLOOR, bundle(tmp_path), MODEL)
    assert flash is False
    assert "newer than" in note


def test_no_floor_configured_means_no_firmware_step(tmp_path):
    flash, note = plan_upgrade(OLDER, "", bundle(tmp_path), MODEL)
    assert flash is False
    assert "no floor configured" in note


def test_a_camera_below_the_floor_with_no_image_is_an_error(tmp_path):
    with pytest.raises(CameraError) as e:
        plan_upgrade(OLDER, FLOOR, str(tmp_path / "missing.zip"), MODEL)
    assert "below the" in str(e.value) and "firmware.zip_path" in str(e.value)


def test_a_camera_at_the_floor_needs_no_image_at_all(tmp_path):
    # The common case on a re-run: nothing to do, so a missing image is not
    # worth failing over.
    flash, note = plan_upgrade(FLOOR, FLOOR, None, MODEL)
    assert flash is False
    assert "at the floor" in note


def test_the_model_is_checked_before_any_flash(tmp_path):
    # The interlock has to sit on the path that actually flashes, not beside it.
    wrong = bundle(tmp_path, models=("XX-MVP-OTHER-V200",))
    with pytest.raises(CameraError) as e:
        plan_upgrade(OLDER, FLOOR, wrong, MODEL)
    assert "bricks" in str(e.value)


# ── date, not version number ─────────────────────────────────────────────────
#
# The vendor's numbering is not chronological, and this is the pair that proves
# it: two cameras of the SAME model, both taking the same image. Ordering by the
# numbers leaves the May camera on its old build, which is the exact outcome a
# floor exists to prevent.

def test_the_older_build_is_older_even_though_it_sorts_higher(tmp_path):
    assert OLDER > FLOOR                       # ...as plain strings
    flash, note = plan_upgrade(OLDER, FLOOR, bundle(tmp_path), MODEL)
    assert flash is True
    assert "before the floor's 2026-09-03" in note


def test_the_firmware_row_agrees_with_the_flash_decision(tmp_path):
    # If these two ever disagree, a camera is either flashed and then failed, or
    # skipped and then passed. They must share one comparison.
    for current in (OLDER, FLOOR, NEWER, "", "B1.0.22.29.16"):
        flash, _ = plan_upgrade(current, FLOOR, bundle(tmp_path), MODEL)
        assert flash is not fw_at_least(current, FLOOR), current


def test_a_floor_with_no_date_is_a_configuration_error(tmp_path):
    # Silently comparing nothing would leave every camera on whatever it shipped
    # with, so this names the setting and shows the shape it wants.
    with pytest.raises(CameraError) as e:
        plan_upgrade(OLDER, "B1.0.22.29.16", bundle(tmp_path), MODEL)
    assert "firmware.minimum_version" in str(e.value)
    assert "2026-09-03" in str(e.value)


def test_a_camera_whose_version_has_no_date_is_flashed_rather_than_assumed_current(tmp_path):
    # Flashing costs a reboot; skipping ships the wrong build without saying so.
    flash, note = plan_upgrade("B1.0.22.29.16", FLOOR, bundle(tmp_path), MODEL)
    assert flash is True
    assert "no readable build date" in note


def test_a_camera_that_reports_nothing_is_flashed_too(tmp_path):
    flash, _ = plan_upgrade("", FLOOR, bundle(tmp_path), MODEL)
    assert flash is True


# ── the pipeline step ────────────────────────────────────────────────────────
#
# The step is what turns a decision into a flash, a wait and a re-login. It is
# driven directly rather than through configure_camera, so each outcome can be
# arrived at without standing up the whole pipeline.

class StubCamera:
    """A camera for the firmware step: records what was asked of it, and can be
    told to come back on a different address, or not at all."""

    def __init__(self, *, after=FLOOR, comes_back=True):
        self.calls: list[str] = []
        self.flashed: list[str] = []
        self.after = after
        self.comes_back = comes_back

    def upgrade_firmware(self, zip_path, **kw):
        self.calls.append("upgrade_firmware")
        self.flashed.append(zip_path)

    def follow_by_mac(self, mac, subnets, **kw):
        self.calls.append("follow_by_mac")
        return self.comes_back

    def relogin(self, passwords, **kw):
        self.calls.append("relogin")

    def get_identity(self):
        return {"firmware": self.after, "model": MODEL, "mac": MAC}


MAC = "ac:86:d1:40:cc:7d"


def run_step(camera, settings, *, firmware=OLDER, mac=MAC):
    failures, step = make_step_runner(cfg.log, CameraError)
    note = cfg.apply_firmware_floor(
        camera, settings, {"firmware": firmware, "model": MODEL, "mac": mac},
        subnets=["192.168.1.0/24"], passwords=["pw"], step=step)
    return note, failures


def fw_settings(tmp_path, **over):
    return {"firmware": {"minimum_version": FLOOR,
                         "zip_path": bundle(tmp_path), **over}}


def test_a_camera_below_the_floor_is_flashed_then_followed_and_relogged_in(tmp_path):
    # The order matters and is the whole point of the step: the flash reboots
    # the camera onto an address we did not choose, so it has to be found again
    # before anything can log in.
    cam = StubCamera()
    note, failures = run_step(cam, fw_settings(tmp_path))
    assert cam.calls == ["upgrade_firmware", "follow_by_mac", "relogin"]
    assert failures == []
    assert note == f"{OLDER} -> {FLOOR}"


def test_a_camera_at_the_floor_is_not_touched(tmp_path):
    cam = StubCamera()
    note, failures = run_step(cam, fw_settings(tmp_path), firmware=FLOOR)
    assert cam.calls == []
    assert failures == [] and "at the floor" in note


def test_no_firmware_configured_leaves_the_camera_alone(tmp_path):
    # A bench that has not been given an image behaves exactly as it did before
    # the firmware step existed.
    cam = StubCamera()
    note, failures = run_step(cam, {})
    assert cam.calls == [] and failures == []
    assert "no floor configured" in note


def test_a_camera_that_never_comes_back_fails_loudly_without_aborting(tmp_path):
    # And says the one thing that matters: a camera mid-write must not be
    # power-cycled, which is the operator's instinct when a step hangs.
    cam = StubCamera(comes_back=False)
    note, failures = run_step(cam, fw_settings(tmp_path))
    assert cam.calls == ["upgrade_firmware", "follow_by_mac"]
    assert len(failures) == 1
    # The image is written by this point, so the message must send the operator
    # looking for the camera rather than warning them off the power switch.
    assert "did not reappear" in failures[0]


def test_a_flash_that_did_not_take_is_caught(tmp_path):
    # The camera came back, logged in, and is still on the old build.
    cam = StubCamera(after=OLDER)
    note, failures = run_step(cam, fw_settings(tmp_path))
    assert len(failures) == 1
    assert "rather than" in failures[0]


def test_a_camera_with_no_mac_is_not_flashed_into_the_dark(tmp_path):
    # Without a MAC there is no way to find it after it reboots, so the flash
    # must not start at all.
    cam = StubCamera()
    note, failures = run_step(cam, fw_settings(tmp_path), mac="")
    assert cam.calls == []
    assert len(failures) == 1 and "refusing to flash" in failures[0]


def test_force_flashes_a_camera_that_is_already_current(tmp_path):
    # --firmware-only --force-firmware exists to find out what a bundle reports,
    # so the floor comparison is exactly what has to be bypassed.
    cam = StubCamera()
    failures, step = make_step_runner(cfg.log, CameraError)
    cfg.apply_firmware_floor(cam, fw_settings(tmp_path),
                             {"firmware": FLOOR, "model": MODEL, "mac": MAC},
                             subnets=["192.168.1.0/24"], passwords=["pw"],
                             step=step, force=True)
    assert "upgrade_firmware" in cam.calls and failures == []


def test_force_still_will_not_flash_the_wrong_model(tmp_path):
    # The floor is bypassed; the model interlock is not. The wrong image bricks.
    cam = StubCamera()
    settings = {"firmware": {"minimum_version": "",
                             "zip_path": bundle(tmp_path, models=("XX-OTHER-V1",))}}
    failures, step = make_step_runner(cfg.log, CameraError)
    cfg.apply_firmware_floor(cam, settings,
                             {"firmware": OLDER, "model": MODEL, "mac": MAC},
                             subnets=[], passwords=["pw"], step=step, force=True)
    assert cam.calls == []
    assert len(failures) == 1 and "bricks" in failures[0]


def test_force_with_no_floor_does_not_fail_the_version_check_afterwards(tmp_path):
    # There is nothing to assert the result against, and inventing a comparison
    # would fail the one run whose purpose is to discover the version.
    cam = StubCamera(after="B1.2.02.29.16, 2026-09-03")
    settings = {"firmware": {"minimum_version": "", "zip_path": bundle(tmp_path)}}
    failures, step = make_step_runner(cfg.log, CameraError)
    cfg.apply_firmware_floor(cam, settings,
                             {"firmware": OLDER, "model": MODEL, "mac": MAC},
                             subnets=[], passwords=["pw"], step=step, force=True)
    assert failures == []


def test_a_firmware_failure_does_not_abort_the_run(tmp_path):
    # It is recorded like any other step failure. An operator would rather have
    # a fully configured camera on the wrong build, named in the failures, than
    # an aborted run leaving it half-done.
    cam = StubCamera(comes_back=False)
    run_step(cam, fw_settings(tmp_path))   # does not raise
