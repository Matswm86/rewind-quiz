#!/usr/bin/env bash
# Deploy Rewind to https://quiz.mwmai.no. Run from your workstation; it needs
# SSH to the VPS as a user with sudo (you may be asked for the sudo password).
#
#   VPS=user@host ./deploy.sh
set -euo pipefail

VPS="${VPS:?set VPS=user@host, the SSH target of your server}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Tests"
(cd "$HERE/server" && python3 -m unittest test_server -q)

echo "==> Upload to $VPS"
ssh "$VPS" 'mkdir -p ~/services/mwm-quiz/data ~/services/mwm-quiz/site ~/.config/systemd/user'
rsync -az "$HERE/server/server.py" "$HERE/server/questions.json" \
  "$HERE/deploy/quiz.caddy" "$HERE/deploy/install_remote.sh" "$VPS:services/mwm-quiz/"
rsync -az --delete "$HERE/site/" "$VPS:services/mwm-quiz/site/"
rsync -az "$HERE/deploy/mwm-quiz.service" "$VPS:.config/systemd/user/"

echo "==> Install on the VPS"
ssh -t "$VPS" 'bash ~/services/mwm-quiz/install_remote.sh'

echo "==> Check https://quiz.mwmai.no (the first request fetches the TLS certificate)"
for _ in $(seq 1 15); do
  if curl -fsS https://quiz.mwmai.no/api/health; then
    echo
    echo "Live: https://quiz.mwmai.no"
    exit 0
  fi
  sleep 2
done
echo "The site did not answer yet. Check: ssh $VPS 'journalctl -u caddy -n 50 --no-pager'" >&2
exit 1
