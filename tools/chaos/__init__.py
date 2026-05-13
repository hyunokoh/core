"""Chaos engineering toolkit for the zkCEX demo stack.

Modules:
  service_registry   port -> safety tier -> restart command
  kill_loop          random service killer (chaos monkey)
  supervisor         background watchdog that restarts dead services
  network_partition  emit pfctl / iptables commands to isolate a port
  clock_skew         verify HMAC recvWindow rejection
  data_corruption    evil proxy that flips random bytes in responses

Kill switch (works for every script in this package):
    KILL_CHAOS=1 pkill -f tools/chaos
"""
