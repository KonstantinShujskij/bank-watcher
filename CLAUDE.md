# bank-watcher

Active branch: `main`. Standalone Python/FastAPI service that monitors bank **jar/збір** links (monobank + PUMB + **Privat24** via headless Chromium/Playwright) and auto-confirms ncP2P deposits by detecting top-ups (balance-delta). SQLite + NordVPN. Part of the ncp ecosystem.

- **Server:** `ssh bank-watcher` → `206.188.197.2` (Ubuntu 26.04, 2 vCPU/2 GB; migrated May 2026 from `168.100.8.94`), code at `/opt/bank-watcher`, systemd `bank-watcher.service`.
- **Local path:** `ncp/ncp2p/bank-watcher`.

## Always read first

This repo's working knowledge lives in the shared knowledge base **[`KonstantinShujskij/ncp-knowledge`](https://github.com/KonstantinShujskij/ncp-knowledge)** (private).

Recommended local clone: `../../../ncp-knowledge` (i.e. `ncp/ncp-knowledge`). If not present:

```bash
git clone git@github.com:KonstantinShujskij/ncp-knowledge.git ../../../ncp-knowledge
```

Before doing any work, **read these**:

- `ncp-knowledge/bank-watcher/README.md` — service overview, how detection works, API, auth
- `ncp-knowledge/bank-watcher/integration.md` — the ncP2P contract (subscribe, signed callback, matching, BankCredit, dashboard, config)
- `ncp-knowledge/bank-watcher/deployment.md` — server, systemd, NordVPN reactive exit-search, deploy recipe
- `ncp-knowledge/shared/glossary.md` — Банка/jar, Invoice, Payment, BankCredit
- `ncp-knowledge/shared/ssh.md` — SSH alias convention (`bank-watcher` → 168.100.8.94)

## Quick orientation (cheat sheet)

- **Detection is balance-delta only** — the Monobank jar API exposes only aggregates (no per-tx, no txId). Two top-ups in one 1s poll merge. Amounts in kopecks. Dedup id = `sha256(jarRef + cumulative balance)`.
- **ncP2P owns matching/confirm**; this service only sources credits and fires a signed callback.
- **Signing:** inbound (ncP2P→here) = raw-body HMAC `X-Signature` (`INBOUND_SECRET`); outbound callback = canonical-field HMAC in body `signature` (`CALLBACK_SECRET`).
- **NordVPN:** monobank 403s many VPN exits → reactive exit-search (probe, keep working exit, re-search only on poller errors, fallback to direct). Allowlist ports 22 + 8080. Toggle with `VPN_ENABLED`.
- **Config:** server `.env` (gitignored). Web UI behind a session login (`AUTH_USER`/`AUTH_PASSWORD`).
- Deploy: `git pull && .venv/bin/pip install -r requirements.txt && systemctl restart bank-watcher`.

If anything here conflicts with the knowledge base, the knowledge base wins.
