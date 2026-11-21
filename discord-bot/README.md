# ARABI VPS Discord Bot

Discord control bot for the existing `~/arabi-vps/manager.sh` VPS manager.

## Expected host layout

```text
~/arabi-vps/
├── manager.sh
├── instances/
└── discord-bot/
    ├── bot.py
    ├── requirements.txt
    ├── .env
    └── arabi.db        # created automatically
```

## Setup

```bash
cd ~/arabi-vps/discord-bot
cp .env.example .env
nano .env
chmod +x start.sh
./start.sh
```

Set `DISCORD_TOKEN` and `OWNER_ID` in `.env`. The configured owner is automatically an admin.

## Manager interface expected

The bot calls these commands:

```bash
bash ~/arabi-vps/manager.sh list
bash ~/arabi-vps/manager.sh create NAME RAM_MB CPU DISK_GB
bash ~/arabi-vps/manager.sh start NAME
bash ~/arabi-vps/manager.sh stop NAME
bash ~/arabi-vps/manager.sh restart NAME
bash ~/arabi-vps/manager.sh delete NAME
```

The manager must return exit code 0 on success and non-zero on failure. VPS directories are expected under `~/arabi-vps/instances/<name>/` with a `config` file.

**Important:** This bot does not implement the KVM manager itself. It securely invokes the existing manager script using argument arrays; it does not execute arbitrary commands supplied by Discord users. Run it on the actual KVM host, not a development-only Codespace without `/dev/kvm` and libvirt access.
