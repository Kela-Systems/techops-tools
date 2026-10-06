"""S0–S2 parsers against captured-shape output."""
from gotcha_atp.devices import normalize_mac, parse_info_table
from gotcha_atp.stages import s0, s1, s2

PING = """PING 192.168.88.50 (192.168.88.50) 56(84) bytes of data.

--- 192.168.88.50 ping statistics ---
20 packets transmitted, 20 received, 0% packet loss, time 3810ms
rtt min/avg/max/mdev = 0.312/0.498/1.204/0.191 ms"""


def test_ping():
    p = s1.parse_ping(PING)
    assert p == {"sent": 20, "received": 20, "loss_pct": 0.0, "min": 0.312, "avg": 0.498,
                 "max": 1.204, "mdev": 0.191}
    lost = s1.parse_ping("20 packets transmitted, 0 received, +20 errors, 100% packet loss, time 4ms")
    assert lost["loss_pct"] == 100.0 and lost["avg"] is None


def test_blocks():
    text = "@@PING 192.168.88.1\nbody one\n@@END\n@@PING 192.168.88.2\nline\nline2\n@@END\n"
    assert s1.parse_blocks(text, "PING") == {"192.168.88.1": "body one", "192.168.88.2": "line\nline2"}


def test_arp_probe_script_and_parse():
    import base64
    compile(s1.ARP_PROBE, "arp_probe", "exec")
    cmd = s1.arp_probe_command("192.168.88.1", ["192.168.88.1", "192.168.88.50"], "192.168.88.10")
    b64 = cmd.split("echo ")[1].split(" |")[0]
    assert base64.b64decode(b64).decode() == s1.ARP_PROBE
    assert cmd.endswith("192.168.88.1,192.168.88.50 192.168.88.10")
    out = 'noise\n{"192.168.88.1": ["209727363a84"], "192.168.88.50": ["aabbccddee01", "aabbccddee99"], "192.168.88.10": []}\n'
    assert s1.parse_arp_probe(out) == {"192.168.88.1": ["20:97:27:36:3a:84"],
                                       "192.168.88.50": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:99"],
                                       "192.168.88.10": []}
    assert s1.parse_arp_probe("sudo: a password is required") is None


POE = """
 Port | PoE State | Status    | Class | Power Used(W) | Current(mA) | Priority | PD Class | Power Limit(W)
------+-----------+-----------+-------+---------------+-------------+----------+----------+---------------
  01  | Enable    | On        |   4   |  31.2         |  580        | low      | class4   | 95.0
  02  | Enable    | On        |   4   |  29.8         |  560        | low      | class4   | 95.0
  03  | Enable    | On        |   4   |  33.0         |  600        | low      | class4   | 95.0
  04  | Enable    | On        |   4   |  30.1         |  570        | low      | class4   | 95.0
  05  | Enable    | On        |   2   |   4.6         |   90        | low      | class2   | 95.0
  06  | Enable    | Off       |   0   |   0.0         |    0        | low      | -        | 95.0
"""


def test_poe_draw_uses_the_delivered_column_not_the_limit():
    draw, head = s1.parse_poe_draw(POE)
    assert head.startswith("Power Used")
    assert draw == {1: 31.2, 2: 29.8, 3: 33.0, 4: 30.1, 5: 4.6, 6: 0.0}


def test_poe_without_a_draw_column_is_unknown():
    draw, why = s1.parse_poe_draw(" Port | State | Limit(W)\n 01 | Enable | 95.0\n")
    assert draw == {} and "no delivered-power column" in why


def test_mac_table():
    text = ("VID  MAC Address        Type     Ports\n"
            "1    a8-f7-e0-00-00-50  Dynamic  gi1\n"
            "1    00:1e:42:aa:bb:cc  Dynamic  GigabitEthernet9\n"
            "1    aabb.ccdd.ee70     Dynamic  gi5\n")
    assert s1.parse_mac_table(text) == {"a8:f7:e0:00:00:50": 1, "00:1e:42:aa:bb:cc": 9,
                                        "aa:bb:cc:dd:ee:70": 5}


def test_port_status():
    text = ("Port  Link  Speed   Duplex\n"
            "gi1   Up    1000M   Full\n"
            "gi2   Up    100M    Full\n"
            "gi7   Down  -       -\n")
    st = s1.parse_port_status(text)
    assert st[1] == {"link": "up", "speed": "1000", "duplex": "full"}
    assert st[2]["speed"] == "100" and st[7]["link"] == "down"


def test_netdev_status_keeps_physical_linked_ports():
    text = ('{"lan1": {"carrier": true, "speed": "1000F", "statistics": {"rx_errors": 0, "tx_errors": 2}},'
            ' "lan2": {"carrier": false, "speed": "-1"},'
            ' "br-lan": {"type": "bridge", "carrier": true, "speed": "1000F"},'
            ' "lan1.10": {"carrier": true, "speed": "1000F"}}')
    assert s1.parse_netdev_status(text) == [{"name": "lan1", "speed": "1000F", "errors": 2}]


def test_modem_parsers():
    assert s1.parse_cops_act('+COPS: 0,0,"Partner IL",7\n\nOK') == 7
    assert s1.parse_cops_act("+COPS: 0\nOK") is None
    assert s1.parse_signal_dbm("-71") == -71
    sim = ("simcard.@sim[0]=sim\nsimcard.@sim[0].position='1'\nsimcard.@sim[0].primary='1'\n"
           "simcard.@sim[2]=sim\nsimcard.@sim[2].position='3'\n")
    assert s1.parse_primary_sim(sim) == "1"
    assert s1.parse_primary_sim("simcard.@sim[2].position='3'\nsimcard.@sim[2].primary='1'") == "3"


