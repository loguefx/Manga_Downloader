# Manga Downloader for Jellyfin

Automatically downloads new English manga chapters from MangaDex and saves them as `.cbz` files to your NAS — ready for Jellyfin (or Kavita / Komga).

---

## How it works

1. You add manga to `config.yaml` by their **MangaDex UUID**.
2. The service calls the MangaDex v5 API and filters for **English** (`translatedLanguage[]=en`) chapters only — no guessing needed from country flags.
3. Each chapter is downloaded and packaged as a **CBZ** file (a ZIP of images that Jellyfin can read natively).
4. A `state.json` file remembers what was already downloaded so only **new** chapters are grabbed on subsequent runs.
5. It runs on a configurable interval (default: every 6 hours) as a daemon, or you can trigger it manually / via Task Scheduler.

---

## Requirements

- Python 3.11 or later
- pip

---

## Setup

### 1 — Install dependencies

```powershell
pip install -r requirements.txt
```

### 2 — Edit `config.yaml`

```yaml
nas_path: "//NAS/Media/Manga"    # Path to your NAS share or local folder
check_interval_hours: 6
language: "en"                   # English — change to "ja", "fr", etc. if needed

manga:
  - id: "a77742b1-befd-49a4-bff5-1ad4e6b0ef7b"
    name: "One Punch Man"
  - id: "32d76d19-8a05-4db0-9fc2-e0b0648fe9d0"
    name: "Solo Leveling"
```

**Finding a manga's UUID:** Open it on [MangaDex](https://mangadex.org) — the UUID is in the URL:
```
https://mangadex.org/title/a77742b1-befd-49a4-bff5-1ad4e6b0ef7b/one-punch-man
                            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ this part
```

### 3 — Point Jellyfin at the NAS folder

In Jellyfin, add a new **Books** library pointing at your `nas_path`. Jellyfin will pick up `.cbz` files automatically. If you want a dedicated manga reader interface, [Kavita](https://www.kavitareader.com/) or [Komga](https://komga.org/) are great alternatives that run alongside Jellyfin.

---

## Running the downloader

### Run once (manual / Task Scheduler)

```powershell
python main.py --run-once
```

### Run as a daemon (stays open, checks on schedule)

```powershell
python main.py
```

Press `Ctrl+C` to stop.

### Add a manga from a URL

```powershell
python main.py --add "https://mangadex.org/title/a77742b1-befd-49a4-bff5-1ad4e6b0ef7b/one-punch-man"
```

This fetches the title automatically and adds it to `config.yaml`.

---

## Setting up automatic runs with Windows Task Scheduler

1. Open **Task Scheduler** → **Create Basic Task…**
2. Name it "Manga Downloader"
3. Trigger: **Daily** (or your preferred interval)
4. Action: **Start a program**
   - Program: `python`
   - Arguments: `"C:\Users\Logan\Documents\Manga-Downloader-Jellyfin\main.py" --run-once`
   - Start in: `C:\Users\Logan\Documents\Manga-Downloader-Jellyfin`
5. Finish. It will run silently in the background and log to `manga_downloader.log`.

---

## File layout on NAS

```
<nas_path>/
  One Punch Man/
    One Punch Man - Chapter 001.cbz
    One Punch Man - Chapter 002.cbz
    …
  Solo Leveling/
    Solo Leveling - Chapter 001.cbz
    …
```

---

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `nas_path` | `"D:/Manga"` | Where to save manga |
| `check_interval_hours` | `6` | How often to poll (daemon mode) |
| `language` | `"en"` | ISO 639-1 language code |
| `image_quality` | `"data"` | `"data"` = original, `"data-saver"` = compressed |
| `page_delay_seconds` | `0.5` | Delay between page downloads |
| `chapter_delay_seconds` | `2` | Delay between chapters |
| `max_chapters_per_run` | `10` | Cap per manga per run (0 = unlimited) |

---

## State file

`state.json` is auto-created and tracks the last downloaded chapter number per manga ID. Delete it (or a specific entry inside it) to re-download everything.

---

## Logs

Activity is logged to both the console and `manga_downloader.log` in the project folder.
