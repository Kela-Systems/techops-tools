#!/bin/bash
# Bench fallback: k3s refuses to start without a default route. If no real
# network shows up, add one on a dummy interface at a high metric so any real
# LAN route wins automatically.
sleep 15
ip route | grep -q '^default' && exit 0
ip link add kela-dummy type dummy 2>/dev/null || true
ip link set kela-dummy up
ip addr replace 10.254.254.1/32 dev kela-dummy
ip route add default dev kela-dummy metric 1000 2>/dev/null || true
