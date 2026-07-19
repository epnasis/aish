#!/bin/bash
# Egress policy for the Claude sandbox: allow internet, block private LAN/VLANs.
# Runs inside the container at every start (postStartCommand, via sudo).
# Requires NET_ADMIN (present in the ToB devcontainer runArgs).
#
# Policy rationale: agents need open web access (web search, scraping),
# but must not reach home LAN ranges (SSRF containment). DNS is exempted
# only toward the resolvers configured in /etc/resolv.conf.
set -euo pipefail

CHAIN=CLD_LAN

# Own subnet (container-to-gateway/container traffic must keep working)
OWN_SUBNET=$(ip -o -f inet addr show eth0 2>/dev/null | awk '{print $4}' | head -1)

iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"

[ -n "$OWN_SUBNET" ] && iptables -A "$CHAIN" -d "$OWN_SUBNET" -j RETURN

# DNS: allow only the configured resolvers (loopback resolvers like Docker's
# embedded DNS never traverse this chain's reject rules anyway)
while read -r ns; do
  iptables -A "$CHAIN" -d "$ns" -p udp --dport 53 -j RETURN
  iptables -A "$CHAIN" -d "$ns" -p tcp --dport 53 -j RETURN
done < <(awk '/^nameserver/ {print $2}' /etc/resolv.conf | grep -E '^[0-9.]+$' || true)

iptables -A "$CHAIN" -d 10.0.0.0/8      -j REJECT
iptables -A "$CHAIN" -d 100.64.0.0/10   -j REJECT
iptables -A "$CHAIN" -d 172.16.0.0/12   -j REJECT
iptables -A "$CHAIN" -d 192.168.0.0/16  -j REJECT
iptables -A "$CHAIN" -d 169.254.0.0/16  -j REJECT

# Hook into OUTPUT exactly once
iptables -C OUTPUT -j "$CHAIN" 2>/dev/null || iptables -I OUTPUT -j "$CHAIN"

# IPv6: block unique-local and link-local ranges (best effort; docker
# networks are typically IPv4-only). Note: if Docker IPv6 is ever enabled
# and the LAN has global IPv6, private-range filters won't cover it.
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -N "$CHAIN" 2>/dev/null || ip6tables -F "$CHAIN"
  ip6tables -A "$CHAIN" -d fc00::/7  -j REJECT 2>/dev/null || true
  ip6tables -A "$CHAIN" -d fe80::/10 -j REJECT 2>/dev/null || true
  ip6tables -C OUTPUT -j "$CHAIN" 2>/dev/null || ip6tables -I OUTPUT -j "$CHAIN" 2>/dev/null || true
fi

echo "[init-firewall] LAN egress blocked (own subnet: ${OWN_SUBNET:-unknown})"