def test_rms_get_status():
    # gotcha-rev3-prototype (connected) and gotcha-rev3-dev (license expired), RUTM_R_00.07.24.3
    assert s1.rms_state('{"next_retry": 0, "connection_status": 0, "error_text": "", "error_code": 0, '
                        '"error": 0}') == (True, "connected")
    ok, why = s1.rms_state('{"next_retry": 1791292406, "connection_status": 1, "error_text": "Expired license", '
                           '"error_code": 15, "error": 1}')
    assert ok is False and why == "Expired license (error 15)"
    assert s1.rms_state("") == (None, "")


def test_rms_connected():
    assert s1.rms_connected('{"connection_state": "connected"}') is True
    assert s1.rms_connected('{"status": "disconnected", "disconnect_reason": "x"}') is False
    assert s1.rms_connected("") is None
    assert s1.rms_connected('{"unrelated": 1}') is None


def test_df_free():
    df = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/nvme0n1p1 960000 200000 760000 21% /mnt/data\n"
    assert s2.df_used_pct(df) == 21
    free = ("               total        used        free      shared  buff/cache   available\n"
            "Mem:           30536        6143       18000          55        6392       23810\n")
    assert s2.free_available_mb(free) == 23810


def test_chrony():
    tracking = ("Reference ID    : C0A85801 (192.168.88.1)\nStratum         : 3\n"
                "System time     : 0.000812345 seconds slow of NTP time\nLeap status     : Normal\n")
    tr = s2.chrony_tracking(tracking)
    assert tr["leap"] == "Normal" and tr["stratum"] == 3 and round(tr["offset_ms"], 3) == -0.812
    sources = ("MS Name/IP address         Stratum Poll Reach LastRx Last sample\n"
               "===============================================================================\n"
               "^* 162.159.200.1                 3   6   377    35   -123us[ -150us] +/-   12ms\n"
               "^+ 216.239.35.0                  1   6   377    36   +234us[ +207us] +/-   20ms\n")
    assert [x["state"] for x in s2.chrony_sources(sources)] == ["*", "+"]
    clients = ("Hostname                      NTP   Drop Int IntL Last     Cmd   Drop Int  Last\n"
               "===============================================================================\n"
               "192.168.88.50                  12      0   6   -    23       0      0   -     -\n"
               "192.168.88.60                  12      0   6   -    23       0      0   -     -\n")
    assert s2.chrony_clients(clients) == {"192.168.88.50", "192.168.88.60"}


def test_l4t():
    line = "# R36 (release), REVISION: 4.3, GCID: 38968081, BOARD: generic, EABI: aarch64, DATE: Wed Jan  8 01:49:37 UTC 2025"
    assert s2.l4t_version(line) == "R36.4.3"


def test_build_info():
    bi = s0.parse_build_info("site=kela-gotcha-07\nrole=gotcha\n# comment\nstatic_ip=192.168.88.10/24\n")
    assert bi == {"site": "kela-gotcha-07", "role": "gotcha", "static_ip": "192.168.88.10/24"}


def test_discovery_errors_are_worded():
    from gotcha_atp.discovery import friendly_error
    assert "web interface" in friendly_error(Exception("ProtocolError: Malformed reply"))
    assert friendly_error(Exception("SSH to x failed: SSHException: Error reading SSH protocol banner")) \
        == "no answer on SSH (port 22)"
    assert friendly_error(Exception("SSHException: No existing session")) == "no answer on SSH (port 22)"
    assert friendly_error(Exception("192.168.88.50 login refused (HTTP 401)")) == "192.168.88.50 login refused (HTTP 401)"


def test_teltonika_mnfinfo_is_unwrapped():
    from gotcha_atp.access.exec import ExecResult
    from gotcha_atp.devices import teltonika_identity

    class Fake:
        host = "192.168.88.1"

        def sections(self, commands, timeout=40):
            return {"mnf": ExecResult(0, '{"mnfinfo": {"serial": "6010212571", "mac": "209727377BB8"}}'),
                    "board": ExecResult(0, '{"model": "Teltonika RUTM08", "hostname": "RUTM08"}'),
                    "version": ExecResult(0, "RUTM_R_00.07.24.3\n")}

    ident = teltonika_identity(Fake())
    assert ident["serial"] == "6010212571" and ident["mac"] == "20:97:27:37:7b:b8"
    assert ident["model"] == "Teltonika RUTM08" and ident["firmware"] == "RUTM_R_00.07.24.3"


def test_planet_info_table_and_macs():
    page = ("<table><tr><td class=a>System Name</td><td>IGS-4215-8UP2T2S</td></tr>"
            "<tr><td>Firmware Version</td><td>1.305b260324</td></tr>"
            "<tr><td>MAC Address</td><td>A8-F7-E0-F6-C4-3A</td></tr></table>")
    f = parse_info_table(page)
    assert f["System Name"] == "IGS-4215-8UP2T2S" and f["Firmware Version"] == "1.305b260324"
    assert normalize_mac(f["MAC Address"]) == "a8:f7:e0:f6:c4:3a"
    assert normalize_mac("not a mac") is None
