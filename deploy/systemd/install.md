# Installing VectorStep-Gateway on a VM (systemd)

FHS-ish layout, identical in shape to VectorStep's
(`../../../VectorStep/deploy/systemd/install.md`):

| What | Where |
|---|---|
| Code (checkout + venv) | `/opt/vectorstep/vectorstep-gateway/` |
| Config | `/etc/vectorstep/vectorstep-gateway/` |
| State (identity, agents) | `/var/lib/vectorstep/vectorstep-gateway/` |
| Logs | `/var/log/vectorstep/vectorstep-gateway/` |
| Secrets | `/etc/vectorstep/vectorstep-gateway/env` (root:vectorstep, mode 640) |

## Install

1. Create the system user (share it with VectorStep if both run on the same
   host; harmless if it already exists):
   ```sh
   sudo useradd -r -s /usr/sbin/nologin -d /opt/vectorstep/vectorstep-gateway vectorstep || true
   ```

2. Clone the repo and create the venv:
   ```sh
   sudo mkdir -p /opt/vectorstep
   sudo git clone https://github.com/bantex01/VectorStep-Gateway.git /opt/vectorstep/vectorstep-gateway
   cd /opt/vectorstep/vectorstep-gateway
   sudo python3 -m venv .venv
   sudo .venv/bin/pip install -r requirements.txt
   ```

3. Install Node.js 22 LTS — the gateway spawns MCP servers as subprocesses
   and the documented config uses `npx -y <package>`:
   ```sh
   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
   sudo apt-get install -y nodejs
   ```

4. Create the config/state/log trees with ownership:
   ```sh
   sudo mkdir -p /etc/vectorstep/vectorstep-gateway
   sudo mkdir -p /var/lib/vectorstep/vectorstep-gateway/{identity,agents}
   sudo mkdir -p /var/log/vectorstep/vectorstep-gateway
   sudo chown -R vectorstep:vectorstep /opt/vectorstep/vectorstep-gateway \
     /var/lib/vectorstep/vectorstep-gateway /var/log/vectorstep/vectorstep-gateway
   sudo chown -R root:vectorstep /etc/vectorstep/vectorstep-gateway
   ```

5. Copy the sample config and point its writable paths at `/var/lib` and `/var/log`:
   ```sh
   sudo cp samples/config.yaml.example /etc/vectorstep/vectorstep-gateway/config.yaml
   sudo $EDITOR /etc/vectorstep/vectorstep-gateway/config.yaml
   ```
   Set at minimum:
   ```yaml
   agents_dir: /var/lib/vectorstep/vectorstep-gateway/agents
   identity:
     path: /var/lib/vectorstep/vectorstep-gateway/identity
   logging:
     dir: /var/log/vectorstep/vectorstep-gateway
   ```
   **This is not optional.** The unit sets `ProtectHome=true`, which breaks
   the default `~/.vectorstep-gateway/identity` path outright — `identity.path`
   must point into `/var/lib/vectorstep/vectorstep-gateway/` or the service
   won't be able to read or write its identity files.
   (See `../../samples/config.yaml.example` for the full annotated reference
   — MCP servers, LLM providers, tool policy, etc.)

6. Create the env file with your secrets:
   ```sh
   sudo cp env.example /etc/vectorstep/vectorstep-gateway/env
   sudo $EDITOR /etc/vectorstep/vectorstep-gateway/env
   sudo chown root:vectorstep /etc/vectorstep/vectorstep-gateway/env
   sudo chmod 640 /etc/vectorstep/vectorstep-gateway/env
   ```

7. Install and start the unit:
   ```sh
   sudo cp deploy/systemd/vectorstep-gateway.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now vectorstep-gateway
   sudo systemctl status vectorstep-gateway
   ```

8. Verify, and fetch the operator token VectorStep needs
   (`executors.gateway.token` in VectorStep's config — see
   `../../../VectorStep/deploy/systemd/install.md`):
   ```sh
   curl -s http://localhost:18780/health
   sudo cat /var/lib/vectorstep/vectorstep-gateway/identity/device-auth.json
   ```

## Upgrade

```sh
cd /opt/vectorstep/vectorstep-gateway
sudo -u vectorstep git pull
sudo -u vectorstep .venv/bin/pip install -r requirements.txt
sudo systemctl restart vectorstep-gateway
```

For a **YAML-only** change (editing `config.yaml`, e.g. adding an agent),
`reload` is enough — the Gateway already implements SIGHUP reload for agents:

```sh
sudo systemctl reload vectorstep-gateway
journalctl -u vectorstep-gateway -n 50   # confirm the reload log line
```
