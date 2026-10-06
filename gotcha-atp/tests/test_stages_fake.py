"""S1/S2 row logic against a fake session returning canned host output."""
from pathlib import Path

from gotcha_atp import release
from gotcha_atp.access.creds import Credentials, Redactor
from gotcha_atp.access.exec import ExecResult
from gotcha_atp.access.session import Unit
from gotcha_atp.context import Context
from gotcha_atp.stages import s1, s2

ROOT = Path(__file__).resolve().parents[1]
REL = release.load(ROOT / "release.yaml")

S2_OUT = {
    "hostname": "kela-gotcha-07",
    "machine_id": "0123456789abcdef0123456789abcdef",
    "mountpoint": "mounted",
    "findmnt": "1111-2222 /dev/nvme0n1p1",
    "fstab": "UUID=1111-2222 /mnt/data ext4 defaults 0 2\n",
    "df_data": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/nvme0n1p1 1 1 1 42% /mnt/data\n",
    "df_root": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/mmcblk0p1 1 1 1 81% /\n",
    "free": "  total used free shared buff/cache available\nMem: 30536 6143 18000 55 6392 23810\n",
    "tracking": "Leap status     : Normal\nSystem time     : 0.000100000 seconds fast of NTP time\n",
    "sources": "^* 162.159.200.1  3 6 377 35 -1us[-1us] +/- 12ms\n",
    "clients": "\n".join(f"{ip}  12 0 6 - 23 0 0 - -" for ip in
                         ["192.168.88.50", "192.168.88.51", "192.168.88.52", "192.168.88.53",
                          "192.168.88.60", "192.168.88.61", REL.plan.camera]),
    "tz": "Asia/Jerusalem",
    "l4t": "# R36 (release), REVISION: 4.3, GCID: 1",
    "active": "active\nactive",
    "tsip": "100.84.12.10",
    "kernel": "[  12.3] eth0: Link is Down\n",
}


class FakeSession:
    has_operator = True
    route = "tailnet"
    operator_error = "no gotcha-x-operator peer on the tailnet"

    def __init__(self, sections=None, execs=None):
        self._sections = sections or {}
        self._execs = execs or {}

    def sections(self, target, commands, timeout=60):
        return {k: ExecResult(0, self._sections.get(k, "")) for k in commands}

    def exec(self, target, script, timeout=30):
        for needle, out in self._execs.items():
            if needle in script:
                return ExecResult(0, out)
        return ExecResult(1, "", "unexpected command")

    def proxy_command(self, host, port=22):
        return "true"


class FakeDevice:
    def __init__(self, outputs):
        self.outputs = outputs

    def sections(self, commands, timeout=40):
        return {k: ExecResult(0, self.outputs.get(k, "")) for k in commands}

    def run(self, cmd, timeout=None):
        return ExecResult(0, self.outputs.get(cmd, ""))

    def close(self):
        pass


def _ctx(session, devices=None):
    ctx = Context(unit=Unit(site="kela-gotcha-07"), creds=Credentials(), redact=Redactor(),
                  release=REL, session=session)
    if devices is not None:
        ctx.device_ssh = lambda host, family="teltonika": devices[host]
    return ctx


def test_s2_rows():
    rows = {r.id: r for r in s2.run(_ctx(FakeSession(sections=S2_OUT)))}
    assert len(rows) == 8
    assert rows["S2.1"].state == "pass"
    assert rows["S2.2"].state == "pass"
    assert rows["S2.3"].state == "fail" and "81% used" in rows["S2.3"].actual
    assert rows["S2.4"].state == "fail" and "missing router 192.168.88.1" in rows["S2.4"].actual
    assert rows["S2.5"].state == "pass"
    assert rows["S2.6"].state == "pass" and rows["S2.6"].actual == "R36.4.3"
    assert rows["S2.7"].state == "pass"
    assert rows["S2.8"].state == "fail" and "1 events" in rows["S2.8"].actual


def test_s1_ping_matrix():
    body = ("20 packets transmitted, 20 received, 0% packet loss, time 3810ms\n"
            "rtt min/avg/max/mdev = 0.3/0.5/1.2/0.2 ms")
    slow = ("20 packets transmitted, 19 received, 5% packet loss, time 3810ms\n"
            "rtt min/avg/max/mdev = 0.3/9.5/40.2/8.2 ms")
    out = "".join(f"@@PING {ip}\n{slow if ip.endswith('.52') else body}\n@@END\n"
                  for ip in REL.plan.addresses())
    ctx = _ctx(FakeSession(execs={"ping -n -q": out}))
    row = s1._s11(ctx)
    assert row.state == "fail"
    assert "radar_2 .52: 5% loss" in row.actual
    assert row.detail["matrix"]["192.168.88.50"]["avg"] == 0.5


