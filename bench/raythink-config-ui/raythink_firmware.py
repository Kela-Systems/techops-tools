#!/usr/bin/env python3
"""
Firmware images for the newer (REST /v1) Raythink cameras.

Everything here is about the image FILE — what it is, and whether it belongs on
the camera in front of us. The upload itself lives on the client, because it is
a device conversation; this module is pure and takes a path, so it can be
tested without a camera and used by the web UI's startup banner.

The vendor ships one zip per release, named like

    MVP-JUPITER4S-B1V0222916-CN-20260903.zip

and it carries its own manifest:

  * `install.txt` — the images to write, in order (dtb, kernel, rootfs, usr,
    web, alg). Not something we act on, but its presence is a good signal that
    a file really is one of these bundles rather than, say, a config export
    somebody renamed.
  * `check.img` — the product models the image is BUILT FOR, one per line. This
    is the safety interlock: flashing a thermal camera with an image meant for
    another model is not a failed step, it is a brick, and the camera reports
    its own model as PDName. Checking beforehand costs one zip directory read.

The version the camera will report is not stated in the file. The name encodes
it — `B1V0222916` flashed to a camera reporting `B1.0.22.29.16, 2026-09-03` —
but that is one observation rather than a documented rule, so the floor is
configured explicitly (`firmware.minimum_version`) from what a camera actually
reports after a flash.

Versions are compared BY BUILD DATE, not by their numbers. Two cameras of the
same model, both taking this same image, reported `B1.2.01.01.15, 2026-05-14`
and `B1.0.22.29.16, 2026-09-03`: the first sorts higher numerically and is four
months older. See `fw_at_least` in raythink_base.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

from raythink_base import CameraError, fw_build_date, log

# The manifest entries the vendor's bundles carry. `check.img` is the one we
# act on; `install.txt` is only used to recognise a bundle for what it is.
MODELS_ENTRY = "check.img"
INSTALL_ENTRY = "install.txt"


def read_models(zip_path: str) -> list[str]:
    """The product models an image bundle is built for, per its own manifest.

    Raises CameraError rather than returning empty for a file that is not one of
    these bundles at all: "this zip has no model list" and "this zip is not a
    firmware image" want the same answer from the operator, and an empty list
    would quietly read as "compatible with everything".
    """
    path = Path(zip_path)
    if not path.is_file():
        raise CameraError(f"Firmware image not found: {zip_path}")
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            if MODELS_ENTRY not in names or INSTALL_ENTRY not in names:
                raise CameraError(
                    f"{path.name} does not look like a Raythink firmware bundle "
                    f"(no {MODELS_ENTRY}/{INSTALL_ENTRY} inside). Check "
                    "firmware.zip_path in the config.")
            raw = bundle.read(MODELS_ENTRY).decode("utf-8", "replace")
    except zipfile.BadZipFile:
        raise CameraError(f"{path.name} is not a readable zip archive.")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def check_model(zip_path: str, model: str) -> None:
    """Refuse an image that is not built for `model`.

    Deliberately strict in the one direction that matters. A camera whose model
    we could not read is let through with a warning — that is a gap in our
    reading, not evidence of a mismatch, and the operator chose this image. A
    camera whose model we CAN read and which is absent from the bundle's own
    list is refused outright, because the failure mode there is a brick rather
    than a bad config.
    """
    models = read_models(zip_path)
    if not model:
        log.warning("The camera did not report a model, so %s could not be "
                    "checked against it — flashing on the operator's word.",
                    Path(zip_path).name)
        return
    if model not in models:
        raise CameraError(
            f"{Path(zip_path).name} is built for {', '.join(models)}, and this "
            f"camera is a {model}. Refusing to flash it: the wrong image does "
            "not fail, it bricks. Check firmware.zip_path in the config.")


def plan_upgrade(current: str, minimum: str, zip_path: Optional[str],
                 model: str) -> tuple[bool, str]:
    """Whether to flash, and the note that goes in the run record either way.

    A FLOOR, not a pin: a camera that arrives older than `minimum` is flashed,
    and one that arrives NEWER is passed through with a note rather than being
    downgraded. A newer camera is a camera somebody upgraded on purpose, and
    rolling it back on the bench would be a surprising thing for a provisioning
    run to do.

    "Older" and "newer" mean BUILD DATE, not version number — see fw_at_least
    for why the numbers cannot be used.

    Returns (should_flash, note). Raises CameraError only for the cases an
    operator must fix before the camera can ship: a floor with no date in it, an
    image that is missing when one is needed, or one built for a different model.
    """
    if not minimum:
        return False, "no floor configured — firmware left as-is"

    floor_date = fw_build_date(minimum)
    if floor_date is None:
        raise CameraError(
            f"firmware.minimum_version is {minimum!r}, which has no build date in "
            "it. Versions are compared by date (the vendor's numbering is not "
            "chronological), so the floor must look like "
            "'B1.0.22.29.16, 2026-09-03'.")

    current_date = fw_build_date(current)
    if current_date == floor_date:
        return False, f"{current} — at the floor"
    if current_date is not None and current_date > floor_date:
        return False, (f"{current} — built {current_date}, newer than the floor's "
                       f"{floor_date}, left alone")

    if not zip_path or not Path(zip_path).is_file():
        raise CameraError(
            f"The camera is on {current or 'an unreadable version'}, below the "
            f"{minimum} floor, and the image is missing. Put the vendor zip in "
            "firmware/ and point firmware.zip_path at it.")
    check_model(zip_path, model)
    if current_date is None:
        # Undatable. Flashing costs a reboot; skipping ships the wrong build
        # silently, so this errs towards the one that is visible and recoverable.
        return True, (f"{current or 'unknown'} has no readable build date, so it "
                      f"cannot be shown to meet the {minimum} floor — flashing")
    return True, f"{current} was built {current_date}, before the floor's {floor_date}"


__all__ = ["read_models", "check_model", "plan_upgrade",
           "MODELS_ENTRY", "INSTALL_ENTRY"]
