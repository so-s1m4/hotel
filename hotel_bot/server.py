"""Local presentation server for the Hotel dashboard and guest-request API.

The server binds only to loopback. It serves a built frontend and delegates all
hotel state and approval decisions to HotelService. Starting it does not contact
Cerebras; the configured profile is installed once for the server's lifetime.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from threading import Event
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .config import CerebrasConfigurationError, load_cerebras_config
from .datasets import DatasetImportError, MAX_UPLOAD_BYTES

MAX_BODY_BYTES = 16 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
_JOB_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
_ARTIFACTS = {
    "decisions": ("decisions.jsonl", "application/x-ndjson"),
    "audit": ("audit.jsonl", "application/x-ndjson"),
    "journal": ("journal.jsonl", "application/x-ndjson"),
    "extractions": ("extractions.jsonl", "application/x-ndjson"),
    "rejections": ("rejections.jsonl", "application/x-ndjson"),
    "summary": ("summary.json", "application/json"),
    "validation": ("validation.json", "application/json"),
}
_ERROR_MESSAGES = {
    400: ("invalid_request", "Проверьте поля запроса."),
    403: ("forbidden_origin", "Запрос разрешён только со страницы Hotel на этом компьютере."),
    404: ("not_found", "Запись или страница не найдена."),
    405: ("method_not_allowed", "Этот способ запроса не поддерживается."),
    409: ("conflict", "Состояние уже изменилось. Обновите страницу и проверьте действие."),
    411: ("length_required", "Не указан размер запроса."),
    413: ("request_too_large", "Сообщение слишком большое."),
    415: ("unsupported_media_type", "Ожидается запрос в формате JSON."),
    500: ("internal_error", "Не удалось выполнить запрос. Попробуйте ещё раз."),
    503: ("service_unavailable", "Интерфейс или сервис ещё недоступен. Проверьте запуск Hotel."),
}


def _strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON value")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


class HotelHTTPServer(ThreadingHTTPServer):
    # Provider settings are process-global: accepted handlers must finish before
    # main can leave the fixed configuration context.
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, service, static_dir: Path, replay_manager=None):
        self.service = service
        self.replay_manager = replay_manager
        self.static_dir = static_dir.resolve()
        self.stopping = Event()
        super().__init__(address, HotelRequestHandler)

    def stop_accepting(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.service.stop_accepting()
        if self.replay_manager is not None:
            self.replay_manager.close(wait=False)

    def shutdown(self):
        self.stop_accepting()
        super().shutdown()

    def server_close(self):
        try:
            self.stop_accepting()
        finally:
            # ThreadingMixIn joins every accepted non-daemon handler here.
            super().server_close()

    def handle_error(self, request, client_address):
        # Handler errors must not print request data, headers or exception text.
        # A disconnected local browser is not a reason to emit a traceback.
        pass


class HotelRequestHandler(BaseHTTPRequestHandler):
    server_version = "HotelBot"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(65)

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler logs raw paths; keep credentials and guest data
        # out of terminal output even if a client puts them in a malformed URL.
        pass

    def _headers(self, status: int, content_type: str, length: int, *, filename: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if filename is not None:
            # Callers supply only a fixed name from _ARTIFACTS, never a URL/path.
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", (
            "default-src 'self'; connect-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        ))
        self.end_headers()

    def _json(self, status: int, value: Any):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int):
        code, message = _ERROR_MESSAGES[status]
        self._json(status, {"error": {"code": code, "message": message}})

    def _allowed_request(self, *, write: bool = False) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            self._error(403)
            return False
        try:
            if any(char.isspace() for char in hosts[0]):
                raise ValueError
            host = urlsplit("//" + hosts[0])
            if (host.hostname not in _LOOPBACK_HOSTS or (host.port or 80) != self.server.server_port
                    or host.username is not None or host.password is not None
                    or host.path or host.query or host.fragment):
                raise ValueError
        except ValueError:
            self._error(403)
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            self._error(403)
            return False
        if origins:
            try:
                if any(char.isspace() for char in origins[0]):
                    raise ValueError
                origin = urlsplit(origins[0])
                if (origin.scheme != "http" or origin.hostname != host.hostname
                        or (origin.port or 80) != (host.port or 80) or origin.username is not None
                        or origin.password is not None or origin.path or origin.query or origin.fragment):
                    raise ValueError
            except ValueError:
                self._error(403)
                return False
        if write and self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._error(403)
            return False
        return True

    def _path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("Only local paths are accepted")
        path = unquote(parsed.path, errors="strict")
        if not path.startswith("/") or "\x00" in path or "\\" in path:
            raise ValueError("Invalid path")
        return path

    def _pagination(self, *, max_limit: int = 500) -> dict[str, int]:
        pairs = parse_qsl(urlsplit(self.path).query, keep_blank_values=True,
                          strict_parsing=True, errors="strict", max_num_fields=2)
        if len({name for name, _ in pairs}) != len(pairs):
            raise ValueError("Duplicate pagination fields")
        values = {"after": 0, "limit": 100}
        for name, value in pairs:
            if name not in values or not re.fullmatch(r"[0-9]{1,18}", value):
                raise ValueError("Invalid pagination")
            values[name] = int(value)
        if not 1 <= values["limit"] <= max_limit:
            raise ValueError("Invalid pagination limit")
        return values

    def _no_query(self):
        if urlsplit(self.path).query:
            raise ValueError("This route accepts no query parameters")

    def _frame_selection(self):
        pairs = parse_qsl(urlsplit(self.path).query, keep_blank_values=True,
                          strict_parsing=True, errors="strict", max_num_fields=1)
        if not pairs:
            return {"cursor": 0}
        name, value = pairs[0]
        if name == "cursor" and re.fullmatch(r"[0-9]{1,18}", value):
            return {"cursor": int(value)}
        if name == "at" and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
            return {"at": value}
        raise ValueError("Choose one playback cursor or timezone-aware timestamp")

    def _replays(self):
        if self.server.replay_manager is None:
            error = ValueError("Replay service unavailable")
            error.status_code = 503
            raise error
        return self.server.replay_manager

    def _domain_error(self, exc):
        if isinstance(exc, DatasetImportError):
            self._json(exc.status_code, {"error": {"code": exc.public_code, "message": exc.public_message}})
            return
        status = getattr(exc, "status_code", 400)
        self._error(status if type(status) is int and status in {400, 404, 409, 503} else 400)

    def _artifact(self, job_id: str, kind: str):
        self._no_query()
        # The manager is the only authority for artifact locations and readiness;
        # the client cannot submit a filename or any filesystem path.
        path = self._replays().artifact(job_id, kind)
        filename, content_type = _ARTIFACTS[kind]
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            self._headers(200, content_type, size, filename=filename)
            if self.command != "HEAD":
                remaining = size
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def do_GET(self):
        if not self._allowed_request():
            return
        try:
            path = self._path()
            if path == "/api/state":
                self._json(200, self.server.service.snapshot())
                return
            if path == "/api/journal":
                self._json(200, self.server.service.journal(**self._pagination()))
                return
            if path == "/api/datasets":
                self._no_query()
                self._json(200, {"datasets": self._replays().list_datasets()})
                return
            playback = re.fullmatch(r"/api/replays/(" + _JOB_ID + r")/(playback|frame|timeline)", path)
            if playback is not None:
                job_id, route = playback.groups()
                if route == "playback":
                    self._no_query()
                    value = self._replays().playback_manifest(job_id)
                elif route == "frame":
                    value = self._replays().playback_frame(job_id, **self._frame_selection())
                else:
                    value = self._replays().timeline(job_id, **self._pagination(max_limit=2000))
                self._json(200, value)
                return
            replay = re.fullmatch(r"/api/replays/(" + _JOB_ID + r")", path)
            if replay is not None:
                self._json(200, self._replays().status(replay.group(1), **self._pagination(max_limit=200)))
                return
            artifact = re.fullmatch(r"/api/replays/(" + _JOB_ID + r")/artifacts/(" + "|".join(_ARTIFACTS) + r")", path)
            if artifact is not None:
                self._artifact(artifact.group(1), artifact.group(2))
                return
            if path.startswith("/api/"):
                self._error(404)
                return
            root = self.server.static_dir
            candidate = (root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if not candidate.is_relative_to(root):
                self._error(404)
                return
            if not candidate.is_file():
                self._error(503 if path == "/" and not (root / "index.html").is_file() else 404)
                return
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type += "; charset=utf-8"
            self._headers(200, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)
        except ValueError as exc:
            self._domain_error(exc)
        except Exception:
            self._error(500)

    def do_HEAD(self):
        self.do_GET()

    def _body(self) -> dict | None:
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._error(415)
            return None
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400)
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(411)
            return None
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            self._error(400)
            return None
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            self._error(413)
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._error(400)
            return None
        try:
            return _strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            self._error(400)
            return None

    def _import_zip(self):
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/zip":
            raise DatasetImportError("zip_required", "Choose a ZIP file and upload it as application/zip.", 415)
        if self.headers.get("Transfer-Encoding") is not None:
            raise DatasetImportError("invalid_upload", "Upload the ZIP with a fixed Content-Length.")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise DatasetImportError("length_required", "The ZIP size is required.", 411)
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,18}", lengths[0]):
            raise DatasetImportError("invalid_upload", "The ZIP size is invalid.")
        length = int(lengths[0])
        if not 0 < length <= MAX_UPLOAD_BYTES:
            raise DatasetImportError("upload_size_limit", "Choose a ZIP no larger than 128 MiB.", 413)
        return self._replays().import_dataset(self.rfile, length, cancelled=self.server.stopping.is_set)

    def do_POST(self):
        if not self._allowed_request(write=True):
            return
        if self.server.stopping.is_set():
            self._error(503)
            return
        try:
            path = self._path()
            self._no_query()
            if path == "/api/datasets/import":
                self._json(200, self._import_zip())
                return
            approval = re.fullmatch(r"/api/approvals/([A-Za-z0-9_.:-]{1,128})", path)
            cancellation = re.fullmatch(r"/api/replays/(" + _JOB_ID + r")/cancel", path)
            if path not in {"/api/messages", "/api/replays"} and approval is None and cancellation is None:
                self._error(404)
                return
            payload = self._body()
            if payload is None:
                return
            if self.server.stopping.is_set():
                self._error(503)
                return
            if path == "/api/replays":
                if set(payload) != {"dataset_id"} or not isinstance(payload["dataset_id"], str):
                    raise ValueError("Exactly one dataset_id is required")
                value = {"job": self._replays().start(payload["dataset_id"])}
            elif cancellation is not None:
                if payload:
                    raise ValueError("Cancellation accepts an empty object")
                value = {"job": self._replays().cancel(cancellation.group(1))}
            elif approval is not None:
                decision = payload.get("decision")
                if (set(payload) - {"decision", "consent"}
                        or not isinstance(decision, str) or decision not in {"approve", "reject"}
                        or type(payload.get("consent", False)) is not bool):
                    self._error(400)
                    return
                value = self.server.service.approve(approval.group(1), payload["decision"],
                                                    consent=payload.get("consent", False))
            else:
                value = self.server.service.submit_message(payload)
            self._json(200, value)
        except ValueError as exc:
            self._domain_error(exc)
        except Exception:
            self._error(500)

    def do_OPTIONS(self):
        self._error(405)


def create_server(service, *, host: str = "127.0.0.1", port: int = 8765,
                  static_dir: str | Path | None = None, replay_manager=None) -> HotelHTTPServer:
    """Construct a loopback server; model inference is exclusively request-driven."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("Hotel server must bind to localhost or 127.0.0.1")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid Hotel server port")
    root = (Path(static_dir).expanduser() if static_dir is not None
            else Path(__file__).resolve().parents[1] / "hotel_web" / "dist")
    return HotelHTTPServer((host, port), service, root, replay_manager)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальная демонстрация Hotel с Cerebras API")
    parser.add_argument("--host", choices=sorted(_LOOPBACK_HOSTS), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--static-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    project_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--dataset-root", type=Path, default=project_root / "data")
    parser.add_argument("--replay-output-root", type=Path, default=project_root / "outputs" / "bot-replays")
    parser.add_argument("--import-root", type=Path, default=project_root / "outputs" / "imported-datasets")
    args = parser.parse_args(argv)
    try:
        config = load_cerebras_config(key_file=args.key_file, cache_dir=args.cache_dir)
        from .service import HotelService
        from .replay import ReplayManager

        with config.environment():
            replays = ReplayManager(dataset_root=args.dataset_root, output_root=args.replay_output_root,
                                    import_root=args.import_root)
            try:
                server = create_server(HotelService(), host=args.host, port=args.port,
                                       static_dir=args.static_dir, replay_manager=replays)
                print(f"Hotel: http://{args.host}:{server.server_port}", flush=True)
                print("Cerebras настроен. Подключение проверяется при первом сообщении.", flush=True)
                try:
                    server.serve_forever(poll_interval=0.25)
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
            finally:
                # Signal only owned replay jobs. Keep the fixed provider profile
                # installed until the pending bounded request has returned.
                while not replays.close(wait=True, timeout=65):
                    # Artifact finalization can outlast the model timeout. Never
                    # restore provider settings while an owned worker is alive.
                    pass
        return 0
    except CerebrasConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("Не удалось запустить Hotel. Проверьте порт и параметры запуска.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
