#!/usr/bin/env bash
# One-time setup on Ubuntu 20.04 / 22.04 / 24.04 (run from the project root).
set -e
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-pip python3-venv python3-matplotlib iperf3 git

# SDN controller library. Ryu does not install on Python >= 3.10 without pins,
# so we use os-ken (the maintained Ryu fork); the code supports both.
PYV=$(python3 -c 'import sys; print("%d%d" % sys.version_info[:2])')
if [ "$PYV" -ge 312 ]; then
  sudo pip3 install --break-system-packages os-ken
else
  sudo pip3 install os-ken
fi

python3 tools/manage_users.py init-demo
sudo mn -c
echo
echo "Setup done. Next:"
echo "  terminal 1:  python3 controller/run_controller.py"
echo "  terminal 2:  sudo python3 topology/topo.py"
