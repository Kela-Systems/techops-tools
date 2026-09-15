"""The exporter must not ship a template as if it were a site.

`sites/*.yaml` globs the schema template in with the real sites, and the UI
renders the first site in the payload. The template's name sorts before every
real site name, so it became the site the dashboard showed — for several
published versions, with prose about a real site wrapped around it.
"""
import export
from model import load_site


def _sites():
    return [load_site("sites/_template-magos-site.yaml"),
            load_site("sites/kela-fob-03.yaml")]


def test_a_leading_underscore_marks_a_template():
    template, real = _sites()
    assert export.is_template(template)
    assert not export.is_template(real)


def test_templates_are_dropped_and_named():
    kept, dropped = export.drop_templates(_sites())
    assert [s.name for s in kept] == ["kela-fob-03"]
    assert dropped == ["_template-magos-site"], (
        "the caller has to be able to SAY what it left out — a silent drop is "
        "how the opposite bug happened"
    )


def test_dropping_templates_leaves_a_real_site_untouched():
    real = load_site("sites/kela-fob-03.yaml")
    kept, dropped = export.drop_templates([real])
    assert kept == [real]
    assert dropped == []


def test_a_payload_of_only_templates_keeps_nothing():
    # The caller turns this into "nothing to export" rather than writing an
    # empty payload the page renders as a blank site.
    template = load_site("sites/_template-magos-site.yaml")
    kept, dropped = export.drop_templates([template])
    assert kept == []
    assert dropped == ["_template-magos-site"]
