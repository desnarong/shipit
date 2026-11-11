#!/usr/bin/env bash
# shipit installer for Linux servers (Debian/Ubuntu, Fedora/RHEL, Alpine is not supported).
#
#   curl -fsSL https://raw.githubusercontent.com/desnarong/shipit/main/install.sh | sudo bash
#
# What it does:
#   1. installs git, python3 (+venv) and Docker if they are missing
#   2. installs shipit into /opt/shipit and links it to /usr/local/bin/shipit
#   3. creates a `shipit` system user (in the docker group) with its data in /var/lib/shipit
#   4. installs and starts a systemd service that listens for webhooks on port 9000
#
# Options: --dry-run (print what would happen), --no-docker (skip installing Docker),
#          --no-service (skip systemd), --from-git (install from GitHub instead of PyPI)
set -euo pipefail

DRY=0; NO_DOCKER=0; NO_SERVICE=0; FROM_GIT=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --no-docker) NO_DOCKER=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --from-git) FROM_GIT=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
run() { echo "+ $*"; if [ "$DRY" = 0 ]; then "$@"; fi; }

if [ "$DRY" = 0 ]; then
  [ "$(id -u)" = 0 ] || { echo "Please run as root: curl ... | sudo bash" >&2; exit 1; }
  [ "$(uname -s)" = "Linux" ] || { echo "This installer supports Linux only." >&2; exit 1; }
fi

if   command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
elif command -v yum     >/dev/null 2>&1; then PKG=yum
else PKG=unknown; fi

install_pkgs() {
  case "$PKG" in
    apt) run apt-get update -y; run apt-get install -y "$@" ;;
    dnf) run dnf install -y "$@" ;;
    yum) run yum install -y "$@" ;;
    *)   echo "Unsupported package manager. Install manually: $*" >&2; [ "$DRY" = 1 ] || exit 1 ;;
  esac
}

say "Checking prerequisites"
need=()
command -v git     >/dev/null 2>&1 || need+=(git)
command -v curl    >/dev/null 2>&1 || need+=(curl)
command -v python3 >/dev/null 2>&1 || need+=(python3)
if [ "$PKG" = apt ]; then
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || need+=(python3-venv)
fi
if [ "${#need[@]}" -gt 0 ]; then install_pkgs "${need[@]}"; fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || {
  echo "shipit needs Python 3.11+ (found: $(python3 --version 2>&1)). Install a newer Python and re-run." >&2
  [ "$DRY" = 1 ] || exit 1
}

if [ "$NO_DOCKER" = 0 ] && ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (official script from get.docker.com)"
  if [ "$DRY" = 1 ]; then echo "+ curl -fsSL https://get.docker.com | sh"; else curl -fsSL https://get.docker.com | sh; fi
fi
if command -v docker >/dev/null 2>&1; then run systemctl enable --now docker || true; fi

say "Installing shipit"
run python3 -m venv /opt/shipit
if [ -n "${SHIPIT_SRC:-}" ]; then SRC="$SHIPIT_SRC"   # local path/URL override (used by tests)
elif [ "$FROM_GIT" = 1 ]; then SRC="git+https://github.com/desnarong/shipit.git"
else SRC="shipit-deploy"; fi
run /opt/shipit/bin/pip install --upgrade pip "$SRC"
run ln -sf /opt/shipit/bin/shipit /usr/local/bin/shipit

say "Creating the shipit user and data directory"
id shipit >/dev/null 2>&1 || run useradd --system --create-home --home-dir /var/lib/shipit --shell /usr/sbin/nologin shipit
run mkdir -p /var/lib/shipit
if getent group docker >/dev/null 2>&1; then run usermod -aG docker shipit; fi
run chown -R shipit: /var/lib/shipit

# Makes `shipit ...` use the service's config from any directory, for every login shell.
if [ "$DRY" = 1 ]; then
  echo "+ write /etc/profile.d/shipit.sh (export SHIPIT_CONFIG=/var/lib/shipit/shipit.toml)"
else
  printf 'export SHIPIT_CONFIG=/var/lib/shipit/shipit.toml\n' > /etc/profile.d/shipit.sh
fi

if [ "$NO_SERVICE" = 0 ]; then
  say "Installing the systemd service"
  UNIT='[Unit]
Description=shipit auto deploy (webhook server)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=shipit
WorkingDirectory=/var/lib/shipit
Environment=SHIPIT_CONFIG=/var/lib/shipit/shipit.toml
# Wait until the first `shipit up` has created the config, then serve.
ExecStart=/bin/sh -c '"'"'while [ ! -f "$SHIPIT_CONFIG" ]; do sleep 5; done; exec /usr/local/bin/shipit serve'"'"'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
'
  if [ "$DRY" = 1 ]; then
    echo "+ write /etc/systemd/system/shipit.service"
  else
    printf '%s' "$UNIT" > /etc/systemd/system/shipit.service
  fi
  run systemctl daemon-reload
  run systemctl enable --now shipit
fi

cat <<'EOF'

shipit is installed.

Deploy your first app (run as the shipit user so files and Docker access line up):

  sudo -u shipit SHIPIT_CONFIG=/var/lib/shipit/shipit.toml shipit up https://github.com/you/your-app.git

Then turn on auto deploy for a GitHub repo (needs the `gh` CLI, logged in):

  shipit hook your-app --url http://YOUR-SERVER-IP:9000

Open port 9000 in your firewall for webhooks, or put Caddy/nginx with HTTPS in front of it.
Check the setup any time with:  sudo -u shipit shipit doctor
EOF
