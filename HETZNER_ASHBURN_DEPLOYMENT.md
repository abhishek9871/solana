---
name: Hetzner Ashburn deployment for solana_sniper.py
description: Bot runs on Hetzner CPX11 in Ashburn (87.99.151.70) as systemd service. Latency 9.4ms to ST origin vs 307ms from India. Full ops + redeploy commands here so future-me doesn't rediscover.
type: reference
originSessionId: 9f0ffaf2-5c05-4406-b96a-1a4dd646e86a
---
The trading bot `solana_sniper.py` runs in production on a Hetzner CPX11 cloud server in Ashburn, VA. This was deployed 2026-05-03 to escape the 307ms RTT tax from India. Verified RTT to Solana Tracker origin (162.120.18.106, NYC HOSTKEY): **9.4ms** — 32× faster than the user's home machine.

## Server identity

- **IPv4:** `87.99.151.70`  (IPv6: `2a01:4ff:f0:fe7f::1`)
- **Hetzner server name:** `solana-sniper`
- **Hetzner ID:** `128907278`
- **Type:** `cpx11`  (2 vCPU shared x86, 2 GB RAM, 40 GB SSD)  →  ~$4-5/mo
- **Location:** `ash` (Ashburn, VA — only x86 in this region; no ARM)
- **Image:** `ubuntu-22.04` (5.15 kernel, Python 3.11)
- **Default user:** `root` (key-only login; no password auth)

## Local tooling on user's Windows machine

- **hcloud CLI:** `/c/Users/VASU/hcloud-bin/hcloud.exe` (downloaded from `https://github.com/hetznercloud/cli/releases/latest/download/hcloud-windows-amd64.zip`)
- **SSH key:** `/c/Users/VASU/.ssh/hetzner_sniper` (private)  +  `.pub`. ed25519, no passphrase. Hetzner key name `laptop-sniper`, ID `111749571`.
- **API token:** the original one (`PWTvVqcG…`) was pasted in chat and should be revoked. Future ops: ask user for a fresh token, set as `export HCLOUD_TOKEN=…` and don't write it to disk.

## Server filesystem layout

```
/root/sniper/                      # repo clone
  ├── solana_sniper.py             # bot
  ├── active_snipers.txt           # 62-wallet active grad sniper pool (committed)
  ├── .env                         # secrets (NOT in git; scp'd from local)
  ├── venv/                        # Python 3.11 virtualenv with all deps
  └── sniper_v41_17.log            # systemd-redirected runtime log

/etc/systemd/system/solana-sniper.service   # service unit
/etc/logrotate.d/solana-sniper              # hourly rotate, keep 12, 100M cap
```

## systemd service (the one ground-truth way to run the bot)

Service: `solana-sniper.service`. Auto-starts on boot, auto-restarts on failure (10s delay), stdout+stderr appended to `sniper_v41_17.log`.

Key commands (run from local Windows via SSH):

```bash
SSH="ssh -i /c/Users/VASU/.ssh/hetzner_sniper root@87.99.151.70"

$SSH "systemctl status solana-sniper --no-pager"   # health
$SSH "systemctl restart solana-sniper"             # apply code/config change
$SSH "systemctl stop solana-sniper"                # halt
$SSH "systemctl start solana-sniper"               # start
$SSH "journalctl -u solana-sniper -n 50 --no-pager"  # recent stdout (also in sniper_v41_17.log)
$SSH "tail -f /root/sniper/sniper_v41_17.log"      # live tail
```

For a **monitor that streams to chat**, use the same Monitor pattern as local but over SSH:
```bash
ssh -i /c/Users/VASU/.ssh/hetzner_sniper root@87.99.151.70 \
  "tail -F /root/sniper/sniper_v41_17.log" | grep -E --line-buffered \
  "\*\*\* COPY TRADE \*\*\*|GRAD ENTRY|GRAD ABORT|CLOSED |SESSION:|COPY-PIPELINE|Traceback"
```

## Code-update redeploy flow

1. Edit `solana_sniper.py` locally, `git commit && git push origin main`
2. SSH in, pull, restart:

```bash
ssh -i /c/Users/VASU/.ssh/hetzner_sniper root@87.99.151.70 \
  "cd /root/sniper && git pull && systemctl restart solana-sniper && \
   sleep 5 && systemctl status solana-sniper --no-pager | head -10"
```

If new Python deps are needed:
```bash
ssh ... "cd /root/sniper && source venv/bin/activate && pip install <new-pkg> -q && systemctl restart solana-sniper"
```

