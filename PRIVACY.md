# Privacy Policy for SimSync (Sims4 Multiplayer Mod)

**Last Updated:** 2026-10

SimSync is a self-developed multiplayer mod for The Sims 4 (game-side Python script +
custom TCP protocol + Windows launcher). We uphold strict local-first and
zero-telemetry data protection principles.

## 1. Local Processing & Data Ownership

- All mod logic, state synchronization, and save handling execute **100% locally**
  inside the user's own game client.
- Your game saves, player data, and session logs **never leave your machine** and
  remain the sole property of the user.

## 2. Network Boundaries

- **Zero Telemetry:** No analytics SDKs, tracking pixels, crash beacons, or
  phone-home routines exist in this codebase.
- The only outbound connections are to the **host address you explicitly designate**
  (LAN peer or a host you run yourself), using the self-developed TCP/JSON protocol.
- No credentials, machine identifiers, or session data are transmitted to any
  third-party or central server — there is no central server.

## 3. Credential Handling

- Room passwords and session tokens are kept in local configuration only, are never
  bundled into distributed artifacts, and are never committed to version control.

## 4. Contact & Disclosure

For security issues or questions regarding this policy, refer to
[SECURITY.md](SECURITY.md) or open an issue at
https://github.com/mo9652962-ai/sims4-multiplayer-dev
