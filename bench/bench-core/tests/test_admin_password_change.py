"""Changing the label password to the shared one (`set_admin_password`).

Two properties, both learned the hard way on 07.24.3 and neither visible by
reading the method:

1. **admin (REST) is changed BEFORE root (SSH).** Since RutOS 07.24.2 added
   "Password Policy: added password history", the first-login endpoint refuses
   a new password that matches the CURRENT system password, answering HTTP 422
   "Password is the same. Use a different new password." Setting root first is
   exactly what makes it match, so the old order bricked the step on 07.24.x
   while passing on 07.22.3. The order is the whole fix, and it is the kind of
   thing a later tidy-up reorders without noticing — hence a test on it.

2. **A refused REST half is fatal.** It used to log a warning and carry on,
   which left the device on TWO passwords (root new, admin old): every later
   step then failed behind an admin login that could not succeed, and the run
   reported the symptom instead of the cause. Stopping here also stops before
   root is touched, so a failed run leaves the device exactly as it was found.

The early-return case (device already on the shared password) is covered in
test_verify_only_is_mutation_free.py, which needs it for a different reason.
"""
import pytest
import requests

from bench_core import TeltonikaClient

LABEL = "Lbl-9xQ2!"
SHARED = "test-shared-pw"

FIRSTLOGIN = "/system/actions/change_password_firstlogin"

# The real body an OTD500 on 07.24.3 returns when root was set first — the
# failure this ordering exists to avoid (logs/20260910-123521_otd-avis-test).
SAME_PASSWORD_422 = (
    '{"errors":[{"source":"Validation","section":"general","error":"Password '
    'is the same. Use a different new password.","code":1}],"success":false}'
)


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"data": {"token": "tok-1"}}


class FakeSession:
    """Stands in for the requests.Session on the client.

    Shares one `timeline` with the SSH fake, because the property under test is
    the ORDER of a REST call against a shell command — two transports that no
    single recorder would otherwise see together.
    """

    def __init__(self, timeline, firstlogin_status=200, firstlogin_raises=False):
        self.timeline = timeline
        self.headers: dict = {}
        self.verify = False
        self.firstlogin_status = firstlogin_status
        self.firstlogin_raises = firstlogin_raises

    def post(self, url, json=None, timeout=None):
        if FIRSTLOGIN in url:
            self.timeline.append("rest:firstlogin")
            if self.firstlogin_raises:
                raise requests.exceptions.ConnectionError("connection reset")
            body = SAME_PASSWORD_422 if self.firstlogin_status == 422 else '{"success":true}'
            return FakeResponse(self.firstlogin_status, body)
        if url.endswith("/login"):
            self.timeline.append("rest:login")
            return FakeResponse(200, '{"data":{"token":"tok-1"}}')
        raise AssertionError(f"unexpected POST to {url}")

    def close(self):
        pass


class FakeDevice:
    def __init__(self, timeline):
        self.timeline = timeline

    def __call__(self, command, check=True, exec_timeout=None):
        if "chpasswd" in command:
            self.timeline.append("ssh:chpasswd")
        else:
            self.timeline.append(f"ssh:{command}")
        return ""


def client(**session_kwargs):
    """A client on the LABEL password, wired to a shared timeline."""
    timeline: list[str] = []
    c = TeltonikaClient(host="192.0.2.1")
    c.s = FakeSession(timeline, **session_kwargs)
    c.ssh_exec = FakeDevice(timeline)
    c.password = LABEL
    return c, timeline


# ── 1. the ordering ─────────────────────────────────────────────────────────

def test_admin_is_changed_before_root():
    c, timeline = client()

    c.set_admin_password(SHARED)

    assert timeline.index("rest:firstlogin") < timeline.index("ssh:chpasswd"), (
        "root's chpasswd ran first, so the first-login endpoint would see the "
        "new password already in place and refuse it as 'the same' on 07.24.x"
    )


def test_both_halves_run_and_the_client_ends_on_the_shared_password():
    c, timeline = client()

    c.set_admin_password(SHARED)

    assert "rest:firstlogin" in timeline and "ssh:chpasswd" in timeline
    # Re-login under the new password, so the REST token matches it.
    assert timeline[-1] == "rest:login"
    assert c.password == SHARED
    # The half-changed-device fallback is cleared once both halves agree.
    assert c._ssh_alt_passwords == []


# ── 2. a refused REST half is fatal, and root is left alone ─────────────────

@pytest.mark.parametrize("status", [400, 401, 403, 422, 500])
def test_a_refused_first_login_call_is_fatal(status):
    c, _ = client(firstlogin_status=status)

    with pytest.raises(SystemExit) as e:
        c.set_admin_password(SHARED)

    # The operator needs the status AND the body: "422" alone doesn't say which
    # policy refused, and on 07.24.x the body is the only place that says so.
    assert str(status) in str(e.value)


def test_the_422_names_the_policy_that_refused():
    c, _ = client(firstlogin_status=422)

    with pytest.raises(SystemExit) as e:
        c.set_admin_password(SHARED)

    assert "Password is the same" in str(e.value)


def test_a_refused_first_login_call_does_not_touch_root():
    # The point of failing here rather than warning: root must still hold the
    # label password, so the device is re-runnable instead of half-changed.
    c, timeline = client(firstlogin_status=422)

    with pytest.raises(SystemExit):
        c.set_admin_password(SHARED)

    assert "ssh:chpasswd" not in timeline
    assert c.password == LABEL


def test_a_transport_error_is_fatal_too():
    # Same reasoning as a refusal: we cannot tell whether admin changed, so
    # carrying on would be guessing with root's password.
    c, timeline = client(firstlogin_raises=True)

    with pytest.raises(SystemExit) as e:
        c.set_admin_password(SHARED)

    assert "connection reset" in str(e.value)
    assert "ssh:chpasswd" not in timeline
