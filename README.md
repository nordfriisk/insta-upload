# insta-upload

Ein schlankes Python-Skript, das geplante Instagram-Posts, Karussells, Reels und Stories
aus einer CSV-Datei liest und sie zum geplanten Zeitpunkt über die offizielle
[Instagram Graph API](https://developers.facebook.com/docs/instagram-platform/content-publishing)
veröffentlicht. Es läuft per Cron und braucht außer `requests` keine Abhängigkeiten.

Die Graph API lädt Medien nicht direkt hoch, sondern holt sie sich von einer öffentlichen URL.
Deshalb liegen die Bilder und Videos in einem öffentlich erreichbaren Ordner bei Infomaniak;
das Skript hängt nur den Dateinamen an `MEDIA_BASE_URL` an.

## Ablauf

1. Bild oder Video in den Infomaniak-Ordner legen.
2. Eine Zeile in `posts.csv` ergänzen.
3. Cron ruft alle 5 Minuten `insta_upload.py run` auf. Alles, was fällig und noch nicht
   veröffentlicht ist, geht raus. Der Status landet in `state.db` (SQLite).

## Einrichtung

```bash
git clone https://github.com/nordfriisk/insta-upload.git
cd insta-upload
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env            # Werte eintragen
cp posts.example.csv posts.csv  # Zeitplan pflegen
./venv/bin/python insta_upload.py check
```

### Voraussetzungen bei Meta

- Ein **Instagram Business- oder Creator-Konto**.
- Eine **Meta-App** unter <https://developers.facebook.com/apps> mit dem Produkt „Instagram“
  und der Berechtigung `instagram_content_publish` (bzw. `instagram_business_content_publish`
  beim Instagram-Login).
- Die **Instagram-User-ID** (`IG_USER_ID`) und ein **Zugriffstoken** (`IG_ACCESS_TOKEN`).

Zwei Wege zum Token:

| Weg | `GRAPH_HOST` | Hinweis |
| --- | --- | --- |
| Instagram-Login (ohne Facebook-Seite) | `https://graph.instagram.com` | Langzeit-Token gilt 60 Tage und muss vorher erneuert werden (`GET /refresh_access_token`). |
| Facebook-Login (Konto mit Facebook-Seite verknüpft) | `https://graph.facebook.com` | Am bequemsten ist ein **System-User-Token** aus dem Business Manager, das nicht abläuft. |

Läuft das Token ab, schlagen die Läufe mit `OAuthException (190)` fehl; das steht dann in `list`.

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
python insta_upload.py run            # fällige Beiträge veröffentlichen (für Cron)
python insta_upload.py run --dry-run  # nur anzeigen, was veröffentlicht würde
python insta_upload.py list           # Zeitplan mit Status
python insta_upload.py check          # CSV prüfen und alle Medien-URLs per HEAD abrufen
python insta_upload.py retry <id>     # fehlgeschlagenen Beitrag erneut versuchen
```

Mit `--env /pfad/.env` lässt sich eine andere Konfiguration nutzen; relative Pfade in der
`.env` beziehen sich auf deren Ordner.

## Cron

```cron
*/5 * * * * cd /home/stefan/insta-upload && ./venv/bin/python insta_upload.py run >> run.log 2>&1
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
