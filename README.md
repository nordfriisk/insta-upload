# insta-upload

Ein schlankes Python-Skript, das geplante Instagram-Posts, Karussells, Reels und Stories
aus einer CSV-Datei liest und sie zum geplanten Zeitpunkt über die offizielle
[Instagram Graph API](https://developers.facebook.com/docs/instagram-platform/content-publishing)
veröffentlicht. Es läuft auf einem Mac mini per launchd (oder per Cron) und braucht außer
`requests` keine Abhängigkeiten. Den 60-Tage-Token verlängert es selbst.

Die Graph API lädt Medien nicht direkt hoch, sondern holt sie sich von einer öffentlichen URL.
Deshalb liegen die Bilder und Videos in einem öffentlich erreichbaren Ordner bei Infomaniak;
das Skript hängt nur den Dateinamen an `MEDIA_BASE_URL` an.

## Ablauf

1. Bild oder Video in den Infomaniak-Ordner legen.
2. Eine Zeile in `posts.csv` ergänzen.
3. launchd ruft alle 5 Minuten `insta_upload.py run` auf. Alles, was fällig und noch nicht
   veröffentlicht ist, geht raus. Der Status landet in `state.db` (SQLite).

## Einrichtung

Auf dem Mac mini (Python 3.10 oder neuer, z. B. `brew install python`):

```bash
git clone https://github.com/nordfriisk/insta-upload.git ~/insta-upload
cd ~/insta-upload
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env            # Werte eintragen
cp posts.example.csv posts.csv  # Zeitplan pflegen
./venv/bin/python insta_upload.py check
./launchd/install.sh            # alle 5 Minuten automatisch starten
```

Lege das Repo nicht in `~/Documents`, `~/Desktop` oder `~/Downloads`: Dort darf ein
launchd-Job wegen der macOS-Datenschutzregeln ohne „Festplattenvollzugriff“ nichts lesen.

### Voraussetzungen bei Meta

- Ein **Instagram Business- oder Creator-Konto**.
- Eine **Meta-App** unter <https://developers.facebook.com/apps> mit dem Produkt „Instagram“
  und der Berechtigung `instagram_content_publish` (bzw. `instagram_business_content_publish`
  beim Instagram-Login).
- Die **Instagram-User-ID** (`IG_USER_ID`) und ein **Zugriffstoken** (`IG_ACCESS_TOKEN`).

Standard ist der **Instagram-Login** über `https://graph.instagram.com` (keine Facebook-Seite nötig):

1. In der App unter **Instagram › API setup with Instagram login** das Konto hinzufügen und
   **Generate token** klicken. Das ergibt einen Langzeit-Token (60 Tage) → `IG_ACCESS_TOKEN`.
2. User-ID abfragen → `IG_USER_ID`:
   ```bash
   curl "https://graph.instagram.com/me?fields=user_id,username&access_token=TOKEN"
   ```

### Automatische Token-Verlängerung

Bei jedem `run` prüft das Skript, ob die letzte Verlängerung mindestens `TOKEN_REFRESH_DAYS`
(Standard 7) Tage her ist, und ruft dann `GET /refresh_access_token?grant_type=ig_refresh_token`
auf. Der neue Token wird in `state.db` gespeichert (Dateirechte `600`) und ab dann verwendet;
der Token in der `.env` bleibt nur der Ausgangswert. Instagram verlängert erst Tokens, die
mindestens 24 Stunden alt sind. Schlägt ein Versuch fehl, versucht das Skript es höchstens
einmal pro Stunde erneut und veröffentlicht mit dem bisherigen Token weiter.

Ist der Token doch einmal abgelaufen (z. B. weil der Mac zwei Monate aus war), erzeugst du in
der Meta-App einen neuen und trägst ihn in die `.env` ein. Das Skript erkennt den geänderten
Token und ersetzt den gespeicherten. Sofort verlängern: `insta_upload.py refresh-token`.
`list` zeigt, bis wann der Token gilt; ab 7 Tagen vor Ablauf steht eine Warnung im Log.

Wer stattdessen den Facebook-Login nutzt, setzt `GRAPH_HOST=https://graph.facebook.com`;
dort entfällt die automatische Verlängerung (ein System-User-Token läuft nicht ab).

## Zeitplan: `posts.csv`

| Spalte | Bedeutung |
| --- | --- |
| `id` | Eindeutige Kennung. Darüber merkt sich das Skript, was schon veröffentlicht ist. Nicht nachträglich ändern. |
| `publish_at` | `2026-10-20 18:30` (in `TIMEZONE`, Standard `Europe/Berlin`) oder ISO mit Offset. |
| `type` | `post`, `story` oder `reel`. |
| `media` | Dateiname(n) relativ zu `MEDIA_BASE_URL`, mehrere mit `\|` getrennt. Volle `https://`-URLs gehen auch. |
| `caption` | Bildunterschrift inkl. Hashtags (max. 2200 Zeichen). Bei Stories wird sie ignoriert. |

So wird die Zeile umgesetzt:

- `post` mit einem Bild → normaler Bild-Post
- `post` mit einem Video → Reel, das auch im Feed erscheint (Instagram kennt keine reinen Feed-Videos mehr)
- `post` mit 2–10 Dateien → Karussell (Bilder und Videos gemischt möglich)
- `story` → Story mit genau einem Bild oder Video
- `reel` → Reel mit genau einem Video

Unterstützte Formate: `.jpg`, `.jpeg`, `.png`, `.mp4`, `.mov`. Instagram selbst akzeptiert für
Bilder zuverlässig nur JPEG; Seitenverhältnisse und Videolängen prüft die API.

## Befehle

```bash
python insta_upload.py run            # fällige Beiträge veröffentlichen (für launchd/Cron)
python insta_upload.py run --dry-run  # nur anzeigen, was veröffentlicht würde
python insta_upload.py list           # Zeitplan mit Status
python insta_upload.py check          # CSV prüfen und alle Medien-URLs per HEAD abrufen
python insta_upload.py retry <id>     # fehlgeschlagenen Beitrag erneut versuchen
python insta_upload.py refresh-token  # Zugriffstoken sofort verlängern
```

Mit `--env /pfad/.env` lässt sich eine andere Konfiguration nutzen; relative Pfade in der
`.env` beziehen sich auf deren Ordner.

## Mac mini (launchd)

`./launchd/install.sh` erzeugt `~/Library/LaunchAgents/com.nordfriisk.insta-upload.plist`
aus der Vorlage in `launchd/` und lädt den Job. Er startet `insta_upload.py run` beim Anmelden
und danach alle 5 Minuten. Nach Änderungen am Repo einfach erneut aufrufen;
`./launchd/install.sh uninstall` entfernt den Job.

```bash
tail -f ~/insta-upload/logs/insta-upload.log                              # Log
launchctl print gui/$(id -u)/com.nordfriisk.insta-upload | grep -E 'state|last exit'
launchctl kickstart gui/$(id -u)/com.nordfriisk.insta-upload              # sofort ausführen
```

Damit der Job zuverlässig läuft:

- Ein LaunchAgent läuft nur, solange du am Mac angemeldet bist. Für einen Mac mini ohne
  Bildschirm: **Systemeinstellungen › Benutzer & Gruppen › Automatisch anmelden**.
- Im Ruhezustand läuft nichts; launchd holt den Lauf nach dem Aufwachen nach. Wenn Beiträge
  pünktlich rausgehen sollen: **Systemeinstellungen › Energie › Automatischen Ruhezustand
  verhindern** einschalten (oder `sudo pmset -a sleep 0`).

### Alternativ: Cron

```cron
*/5 * * * * cd /home/stefan/insta-upload && ./venv/bin/python insta_upload.py run >> logs/insta-upload.log 2>&1
```

Ein Lock verhindert, dass sich zwei Läufe überschneiden (z. B. während ein Video verarbeitet wird).
Schlägt ein Beitrag fehl, versucht das Skript es bei den nächsten Läufen erneut, insgesamt
`MAX_ATTEMPTS`-mal (Standard 3). Danach bleibt er auf „fehlgeschlagen“, bis du `retry` aufrufst.

Ein Beitrag, dessen Zeitpunkt schon vorbei ist (z. B. weil der Rechner aus war), wird beim
nächsten Lauf nachgeholt.

## Status per SQL

```sql
SELECT id, status, attempts, media_id, last_error, updated_at
FROM publications
ORDER BY scheduled_at DESC;
```

## Grenzen der API

- Höchstens 100 per API veröffentlichte Beiträge in 24 Stunden pro Konto.
- Personen-Tags, Musik, Sticker und Story-Links lassen sich über die API nicht setzen.
- Die Medien müssen während der Verarbeitung öffentlich abrufbar bleiben.

## Tests

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest
```
