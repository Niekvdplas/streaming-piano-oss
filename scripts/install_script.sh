#!/bin/bash
#
# Streaming Piano — Installation Script
#
# Run this from the cloned repository directory on the Jetson device:
#   sudo ./scripts/install_script.sh
#
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/dist"
SERVICE_DIR="$REPO_DIR/services"
SYSTEMD_DIR="/etc/systemd/system"

echo "==> Installing Streaming Piano from $REPO_DIR"

# ── System dependencies ──────────────────────────────────────────────

sudo apt-get update -y
sudo apt-get install -y curl gnupg apt-transport-https python3-pip python3-pyaudio \
    gfortran libopenblas-dev liblapack-dev libasound2-dev libssl-dev pkg-config

pip install --upgrade pip setuptools wheel

# ── RabbitMQ ─────────────────────────────────────────────────────────

curl -1sLf "https://keys.openpgp.org/vks/v1/by-fingerprint/0A9AF2115F4687BD29803A206B73A36E6026DFCA" \
    | sudo gpg --dearmor | sudo tee /usr/share/keyrings/com.rabbitmq.team.gpg > /dev/null
curl -1sLf https://github.com/rabbitmq/signing-keys/releases/download/3.0/cloudsmith.rabbitmq-erlang.E495BB49CC4BBE5B.key \
    | sudo gpg --dearmor | sudo tee /usr/share/keyrings/rabbitmq.E495BB49CC4BBE5B.gpg > /dev/null
curl -1sLf https://github.com/rabbitmq/signing-keys/releases/download/3.0/cloudsmith.rabbitmq-server.9F4587F226208342.key \
    | sudo gpg --dearmor | sudo tee /usr/share/keyrings/rabbitmq.9F4587F226208342.gpg > /dev/null

sudo tee /etc/apt/sources.list.d/rabbitmq.list <<EOF
deb [signed-by=/usr/share/keyrings/rabbitmq.E495BB49CC4BBE5B.gpg] https://ppa1.novemberain.com/rabbitmq/rabbitmq-erlang/deb/ubuntu jammy main
deb-src [signed-by=/usr/share/keyrings/rabbitmq.E495BB49CC4BBE5B.gpg] https://ppa1.novemberain.com/rabbitmq/rabbitmq-erlang/deb/ubuntu jammy main
deb [signed-by=/usr/share/keyrings/rabbitmq.9F4587F226208342.gpg] https://ppa1.novemberain.com/rabbitmq/rabbitmq-server/deb/ubuntu jammy main
deb-src [signed-by=/usr/share/keyrings/rabbitmq.9F4587F226208342.gpg] https://ppa1.novemberain.com/rabbitmq/rabbitmq-server/deb/ubuntu jammy main
EOF

sudo apt-get update -y
sudo apt-get install -y erlang-base \
    erlang-asn1 erlang-crypto erlang-eldap erlang-ftp erlang-inets \
    erlang-mnesia erlang-os-mon erlang-parsetools erlang-public-key \
    erlang-runtime-tools erlang-snmp erlang-ssl \
    erlang-syntax-tools erlang-tftp erlang-tools erlang-xmerl

sudo apt-get install -y rabbitmq-server --fix-missing
sudo rabbitmq-plugins enable rabbitmq_management

# ── PulseAudio null sink for Spotify ─────────────────────────────────

grep -q "module-null-sink sink_name=spotifySink" /etc/pulse/default.pa 2>/dev/null || \
    echo "load-module module-null-sink sink_name=spotifySink" | sudo tee -a /etc/pulse/default.pa
grep -q "module-null-sink sink_name=spotifySink" /etc/pulse/system.pa 2>/dev/null || \
    echo "load-module module-null-sink sink_name=spotifySink" | sudo tee -a /etc/pulse/system.pa

# ── PyTorch for Jetson (JetPack 5.x / L4T) ──────────────────────────

sudo pip install --no-cache \
    https://developer.download.nvidia.com/compute/redist/jp/v512/pytorch/torch-2.1.0a0+41361538.nv23.06-cp38-cp38-linux_aarch64.whl

# ── Deploy to /dist ──────────────────────────────────────────────────

sudo mkdir -p "$INSTALL_DIR"

# Copy application files
sudo cp "$REPO_DIR/listener.py"    "$INSTALL_DIR/"
sudo cp "$REPO_DIR/player.py"      "$INSTALL_DIR/"
sudo cp "$REPO_DIR/transcriber.py" "$INSTALL_DIR/"
sudo cp "$REPO_DIR/requirements.txt" "$INSTALL_DIR/"
sudo cp "$REPO_DIR/spotifyd"       "$INSTALL_DIR/"
sudo cp -r "$REPO_DIR/wifi"        "$INSTALL_DIR/"
sudo cp -r "$REPO_DIR/scripts/spotify.sh"        "$INSTALL_DIR/"
sudo cp -r "$REPO_DIR/scripts/pubshell.sh"        "$INSTALL_DIR/"
sudo cp -r "$REPO_DIR/scripts/check_internet.sh"  "$INSTALL_DIR/"

# Model weights must be placed manually — see README
if [ -d "$REPO_DIR/model" ]; then
    sudo cp -r "$REPO_DIR/model" "$INSTALL_DIR/"
else
    echo "WARNING: model/ directory not found — copy your model weights to $INSTALL_DIR/model/"
fi

sudo chmod +x "$INSTALL_DIR"/*.sh "$INSTALL_DIR/spotifyd" "$INSTALL_DIR/wifi/wifi-connect" 2>/dev/null || true

# ── Python packages ──────────────────────────────────────────────────

sudo pip install -r "$INSTALL_DIR/requirements.txt"

# ── PulseAudio user access ───────────────────────────────────────────

sudo adduser root pulse-access 2>/dev/null || true

# ── Install systemd services ─────────────────────────────────────────

for service_file in "$SERVICE_DIR"/*.service; do
    if [ -e "$service_file" ]; then
        sudo cp "$service_file" "$SYSTEMD_DIR/"
        sudo systemctl enable "$(basename "$service_file")"
    fi
done

sudo systemctl daemon-reload

echo ""
echo "==> Installation complete!"
echo "    Reboot to start all services: sudo reboot"

