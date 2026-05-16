from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DEFAULT_DOWNLOAD_DIR = APP_ROOT / "downloads"
DEFAULT_SPOTIFY_DOWNLOAD_DIR = DEFAULT_DOWNLOAD_DIR / "spotify"

JOBS_LOCK = threading.RLock()
JOBS: dict[str, "DownloadJob"] = {}


class DownloadCancelled(Exception):
    pass


@dataclass
class DownloadJob:
    id: str
    url: str
    media_type: str
    playlist: bool
    output_dir: str
    source: str = "youtube"
    status: str = "queued"
    title: str = ""
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    filename: str = ""
    message: str = "Waiting to start"
    error: str = ""
    cancel_requested: bool = False
    process: subprocess.Popen[str] | None = field(default=None, repr=False, compare=False)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    logs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "mediaType": self.media_type,
            "playlist": self.playlist,
            "outputDir": self.output_dir,
            "source": self.source,
            "status": self.status,
            "title": self.title,
            "progress": round(self.progress, 2),
            "speed": self.speed,
            "eta": self.eta,
            "filename": self.filename,
            "message": self.message,
            "error": self.error,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "logs": self.logs[-80:],
        }


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: SimpleHTTPRequestHandler, status: int, body: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = time.time()


def add_log(job_id: str, message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.logs.append(f"[{timestamp}] {message}")
        job.updated_at = time.time()


def get_job(job_id: str) -> DownloadJob | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def list_jobs() -> list[dict[str, Any]]:
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda item: item.created_at, reverse=True)
        return [job.to_dict() for job in jobs]


def cancel_active_jobs(message: str) -> None:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.status not in {"complete", "error", "cancelled"}:
                job.cancel_requested = True
                job.message = message
                if job.process and job.process.poll() is None:
                    job.process.terminate()
                job.updated_at = time.time()


def validate_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http or https URL.")
    return url


def is_direct_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    query = parse_qs(parsed.query)
    return path.endswith("/playlist") or ("list" in query and "v" not in query)


def describe_source_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    return "YouTube Music" if host.lower() == "music.youtube.com" else "YouTube"


def parse_spotify_url(value: str) -> tuple[str, bool, str]:
    url = validate_url(value)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if host == "spotify.link":
        return url, False, "Spotify short link"

    if host != "open.spotify.com" or len(parts) < 2:
        raise ValueError("Enter a Spotify song or playlist link.")

    kind = parts[0].lower()
    if kind == "track":
        return url, False, "Spotify song"
    if kind == "playlist":
        return url, True, "Spotify playlist"

    raise ValueError("Enter a Spotify song or playlist link.")


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value).strip()


def validate_media_type(value: str) -> str:
    media_type = value.strip().lower()
    if media_type not in {"mp3", "mp4"}:
        raise ValueError("Choose either mp3 or mp4.")
    return media_type


def resolve_output_dir(value: str | None) -> Path:
    if not value:
        return DEFAULT_DOWNLOAD_DIR

    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    path = Path(expanded)
    if not path.is_absolute():
        path = APP_ROOT / path
    return path.resolve()


def resolve_ffmpeg() -> str | None:
    ffmpeg_from_path = shutil.which("ffmpeg")
    if ffmpeg_from_path:
        return ffmpeg_from_path

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def dependency_status() -> dict[str, Any]:
    yt_dlp_available = True
    yt_dlp_version = ""
    try:
        import yt_dlp

        yt_dlp_version = getattr(yt_dlp.version, "__version__", "")
    except Exception:
        yt_dlp_available = False

    spotdl_available = True
    spotdl_version = ""
    try:
        import spotdl

        spotdl_version = getattr(spotdl, "__version__", "")
    except Exception:
        spotdl_available = False

    ffmpeg_path = resolve_ffmpeg()
    return {
        "python": sys.version.split()[0],
        "ytDlpAvailable": yt_dlp_available,
        "ytDlpVersion": yt_dlp_version,
        "spotdlAvailable": spotdl_available,
        "spotdlVersion": spotdl_version,
        "ffmpegAvailable": bool(ffmpeg_path),
        "ffmpegPath": ffmpeg_path or "",
        "defaultOutputDir": str(DEFAULT_DOWNLOAD_DIR),
        "defaultSpotifyOutputDir": str(DEFAULT_SPOTIFY_DOWNLOAD_DIR),
    }


