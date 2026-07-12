# Phase 5 private HTTPS publishing

Phase 5 publishes the loopback-only interface at:

> `https://bobiverse.tail40344b.ts.net/`

It uses **Tailscale Serve**, not Tailscale Funnel. Serve terminates HTTPS on the tailnet address and proxies only to the protected Unix socket at `/run/game-server-interface/web/interface.sock`. The host never listens for the interface on a LAN address, loopback TCP port, public address, or Internet-routed port.

## Friend access

A friend can reach the website only after all of these conditions are met:

1. Their account and device are approved in the `hlotyaks.github` tailnet.
2. The tailnet access policy grants `autogroup:member` access to `100.84.161.38:443`.
3. The Phase 5 publishing service has been enabled successfully.
4. They use the HTTPS MagicDNS URL above while Tailscale is connected.

The policy must not grant SSH, Docker, controller-socket, database, or broad host access. Use [docs/tailscale-policy.example.hujson](tailscale-policy.example.hujson) as a pattern, replacing the placeholder friend addresses. Add only the game ports from an actually deployed catalog instance; no game port is needed for the website.

## Apply publishing configuration

1. In the Tailscale admin console, allow `autogroup:member` only `100.84.161.38:443`. Keep existing administrator-only SSH restrictions unchanged.
2. Review the policy and save it in the admin console.
3. On bobiverse, run:

        sudo bash ./scripts/install-phase5.sh

The installer enables trusted Tailscale identity handling in the local interface, removes the stale Internet-wide Enshrouded UDP firewall rules, installs a persistent `game-server-interface-serve.service`, configures private HTTPS Serve, and runs firewall validation.

`tailscale serve` stores its configuration in Tailscale state, so it persists across a reboot. The enabled systemd unit reasserts the intended private Serve route after `tailscaled` and the local interface start. It does not invoke `tailscale funnel` and it has no stop action that would erase the route during ordinary service shutdown.

## Identity and authorization boundary

Tailscale Serve removes any incoming `Tailscale-User-Login` header and writes the authenticated tailnet identity itself. With `TRUSTED_ACTOR_HEADER=1`, the interface records that header in controller audit events. The interface listens only on a socket inside a `root:game-interface-api` mode-`0770` directory; a direct LAN, tailnet, or ordinary local user cannot reach it and cannot inject the trusted header.

A local administrator process could still contact a loopback listener, so local host administration remains privileged. Do not grant shell access to friends and do not bind port `8080` beyond loopback.

## Verify and revoke

Run:

    tailscale serve status --json
    sudo bash ./scripts/validate-phase5-firewall.sh
    sudo curl --unix-socket /run/game-server-interface/web/interface.sock --fail http://localhost/healthz

From an approved friend device, open the HTTPS MagicDNS URL and verify that a control action is attributed to their Tailscale login in the root audit log. From an unapproved device, the URL must be denied by the tailnet policy. From outside the tailnet, the URL must be unreachable because Funnel remains disabled.

To revoke access, remove the user/device from the friend group or tailnet in the Tailscale admin console. The policy change takes effect without changing the website or controller configuration.
