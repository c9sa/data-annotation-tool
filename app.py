"""A local CSV/JSON text annotation tool. Run: python app.py [optional-file]"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, unquote, urlparse


HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
GEIST_FONT = HERE / "assets" / "Geist-Variable.woff2"
WORKING = HERE / "working"
SESSIONS = WORKING / ".sessions"
DEFAULT_LABELS = ["question", "problem", "task", "feedback", "other"]
ANNOTATION_FIELDS = ("primary_label", "secondary_labels", "annotation_status")
MAX_UPLOAD = 25 * 1024 * 1024


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def parse_secondary(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
    raise ValueError("secondary_labels must be a JSON array of labels")


class Dataset:
    def __init__(self, path: Path, text_column: str | None = None,
                 labels: list[str] | None = None, mode: str = "single",
                 configured: bool = False) -> None:
        self.path = path.resolve()
        self.kind = self.path.suffix.lower()
        self.lock = threading.RLock()
        self.labels = list(DEFAULT_LABELS if labels is None else labels)
        self.mode = mode
        self.fieldnames: list[str] = []
        self.rows = self._load()
        columns = self.text_columns()
        if not columns:
            raise ValueError("The file needs at least one column to use as message text")
        if text_column is None:
            text_column = "text" if "text" in columns else columns[0]
        self.text_column = text_column
        self.configured = configured
        self.configure(self.labels, self.mode, self.text_column, save=False)

    def _load(self) -> list[dict[str, object]]:
        if self.kind == ".csv":
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.fieldnames = list(reader.fieldnames or [])
                rows = [dict(row) for row in reader]
        elif self.kind == ".json":
            with self.path.open("r", encoding="utf-8-sig") as handle:
                value = json.load(handle)
            if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
                raise ValueError("JSON must contain an array of objects")
            rows = [dict(row) for row in value]
            self.fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        else:
            raise ValueError("Choose a CSV or JSON file")
        if not rows:
            raise ValueError("The file has no rows")
        for row in rows:
            row["primary_label"] = str(row.get("primary_label") or "")
            row["secondary_labels"] = parse_secondary(row.get("secondary_labels"))
            row["annotation_status"] = str(row.get("annotation_status") or
                                           ("labeled" if row["primary_label"] else ""))
        for field in ANNOTATION_FIELDS:
            if field not in self.fieldnames:
                self.fieldnames.append(field)
        return rows

    def text_columns(self) -> list[str]:
        return [field for field in self.fieldnames if field and field not in ANNOTATION_FIELDS]

    def configure(self, labels: list[str], mode: str, text_column: str, save: bool = True) -> None:
        if not isinstance(labels, list) or not 1 <= len(labels) <= 9:
            raise ValueError("Choose between 1 and 9 labels")
        if any(not isinstance(label, str) or not label.strip() or len(label) > 50 for label in labels):
            raise ValueError("Labels must be nonempty text (up to 50 characters)")
        labels = [label.strip() for label in labels]
        if len({label.casefold() for label in labels}) != len(labels):
            raise ValueError("Labels must be unique")
        if mode not in ("single", "multi"):
            raise ValueError("Mode must be single or multi")
        if not isinstance(text_column, str) or text_column not in self.text_columns():
            raise ValueError("Choose a text column from this file")
        for row in self.rows:
            used = [row["primary_label"], *row["secondary_labels"]]
            if any(label and label not in labels for label in used):
                raise ValueError("The new labels would remove labels already used in this file")
            if mode == "single" and row["secondary_labels"]:
                raise ValueError("This file already has secondary labels; keep multi mode")
        with self.lock:
            if save and self.path.parent == WORKING:
                config = {"labels": labels, "mode": mode, "text_column": text_column}
                atomic_write(SESSIONS / f"{self.path.name}.json",
                             json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
            self.labels, self.mode, self.text_column = labels, mode, text_column
            if save:
                self.configured = True

    def public_state(self) -> dict[str, object]:
        with self.lock:
            return {
                "active": True,
                "filename": self.path.name,
                "path": str(self.path),
                "configured": self.configured,
                "labels": self.labels,
                "mode": self.mode,
                "text_column": self.text_column,
                "columns": self.text_columns(),
                "items": [
                    {"index": index, "text": str(row.get(self.text_column) or ""),
                     "primary_label": row["primary_label"],
                     "secondary_labels": list(row["secondary_labels"]),
                     "annotation_status": row["annotation_status"]}
                    for index, row in enumerate(self.rows)
                ],
            }

    def update(self, index: int, primary: str, secondary: list[str], status: str) -> None:
        with self.lock:
            if not isinstance(index, int) or not 0 <= index < len(self.rows):
                raise ValueError("Invalid row index")
            if primary and primary not in self.labels:
                raise ValueError("Unknown primary label")
            if any(label not in self.labels for label in secondary):
                raise ValueError("Unknown secondary label")
            if len(set(secondary)) != len(secondary) or primary in secondary:
                raise ValueError("Choose each label only once")
            if not primary and secondary:
                raise ValueError("Choose a primary label first")
            if "other" in secondary or (primary == "other" and secondary):
                raise ValueError("'other' must stand alone")
            if self.mode == "single" and secondary:
                raise ValueError("Switch to multi mode to add secondary labels")
            if status not in ("", "labeled", "skipped", "needs_review"):
                raise ValueError("Invalid annotation status")
            if status == "labeled" and not primary:
                raise ValueError("Choose a primary label")
            row = self.rows[index]
            previous = (row["primary_label"], row["secondary_labels"], row["annotation_status"])
            row["primary_label"], row["secondary_labels"], row["annotation_status"] = (
                primary, list(secondary), status
            )
            try:
                self._save()
            except Exception:
                row["primary_label"], row["secondary_labels"], row["annotation_status"] = previous
                raise

    def _save(self) -> None:
        if self.kind == ".csv":
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=self.fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in self.rows:
                saved_row = dict(row)
                saved_row["secondary_labels"] = json.dumps(row["secondary_labels"], ensure_ascii=False)
                writer.writerow(saved_row)
            data = output.getvalue().encode("utf-8")
        else:
            data = (json.dumps(self.rows, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write(self.path, data)

    def training_csv(self) -> bytes:
        with self.lock:
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["text", "label"])
            for row in self.rows:
                if row["annotation_status"] == "labeled" and row["primary_label"]:
                    writer.writerow([row.get(self.text_column, ""), row["primary_label"]])
            return output.getvalue().encode("utf-8-sig")


class AppState:
    def __init__(self, initial: Dataset | None = None) -> None:
        self.dataset = initial
        self.lock = threading.RLock()

    def recent(self) -> list[str]:
        if not WORKING.exists():
            return []
        files = [path for path in WORKING.iterdir()
                 if path.is_file() and path.suffix.lower() in (".csv", ".json")]
        return [path.name for path in sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)]

    def open(self, path: Path) -> Dataset:
        config_path = SESSIONS / f"{path.name}.json"
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        dataset = Dataset(path, config.get("text_column"), config.get("labels"),
                          config.get("mode", "single"), configured=config_path.exists())
        with self.lock:
            self.dataset = dataset
        return dataset

    def import_file(self, filename: str, content: bytes) -> Dataset:
        filename = PureWindowsPath(unquote(filename)).name
        if not filename or Path(filename).suffix.lower() not in (".csv", ".json"):
            raise ValueError("Choose a CSV or JSON file")
        WORKING.mkdir(parents=True, exist_ok=True)
        stem, suffix = Path(filename).stem, Path(filename).suffix.lower()
        destination = WORKING / f"{stem}_labeled{suffix}"
        number = 2
        while destination.exists():
            destination = WORKING / f"{stem}_labeled_{number}{suffix}"
            number += 1
        try:
            with destination.open("xb") as handle:
                handle.write(content)
            dataset = Dataset(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        with self.lock:
            self.dataset = dataset
        return dataset


def make_handler(app: AppState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route == "/":
                self.send_body(PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if route == "/assets/Geist-Variable.woff2":
                self.send_body(GEIST_FONT.read_bytes(), "font/woff2")
                return
            if route == "/api/state":
                data = app.dataset.public_state() if app.dataset else {"active": False}
                data["recent"] = app.recent()
                self.send_json(HTTPStatus.OK, data)
                return
            if route in ("/api/download", "/api/training"):
                dataset = app.dataset
                if dataset is None:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Open a file first"})
                    return
                if route == "/api/training":
                    body = dataset.training_csv()
                    filename = f"{dataset.path.stem}_train.csv"
                    kind = "text/csv; charset=utf-8"
                else:
                    with dataset.lock:
                        body = dataset.path.read_bytes()
                    filename = dataset.path.name
                    kind = "text/csv; charset=utf-8" if dataset.kind == ".csv" else "application/json"
                disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
                self.send_body(body, kind, extra={"Content-Disposition": disposition})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Open this page from the local app"})
                return
            route = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > MAX_UPLOAD:
                    raise ValueError("File or request is empty or larger than 25 MB")
                body = self.rfile.read(length)
                if route == "/api/import":
                    dataset = app.import_file(self.headers.get("X-Filename", ""), body)
                    result = dataset.public_state()
                else:
                    payload = json.loads(body)
                    if route == "/api/open":
                        name = payload["name"]
                        if not isinstance(name, str) or Path(name).name != name:
                            raise ValueError("Invalid session name")
                        path = WORKING / name
                        if not path.is_file() or path.suffix.lower() not in (".csv", ".json"):
                            raise ValueError("Session not found")
                        result = app.open(path).public_state()
                    else:
                        dataset = app.dataset
                        if dataset is None:
                            raise ValueError("Open a file first")
                        if route == "/api/config":
                            dataset.configure(payload["labels"], payload["mode"], payload["text_column"])
                        elif route == "/api/annotation":
                            secondary = payload.get("secondary_labels", [])
                            if not isinstance(secondary, list):
                                raise ValueError("secondary_labels must be an array")
                            dataset.update(payload["index"], payload.get("primary_label", ""),
                                           secondary, payload["annotation_status"])
                        else:
                            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                            return
                        result = dataset.public_state()
                result["recent"] = app.recent()
                self.send_json(HTTPStatus.OK, result)
            except (KeyError, TypeError, ValueError, UnicodeError, OSError, csv.Error,
                    json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def send_json(self, status: HTTPStatus, value: object) -> None:
            self.send_body(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", status)

        def send_body(self, body: bytes, kind: str, status: HTTPStatus = HTTPStatus.OK,
                      extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path, help="optional file to open directly")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        app = AppState()
        if args.file:
            source = args.file.expanduser()
            app.import_file(source.name, source.read_bytes())
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    url = f"http://127.0.0.1:{args.port}"
    print(f"\nAnnotation tool: {url}")
    print(f"Working copies: {WORKING}")
    print("Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