def build_ydl_options(job: DownloadJob) -> dict[str, Any]:
    output_path = Path(job.output_dir)
    single_template = str(output_path / "%(title).200B [%(id)s].%(ext)s")
    playlist_template = str(
        output_path
        / "%(playlist_title).180B"
        / "%(playlist_index)s - %(title).180B [%(id)s].%(ext)s"
    )

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "noplaylist": not job.playlist,
        "outtmpl": playlist_template if job.playlist else single_template,
        "progress_hooks": [make_progress_hook(job.id)],
        "logger": JobLogger(job.id),
        "ignoreerrors": False,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
    }

    ffmpeg_path = resolve_ffmpeg()
    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path

    if job.media_type == "mp3":
        if not ffmpeg_path:
            raise RuntimeError(
                "MP3 conversion needs FFmpeg. Install dependencies with "
                "python -m pip install -r requirements.txt or install FFmpeg on PATH."
            )
        options.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "0",
                    },
                    {
                        "key": "FFmpegMetadata",
                        "add_metadata": True,
                        "add_chapters": True,
                    },
                ],
            }
        )
    else:
        options.update(
            {
                "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
                "merge_output_format": "mp4",
                "postprocessors": [
                    {
                        "key": "FFmpegMetadata",
                        "add_metadata": True,
                        "add_chapters": True,
                    }
                ],
            }
        )

    return options


def make_progress_hook(job_id: str):
    def progress_hook(data: dict[str, Any]) -> None:
        job = get_job(job_id)
        if job and job.cancel_requested:
            raise DownloadCancelled("Download cancelled.")

        status = data.get("status")
        info = data.get("info_dict") or {}
        title = info.get("title") or ""
        filename = data.get("filename") or info.get("_filename") or ""

        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            progress = (downloaded / total * 100) if total else 0.0
            update_job(
                job_id,
                status="running",
                title=title,
                filename=filename,
                progress=progress,
                speed=(data.get("_speed_str") or "").strip(),
                eta=(data.get("_eta_str") or "").strip(),
                message="Downloading",
            )
        elif status == "finished":
            update_job(
                job_id,
                title=title,
                filename=filename,
                progress=100.0,
                speed="",
                eta="",
                message="Processing media",
            )
            if title:
                add_log(job_id, f"Finished download: {title}")

    return progress_hook


class JobLogger:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            return
        cleaned = message.strip()
        if cleaned:
            add_log(self.job_id, cleaned)

    def warning(self, message: str) -> None:
        cleaned = message.strip()
        if cleaned:
            add_log(self.job_id, f"Warning: {cleaned}")

    def error(self, message: str) -> None:
        cleaned = message.strip()
        if cleaned:
            add_log(self.job_id, f"Error: {cleaned}")


def run_download(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    try:
        update_job(job_id, status="running", message="Starting download")
        add_log(job_id, f"Saving to {job.output_dir}")
        add_log(job_id, f"Source: {describe_source_url(job.url)}")
        if job.playlist:
            add_log(job_id, "Playlist mode enabled.")
        Path(job.output_dir).mkdir(parents=True, exist_ok=True)

        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "yt-dlp is not installed. Run python -m pip install -r requirements.txt first."
            ) from exc

        options = build_ydl_options(job)
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([job.url])

        latest = get_job(job_id)
        if latest and latest.cancel_requested:
            raise DownloadCancelled("Download cancelled.")

        update_job(
            job_id,
            status="complete",
            progress=100.0,
            speed="",
            eta="",
            message="Complete",
        )
        add_log(job_id, "Download complete.")
    except DownloadCancelled:
        update_job(
            job_id,
            status="cancelled",
            speed="",
            eta="",
            message="Cancelled",
            error="",
        )
        add_log(job_id, "Download cancelled.")
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            speed="",
            eta="",
            message="Failed",
            error=str(exc),
        )
        add_log(job_id, f"Failed: {exc}")
        traceback.print_exc()