If `.env` changed locally, scp it over BEFORE restart:
```bash
scp -i /c/Users/VASU/.ssh/hetzner_sniper \
  /c/Users/VASU/Desktop/tradingMahadevjiwin/.env \
  root@87.99.151.70:/root/sniper/.env
ssh ... "systemctl restart solana-sniper"
```

## Recreating the server from scratch (disaster recovery)

If the server gets nuked or you want a fresh one, the full flow is reproducible:

```bash
HCLOUD=/c/Users/VASU/hcloud-bin/hcloud.exe
export HCLOUD_TOKEN=<ask user for fresh token>

# 1. SSH key — already uploaded as 'laptop-sniper'. If gone:
$HCLOUD ssh-key create --name laptop-sniper --public-key-from-file /c/Users/VASU/.ssh/hetzner_sniper.pub

# 2. Provision server in Ashburn
$HCLOUD server create --name solana-sniper --type cpx11 --image ubuntu-22.04 --location ash --ssh-key laptop-sniper

# 3. Note the IPv4 from the output. Then on the new IP:
SERVER=root@<new-ip>
ssh -o StrictHostKeyChecking=accept-new -i /c/Users/VASU/.ssh/hetzner_sniper $SERVER \
  "apt-get update -qq && apt-get install -y python3.11 python3.11-venv python3-pip git curl"

# 4. Clone repo
ssh ... "cd /root && git clone https://github.com/abhishek9871/solana.git sniper"

# 5. SCP .env from local
scp -i /c/Users/VASU/.ssh/hetzner_sniper /c/Users/VASU/Desktop/tradingMahadevjiwin/.env $SERVER:/root/sniper/.env

# 6. venv + deps
ssh ... "cd /root/sniper && python3.11 -m venv venv && source venv/bin/activate && pip install --upgrade pip -q && pip install solana solders websockets requests base58 -q && python3.11 -m py_compile solana_sniper.py && echo COMPILE_OK"

# 7. Install systemd unit (use the inline cat-heredoc from the original deploy)
#    Service unit content (write to /etc/systemd/system/solana-sniper.service):
#       [Unit]
#       Description=Solana Sniper Bot (V41.17y, Ashburn)
#       After=network-online.target
#       Wants=network-online.target
#       [Service]
#       Type=simple
#       User=root
#       WorkingDirectory=/root/sniper
#       Environment=PYTHONUNBUFFERED=1
#       EnvironmentFile=/root/sniper/.env
#       ExecStart=/root/sniper/venv/bin/python3.11 /root/sniper/solana_sniper.py
#       Restart=on-failure
#       RestartSec=10
#       StandardOutput=append:/root/sniper/sniper_v41_17.log
#       StandardError=append:/root/sniper/sniper_v41_17.log
#       [Install]
#       WantedBy=multi-user.target
ssh ... "systemctl daemon-reload && systemctl enable --now solana-sniper"

# 8. Lock firewall to SSH only
ssh ... "ufw allow 22/tcp && ufw --force enable"
```

The full original deploy session ran end-to-end in ~10 minutes including server provisioning.

## Cost / billing

- Server: cpx11 in Ashburn = ~€3.79/mo (≈ $4/mo)
- Bandwidth: 20 TB/mo included. Bot uses well under 1 TB/mo.
- Hetzner billing is per-hour, project-based — destroy server with `hcloud server delete solana-sniper` to stop charges immediately.

## Why Ashburn (the latency justification)

Measured directly:
- ST gRPC origin (`162.120.18.106`) is in NYC (HOSTKEY).
- From user's India machine: `307ms` direct ping to origin.
- From this Ashburn server: `9.4ms` direct ping (intra-east-coast peering).
- Net: every Jupiter quote / `getAccountInfo` / `/top-traders` HTTP call is ~250-300ms faster.
- shredSubscribe WS latency stays ~50-150ms (already low — WS is persistent so RTT only matters at handshake).

Don't second-guess the move; the numbers proved it. If we ever consider moving back to local, re-measure first.

## Gotchas seen during deployment

1. `cx22` (the cheap Hetzner type from EU pricing pages) does **not exist in Ashburn**. Ashburn only has `cpx*` (shared x86) and `ccx*` (dedicated x86). No ARM/cax. Always check `hcloud server-type list` first.
2. Ubuntu 22.04 ships `python3.11` as `3.11.0rc1` — old but works fine. If a newer Python is needed, add the deadsnakes PPA.
3. `EnvironmentFile=/root/sniper/.env` in the systemd unit requires KEY=VALUE format (no `export`, no quotes around values with spaces). Our `.env` already follows this.
4. `tail -F` (capital F) survives log rotation; `-f` (lowercase) does not. Use `-F` for monitor scripts.
