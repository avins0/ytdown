# ytdown

A local YouTube and YouTube Music downloader UI for saving videos, songs, or playlists as MP4, or audio as MP3.

Only download content that you own, created, or have permission to save.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python.exe app.py
```

Or use the launcher:

```powershell
.\run.ps1
```

For the most reliable double-click launch on Windows, use `run.bat`.

Open the URL printed in the terminal, usually:

```text
http://127.0.0.1:8765
```

The launcher opens the page automatically. The power button in the page shuts down the local server.

The default output folder is `downloads` inside this project. You can also enter an absolute Windows folder path in the UI.

## Spotify Links

The Spotify area accepts song and playlist links and uses `spotdl` to save MP3 files with metadata. It does not download Spotify's protected audio stream; spotDL uses Spotify metadata to find matching audio from YouTube or YouTube Music. Only save music you own or have permission to keep locally.

## Notes

- YouTube Music song and playlist links are supported, including `music.youtube.com/watch` and `music.youtube.com/playlist`.
- MP4 downloads use the best MP4 video and M4A audio that YouTube exposes, then merge to MP4.
- MP3 downloads use `yt-dlp` with FFmpeg and embeds available metadata. The `imageio-ffmpeg` package supplies an FFmpeg binary if one is not already installed on PATH.
- Playlist mode asks `yt-dlp` to download every item in the playlist URL. Direct playlist links are detected automatically; leave playlist mode off for a single song or video from a playlist page.
