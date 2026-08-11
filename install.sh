#!/bin/sh
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (sudo ./install.sh)"
    exit 1
fi

echo "Checking dependencies..."

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is not installed."
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is not installed."
    exit 1
fi

echo "Installing qvnp..."
rm -rf /usr/local/bin/qvnp
curl -fsSL https://raw.githubusercontent.com/quadakr/qvnp/main/qvnp.py \
    -o /usr/local/bin/qvnp
chmod +x /usr/local/bin/qvnp

echo "Linking qp -> qvnp..."
rm -f /usr/local/bin/qp
ln -s /usr/local/bin/qvnp /usr/local/bin/qp

echo "qvnp installed to /usr/local/bin/qvnp"
echo "Short alias 'qp' installed to /usr/local/bin/qp"
echo "Run: qvnp (or: qp)"
