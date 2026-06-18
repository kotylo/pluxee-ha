#!/usr/bin/env bash
set -e
cd ~
# Bootstrap pip + venv
if ! python3 -m pip --version >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    echo "using sudo apt"
    sudo -n apt-get update -qq
    sudo -n apt-get install -y -qq python3-pip python3-venv >/dev/null
  else
    echo "no passwordless sudo; bootstrapping pip via get-pip.py"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    python3 /tmp/get-pip.py --user --break-system-packages -q
  fi
fi
python3 -m pip --version
python3 -m pip install -q --user --break-system-packages \
  pytest-homeassistant-custom-component aioresponses 2>&1 | tail -6
echo "INSTALL_DONE"
python3 -c "from homeassistant.const import __version__; print('HA', __version__)"
cd /mnt/d/Projects/pluxee-ha
python3 -m pytest tests -q 2>&1 | tail -45
