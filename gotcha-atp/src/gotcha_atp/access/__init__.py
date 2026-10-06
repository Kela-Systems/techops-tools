"""How the laptop reaches the unit: SSH over the tailnet (see session.py).

    server    ssh kela@<site>             direct; ControlMaster + -D SOCKS + -L on demand
    operator  ssh kela@<site>-operator    direct; only for the operator's own checks
    backup    kela@192.168.88.10 through the operator, when the direct server login fails

The laptop never joins the unit LAN. Device HTTP/WebSocket goes through the
SOCKS forward and device SSH (Teltonika, Planet) is tunnelled through the
same session — the server's, or the operator's on the backup route.
"""