def test_s1_router_and_switch_and_modem():
    devices = {
        REL.plan.router: FakeDevice({"rms_enable": "1", "rms_status": '{"connection_state": "connected"}',
                                     "ts": "100.70.1.2",
                                     "ntpclient": "ntpclient.1=ntpserver\nntpclient.1.hostname='192.168.88.10'\n"
                                                  "ntpclient.ntpclient=ntpclient\nntpclient.ntpclient.enabled='1'",
                                     # RutOS's dormant timeserver list: disabled, stock pool — ignored
                                     "sysntp": "0\n0.pool.ntp.org 1.pool.ntp.org"}),
        REL.plan.switch: FakeDevice({"ntp": "0.pool.ntp.org", "board": '{"model": "Teltonika TSW202"}'}),
        REL.plan.modem: FakeDevice({"cops": '+COPS: 0,0,"Partner",7', "signal": "-112",
                                    "sim": "simcard.@sim[0].position='1'\nsimcard.@sim[0].primary='1'",
                                    "wan": "3 packets transmitted, 3 received, 0% packet loss"}),
    }
    ctx = _ctx(FakeSession(), devices)
    assert s1._s15(ctx).state == "pass"
    sw = s1._s17(ctx)
    assert sw.state == "fail" and "NTP server: 0.pool.ntp.org" in sw.actual
    modem = s1._s16(ctx)
    assert modem.state == "fail" and "-112 dBm" in modem.actual


def test_s28_empty_sata_ports_are_not_events():
    # gotcha-rev3-prototype (NVMe data disk, both SATA ports empty) and rev3-dev (data on ata2)
    boot = ("[    8.197987] gotcha-rev3-prototype kernel: ata1: SATA link down (SStatus 0 SControl 300)\n"
            "[    8.513981] gotcha-rev3-prototype kernel: ata2: SATA link down (SStatus 0 SControl 300)\n")
    assert s2.kernel_events(boot) == ([], ["ata1", "ata2"])
    dev = ("[   10.155185] kernel: ata1: SATA link down (SStatus 0 SControl 300)\n"
           "[   10.629000] kernel: ata2: SATA link up 3.0 Gbps (SStatus 123 SControl 300)\n")
    assert s2.kernel_events(dev) == ([], ["ata1"])
    lost = dev + "[ 5000.1] kernel: ata2: SATA link down (SStatus 0 SControl 300)\n" \
                 "[ 5000.2] kernel: ata2: hard resetting link\n"
    events, _ = s2.kernel_events(lost)
    assert len(events) == 2 and "ata2: SATA link down" in events[0]


def test_s15_ntpclient_leftovers_fail():
    # gotcha-rev3-prototype, 2026-10-06: .10 set in the WebUI, stock Google servers left behind
    text = ("ntpclient.1=ntpserver\nntpclient.1.hostname='192.168.88.10'\n"
            "ntpclient.2=ntpserver\nntpclient.2.hostname='time2.google.com'\n"
            "ntpclient.ntpclient=ntpclient\nntpclient.ntpclient.enabled='1'\nntpclient.ntpclient.interval='120'")
    assert s1.parse_ntpclient(text) == {"enabled": True, "servers": ["192.168.88.10", "time2.google.com"]}
    devices = {REL.plan.router: FakeDevice({"rms_enable": "1", "rms_status": '{"connection_state": "connected"}',
                                            "ts": "100.70.1.2", "ntpclient": text,
                                            "sysntp": "0\n0.pool.ntp.org"})}
    row = s1._s15(_ctx(FakeSession(), devices))
    checks = {c["label"]: c["ok"] for c in row.detail["checks"]}
    assert checks["NTP client"] is True and checks["other NTP servers"] is False


def test_s1_operator_and_magicdns():
    ctx = _ctx(FakeSession(execs={"getent hosts": "192.168.88.10   kela.local",
                                  "debug prefs": '{"CorpDNS": false}'}))
    assert s1._s18(ctx).state == "pass"
    assert s1._s19(ctx).state == "pass"
    fb = FakeSession()
    fb.has_operator = False
    assert s1._s18(_ctx(fb)).state == "amber"
