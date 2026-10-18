#!/bin/bash
# Richtet insta-upload als launchd-Job (LaunchAgent) auf dem Mac ein.
#   ./launchd/install.sh            installieren bzw. aktualisieren
#   ./launchd/install.sh uninstall  entfernen
set -euo pipefail

LABEL="com.nordfriisk.insta-upload"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [[ "${1:-}" == "uninstall" ]]; then
    rm -f "$PLIST"
    echo "launchd-Job $LABEL entfernt."
    exit 0
fi

case "$DIR" in
    "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
        echo "Achtung: $DIR liegt in einem von macOS geschützten Ordner." >&2
        echo "launchd darf dort ohne Festplattenvollzugriff nicht lesen. Besser z. B. nach ~/insta-upload verschieben." >&2
        ;;
esac

if [[ ! -x "$DIR/venv/bin/python" ]]; then
    echo "Lege virtuelle Umgebung an ..."
    python3 -m venv "$DIR/venv"
    "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi
[[ -f "$DIR/.env" ]] || { echo "Fehlt: $DIR/.env (Vorlage: .env.example)" >&2; exit 1; }

mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
sed -e "s|__LABEL__|$LABEL|g" -e "s|__DIR__|$DIR|g" "$DIR/launchd/insta-upload.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "launchd-Job $LABEL läuft jetzt alle 5 Minuten."
echo "Log:    tail -f $DIR/logs/insta-upload.log"
echo "Status: launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