def run_spotify_download(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    try:
        try:
            import spotdl  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "spotDL is not installed. Run python -m pip install -r requirements.txt first."
            ) from exc

        ffmpeg_path = resolve_ffmpeg()
        if not ffmpeg_path:
            raise RuntimeError(
                "Spotify MP3 downloads need FFmpeg. Run python -m pip install -r requirements.txt first."
            )

        output_dir = Path(job.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / "{artists} - {title}.{output-ext}")

        command = [
            sys.executable,
            "-m",
            "spotdl",
            "download",
            job.url,
            "--format",
            "mp3",
            "--output",
            output_template,
            "--overwrite",
            "skip",
            "--restrict",
            "none",
            "--print-errors",
            "--audio",
            "youtube-music",
            "youtube",
            "--ffmpeg",
            ffmpeg_path,
        ]

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        update_job(job_id, status="running", title="Spotify MP3 download", message="Starting spotDL")
        add_log(job_id, "Using Spotify metadata to find matching audio from YouTube/YouTube Music.")
        add_log(job_id, f"Saving MP3 files to {job.output_dir}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
        )
        update_job(job_id, process=process)

        assert process.stdout is not None
        for line in process.stdout:
            clean_line = strip_ansi(line)
            if clean_line:
                add_log(job_id, clean_line)
                update_job(job_id, message=clean_line[:140])

        return_code = process.wait()
        update_job(job_id, process=None)

        latest = get_job(job_id)
        if latest and latest.cancel_requested:
            raise DownloadCancelled("Download cancelled.")

        if return_code != 0:
            raise RuntimeError(f"spotDL exited with code {return_code}. Check the job log for details.")

        update_job(
            job_id,
            status="complete",
            progress=100.0,
            speed="",
            eta="",
            message="Complete",
        )
        add_log(job_id, "Spotify MP3 download complete.")
    except DownloadCancelled:
        update_job(
            job_id,
            status="cancelled",
            process=None,
            speed="",
            eta="",
            message="Cancelled",
            error="",
        )
        add_log(job_id, "Spotify MP3 download cancelled.")
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            process=None,
            speed="",
            eta="",
            message="Failed",
            error=str(exc),
        )
        add_log(job_id, f"Failed: {exc}")
        traceback.print_exc()


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "ytdown/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/api/health":
            json_response(self, HTTPStatus.OK, dependency_status())
            return

        if self.path == "/api/jobs":
            json_response(self, HTTPStatus.OK, {"jobs": list_jobs()})
            return

        self.serve_static()

    def do_POST(self) -> None:
        if self.path == "/api/download":
            self.create_download()
            return

        if self.path == "/api/spotify/download":
            self.create_spotify_download()
            return

        if self.path == "/api/shutdown":
            self.shutdown_server()
            return

        if self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
            self.cancel_download()
            return

        json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def create_download(self) -> None:
        try:
            payload = self.read_json()
            url = validate_url(str(payload.get("url", "")))
            media_type = validate_media_type(str(payload.get("mediaType", "mp4")))
            playlist = bool(payload.get("playlist", False)) or is_direct_playlist_url(url)
            output_dir = resolve_output_dir(str(payload.get("outputDir", "")).strip())

            job_id = uuid.uuid4().hex[:12]
            job = DownloadJob(
                id=job_id,
                url=url,
                media_type=media_type,
                playlist=playlist,
                output_dir=str(output_dir),
                source=describe_source_url(url),
            )
            with JOBS_LOCK:
                JOBS[job_id] = job

            thread = threading.Thread(target=run_download, args=(job_id,), daemon=True)
            thread.start()
            json_response(self, HTTPStatus.CREATED, {"job": job.to_dict()})
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def create_spotify_download(self) -> None:
        try:
            payload = self.read_json()
            url, playlist, label = parse_spotify_url(str(payload.get("url", "")))
            output_dir = resolve_output_dir(
                str(payload.get("outputDir", "")).strip()
                or str(DEFAULT_SPOTIFY_DOWNLOAD_DIR)
            )

            job_id = uuid.uuid4().hex[:12]
            job = DownloadJob(
                id=job_id,
                url=url,
                media_type="mp3",
                playlist=playlist,
                output_dir=str(output_dir),
                source="Spotify",
                title=label,
                message="Waiting to start spotDL",
            )
            with JOBS_LOCK:
                JOBS[job_id] = job

            thread = threading.Thread(target=run_spotify_download, args=(job_id,), daemon=True)
            thread.start()
            json_response(self, HTTPStatus.CREATED, {"job": job.to_dict()})
        except ValueError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def cancel_download(self) -> None:
        parts = self.path.strip("/").split("/")
        job_id = parts[2] if len(parts) >= 3 else ""
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                return
            if job.status in {"complete", "error", "cancelled"}:
                json_response(self, HTTPStatus.OK, {"job": job.to_dict()})
                return
            job.cancel_requested = True
            job.message = "Cancelling"
            if job.process and job.process.poll() is None:
                job.process.terminate()
            job.updated_at = time.time()
        json_response(self, HTTPStatus.OK, {"job": job.to_dict()})

    def shutdown_server(self) -> None:
        cancel_active_jobs("Cancelling for shutdown")
        json_response(self, HTTPStatus.OK, {"message": "Server is shutting down."})
        thread = threading.Thread(target=self.server.shutdown, daemon=True)
        thread.start()

    def serve_static(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", ""}:
            target = STATIC_ROOT / "index.html"
        else:
            requested = path.lstrip("/")
            if requested.startswith("static/"):
                requested = requested.removeprefix("static/")
            target = (STATIC_ROOT / requested).resolve()

        try:
            target.relative_to(STATIC_ROOT)
        except ValueError:
            text_response(self, HTTPStatus.FORBIDDEN, "Forbidden")
            return

        if not target.exists() or not target.is_file():
            text_response(self, HTTPStatus.NOT_FOUND, "Not found")
            return

        return super().do_GET()

    def translate_path(self, path: str) -> str:
        parsed_path = urlparse(path).path
        if parsed_path in {"/", ""}:
            return str(STATIC_ROOT / "index.html")

        requested = parsed_path.lstrip("/")
        if requested.startswith("static/"):
            requested = requested.removeprefix("static/")
        return str(STATIC_ROOT / requested)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local YouTube downloader UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()

    DEFAULT_DOWNLOAD_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"ytdown running at {url}")
    print("Press Ctrl+C to stop.")
    if args.open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
