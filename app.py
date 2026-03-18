import ast
import io
import os
import queue
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import threading
from contextlib import redirect_stderr
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Callable

from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context, url_for


@dataclass
class SmtpFormData:
    server: str
    port: int
    security: str
    username: str
    password: str
    from_email: str
    to_email: str


DEFAULT_FORM = {
    "server": "",
    "port": 587,
    "security": "auto",
    "username": "",
    "password": "",
    "from_email": "",
    "to_email": "",
}

SECURITY_OPTIONS = {"auto", "none", "starttls", "ssl"}
SOCKET_TIMEOUT_SECONDS = 10
DB_PATH = os.environ.get("SMTP_TOOL_DB_PATH", str(Path.cwd() / "data" / "smtp_tool.db"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS smtp_hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS smtp_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                server TEXT NOT NULL,
                port INTEGER NOT NULL,
                security TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                from_email TEXT NOT NULL DEFAULT '',
                to_email TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS smtp_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server TEXT NOT NULL,
                port INTEGER NOT NULL,
                security TEXT NOT NULL,
                username TEXT NOT NULL,
                from_email TEXT NOT NULL,
                to_email TEXT NOT NULL,
                success INTEGER NOT NULL,
                transcript TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def _is_valid_host(value: str) -> bool:
    host = value.strip().lower()
    if len(host) < 1 or len(host) > 253:
        return False
    if host.startswith(".") or host.endswith("."):
        return False
    if ".." in host:
        return False
    return re.fullmatch(r"[a-z0-9.-]+", host) is not None


def _list_smtp_hosts() -> list[str]:
    with _db_connect() as conn:
        rows = conn.execute("SELECT host FROM smtp_hosts ORDER BY host ASC").fetchall()
    return [row["host"] for row in rows]


def _save_smtp_host(host: str) -> None:
    cleaned = host.strip().lower()
    if not _is_valid_host(cleaned):
        raise ValueError("Host must be a valid domain like mail.example.com.")
    with _db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO smtp_hosts(host) VALUES (?)", (cleaned,))


def _delete_smtp_host(host: str) -> bool:
    cleaned = host.strip().lower()
    with _db_connect() as conn:
        cursor = conn.execute("DELETE FROM smtp_hosts WHERE host = ?", (cleaned,))
    return cursor.rowcount > 0


def _list_smtp_configs() -> list[dict]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, server, port, security, username, from_email, to_email, updated_at
            FROM smtp_configs
            ORDER BY name ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _get_smtp_config(config_id: int) -> dict | None:
    with _db_connect() as conn:
        row = conn.execute(
            """
            SELECT id, name, server, port, security, username, password, from_email, to_email, updated_at
            FROM smtp_configs
            WHERE id = ?
            """,
            (config_id,),
        ).fetchone()
    return dict(row) if row else None


def _delete_smtp_config(config_id: int) -> bool:
    with _db_connect() as conn:
        cursor = conn.execute("DELETE FROM smtp_configs WHERE id = ?", (config_id,))
    return cursor.rowcount > 0


def _save_smtp_config(name: str, form_data: SmtpFormData) -> int:
    cleaned_name = name.strip()
    if not cleaned_name:
        cleaned_name = f"{form_data.server}:{form_data.username}"

    with _db_connect() as conn:
        existing = conn.execute("SELECT id FROM smtp_configs WHERE name = ?", (cleaned_name,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE smtp_configs
                SET server = ?, port = ?, security = ?, username = ?, password = ?, from_email = ?, to_email = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    form_data.server,
                    form_data.port,
                    form_data.security,
                    form_data.username,
                    form_data.password,
                    form_data.from_email,
                    form_data.to_email,
                    existing["id"],
                ),
            )
            return int(existing["id"])

        cursor = conn.execute(
            """
            INSERT INTO smtp_configs(name, server, port, security, username, password, from_email, to_email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_name,
                form_data.server,
                form_data.port,
                form_data.security,
                form_data.username,
                form_data.password,
                form_data.from_email,
                form_data.to_email,
            ),
        )
        return int(cursor.lastrowid)


def _record_history(form_data: SmtpFormData, success: bool, transcript: str) -> None:
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO smtp_history(server, port, security, username, from_email, to_email, success, transcript)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                form_data.server,
                form_data.port,
                form_data.security,
                form_data.username,
                form_data.from_email,
                form_data.to_email,
                1 if success else 0,
                transcript,
            ),
        )


def _list_history(limit: int = 30) -> list[dict]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, server, port, security, username, from_email, to_email, success, transcript, created_at
            FROM smtp_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _clear_history() -> None:
    with _db_connect() as conn:
        conn.execute("DELETE FROM smtp_history")


def _smtp_page_context(
    *,
    form_data: dict,
    result_message: str,
    result_type: str,
    active_tool: str = "smtp",
    active_tab: str = "smtp",
    selected_config_id: int | None = None,
    selected_config_name: str = "",
    notice_message: str = "",
) -> dict:
    return {
        "form_data": form_data,
        "result_message": result_message,
        "result_type": result_type,
        "active_tool": active_tool,
        "active_tab": active_tab,
        "smtp_hosts": _list_smtp_hosts(),
        "saved_configs": _list_smtp_configs(),
        "history_entries": _list_history(),
        "selected_config_id": selected_config_id,
        "selected_config_name": selected_config_name,
        "notice_message": notice_message,
    }


_init_db()


@app.after_request
def _set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    return response


def _is_valid_email(value: str) -> bool:
    _, parsed = parseaddr(value)
    return "@" in parsed and "." in parsed.split("@", 1)[-1]


def _parse_form(raw_form: dict) -> SmtpFormData:
    server = raw_form.get("server", "").strip()
    security = raw_form.get("security", "auto").strip().lower()
    username = raw_form.get("username", "").strip()
    password = raw_form.get("password", "")
    from_email = raw_form.get("from_email", "").strip()
    to_email = raw_form.get("to_email", "").strip()

    try:
        port = int(raw_form.get("port", "0"))
    except ValueError as exc:
        raise ValueError("Port must be a number.") from exc

    if not server:
        raise ValueError("SMTP server is required.")
    if port <= 0 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")
    if security not in SECURITY_OPTIONS:
        raise ValueError("Invalid security mode.")
    if not username:
        raise ValueError("Username is required.")
    if not password:
        raise ValueError("Password is required.")
    if not _is_valid_email(from_email):
        raise ValueError("From email address is invalid.")
    if not _is_valid_email(to_email):
        raise ValueError("To email address is invalid.")

    return SmtpFormData(
        server=server,
        port=port,
        security=security,
        username=username,
        password=password,
        from_email=from_email,
        to_email=to_email,
    )


def _build_message(data: SmtpFormData) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"SMTP test from {data.server}"
    msg["Message-ID"] = make_msgid()
    msg["From"] = data.from_email
    msg["To"] = data.to_email
    msg.set_content("Test message")
    msg.add_alternative("<b>Test message</b>", subtype="html")
    return msg


def _decode_debug_literal(raw_value: str) -> str:
    value = raw_value.strip()
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value

    if isinstance(parsed, bytes):
        return parsed.decode("utf-8", errors="replace")
    if isinstance(parsed, str):
        return parsed
    return str(parsed)


def _clean_debug_payload(payload: str) -> list[str]:
    text = payload.replace("\r\n", "\n").replace("\r", "\n")
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows = rows[:-1]
    return rows


def _normalize_smtp_debug_line(raw_line: str) -> list[str]:
    line = raw_line.strip()
    if not line:
        return []

    if line.startswith("send: "):
        payload = _decode_debug_literal(line.removeprefix("send: "))
        return [f">> {row}" if row else ">>" for row in _clean_debug_payload(payload)]

    if line.startswith("reply: b"):
        payload = _decode_debug_literal(line.removeprefix("reply: "))
        return [f"<< {row}" for row in _clean_debug_payload(payload) if row]

    if line.startswith("reply: retcode") or line.startswith("data:") or line.startswith("connect:"):
        return []

    return [line]


class _SmtpDebugRedactor:
    def __init__(self):
        self.redact_next_challenge_response = False

    def redact(self, line: str) -> str:
        if line.startswith(">> AUTH PLAIN "):
            return ">> AUTH PLAIN [REDACTED]"

        if line.startswith(">> AUTH LOGIN "):
            return ">> AUTH LOGIN [REDACTED]"

        if line.startswith("<< 334 "):
            challenge = line.removeprefix("<< 334 ").strip()
            if challenge in {"VXNlcm5hbWU6", "UGFzc3dvcmQ6"}:
                self.redact_next_challenge_response = True
            return line

        if self.redact_next_challenge_response and line.startswith(">> "):
            payload = line.removeprefix(">> ").strip()
            if re.fullmatch(r"[A-Za-z0-9+/=]+", payload):
                self.redact_next_challenge_response = False
                return ">> [REDACTED]"
            self.redact_next_challenge_response = False

        return line


class _SmtpDebugStream(io.TextIOBase):
    def __init__(self, emit_line: Callable[[str], None]):
        self._emit_line = emit_line
        self._redactor = _SmtpDebugRedactor()
        self._buffer = ""

    def write(self, raw: str) -> int:
        self._buffer += raw
        while "\n" in self._buffer:
            raw_line, self._buffer = self._buffer.split("\n", 1)
            for normalized in _normalize_smtp_debug_line(raw_line):
                self._emit_line(self._redactor.redact(normalized))
        return len(raw)

    def flush(self) -> None:
        if self._buffer:
            for normalized in _normalize_smtp_debug_line(self._buffer):
                self._emit_line(self._redactor.redact(normalized))
            self._buffer = ""


def _connection_uri(data: SmtpFormData, use_implicit_tls: bool) -> str:
    if use_implicit_tls:
        return f"smtps://{data.server}:{data.port}/"
    if data.security in {"starttls", "auto"}:
        mode = "always" if data.security == "starttls" else "auto"
        return f"smtp://{data.server}:{data.port}/?starttls={mode}"
    return f"smtp://{data.server}:{data.port}/"


def _send_test_email(data: SmtpFormData, emit_line: Callable[[str], None] | None = None) -> tuple[bool, str]:
    ssl_context = ssl.create_default_context()
    message = _build_message(data)
    smtp = None

    use_implicit_tls = data.security == "ssl" or (data.security == "auto" and data.port == 465)
    protocol_log: list[str] = []

    def _push(line: str) -> None:
        protocol_log.append(line)
        if emit_line is not None:
            emit_line(line)

    _push(f"Connected to {_connection_uri(data, use_implicit_tls)}")
    debug_stream = _SmtpDebugStream(_push)

    try:
        if use_implicit_tls:
            smtp = smtplib.SMTP_SSL(
                host=data.server,
                port=data.port,
                timeout=SOCKET_TIMEOUT_SECONDS,
                context=ssl_context,
            )
        else:
            smtp = smtplib.SMTP(
                host=data.server,
                port=data.port,
                timeout=SOCKET_TIMEOUT_SECONDS,
            )

        with redirect_stderr(debug_stream):
            smtp.set_debuglevel(1)
            smtp.ehlo()

            if data.security == "starttls" or (data.security == "auto" and not use_implicit_tls):
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=ssl_context)
                    smtp.ehlo()
                else:
                    if data.security == "starttls":
                        raise smtplib.SMTPNotSupportedError("Server does not support STARTTLS on this port.")
                    raise smtplib.SMTPNotSupportedError(
                        "Auto security requires TLS, but STARTTLS is unavailable. "
                        "Use Security=None only on trusted networks."
                    )

            smtp.login(data.username, data.password)
            smtp.send_message(message)

        _push("SUCCESS: Email sent successfully.")
        return True, "\n".join(protocol_log)
    except (smtplib.SMTPException, OSError, socket.timeout, ValueError) as exc:
        _push(f"ERROR: {exc}")
        return False, "\n".join(protocol_log)
    finally:
        if smtp is not None:
            try:
                smtp.set_debuglevel(0)
                with redirect_stderr(debug_stream):
                    smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass


def _is_htmx_request() -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _sanitize_tab(value: str | None) -> str:
    allowed = {"smtp", "configs", "history"}
    if value in allowed:
        return value
    return "smtp"


@app.post("/smtp/test/stream")
def smtp_test_stream():
    try:
        parsed_form = _parse_form(request.form)
    except ValueError as exc:
        message = str(exc)

        def invalid_stream():
            yield f"{message}\n"
            yield "__RESULT__|error\n"

        return Response(
            stream_with_context(invalid_stream()),
            mimetype="text/plain; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
            status=400,
        )

    def generate():
        line_queue: queue.Queue[tuple[str, str | bool]] = queue.Queue()

        def emit_line(line: str) -> None:
            line_queue.put(("line", line))

        def worker() -> None:
            success, transcript = _send_test_email(parsed_form, emit_line=emit_line)
            _record_history(parsed_form, success, transcript)
            line_queue.put(("done", success))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            kind, payload = line_queue.get()
            if kind == "line":
                yield f"{payload}\n"
                continue

            result_type = "success" if payload else "error"
            yield f"__RESULT__|{result_type}\n"
            break

    return Response(
        stream_with_context(generate()),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/smtp/hosts")
def add_smtp_host():
    tab = _sanitize_tab(request.form.get("tab"))
    host = request.form.get("host", "").strip().lower()
    try:
        _save_smtp_host(host)
    except ValueError as exc:
        return redirect(url_for("index", notice=str(exc), tab=tab))

    return redirect(url_for("index", notice=f"SMTP host '{host}' saved.", tab=tab))


@app.post("/smtp/hosts/delete")
def delete_smtp_host():
    tab = _sanitize_tab(request.form.get("tab"))
    host = request.form.get("host", "").strip().lower()
    if not host:
        return redirect(url_for("index", notice="Host is missing.", tab=tab))

    deleted = _delete_smtp_host(host)
    if deleted:
        return redirect(url_for("index", notice=f"SMTP host '{host}' deleted.", tab=tab))
    return redirect(url_for("index", notice="SMTP host not found.", tab=tab))


@app.post("/smtp/configs/save")
def save_smtp_config():
    try:
        parsed_form = _parse_form(request.form)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    config_name = request.form.get("config_name", "").strip()
    config_id = _save_smtp_config(config_name, parsed_form)
    try:
        _save_smtp_host(parsed_form.server)
    except ValueError:
        pass
    return jsonify(
        {
            "ok": True,
            "config_id": config_id,
            "message": "Configuration saved.",
        }
    )


@app.post("/smtp/configs/delete")
def delete_smtp_config():
    tab = _sanitize_tab(request.form.get("tab"))
    config_id = request.form.get("config_id", type=int)
    if not config_id:
        return redirect(url_for("index", notice="Config id is missing.", tab=tab))

    deleted = _delete_smtp_config(config_id)
    if deleted:
        return redirect(url_for("index", notice="Configuration deleted.", tab=tab))
    return redirect(url_for("index", notice="Configuration not found.", tab=tab))


@app.post("/smtp/history/flush")
def flush_smtp_history():
    tab = _sanitize_tab(request.form.get("tab"))
    _clear_history()
    return redirect(url_for("index", notice="History flushed.", tab=tab))


@app.get("/")
def index():
    active_tab = _sanitize_tab(request.args.get("tab"))
    selected_config_id = request.args.get("config_id", type=int)
    notice_message = request.args.get("notice", default="", type=str)
    selected_config_name = ""
    form_data = DEFAULT_FORM.copy()

    if selected_config_id:
        config = _get_smtp_config(selected_config_id)
        if config:
            form_data = {
                "server": config["server"],
                "port": config["port"],
                "security": config["security"],
                "username": config["username"],
                "password": config["password"],
                "from_email": config["from_email"],
                "to_email": config["to_email"],
            }
            selected_config_name = config["name"]
            try:
                _save_smtp_host(config["server"])
            except ValueError:
                pass
            if active_tab != "history":
                active_tab = "smtp"
        else:
            notice_message = "Saved configuration not found."

    return render_template(
        "index.html",
        **_smtp_page_context(
            form_data=form_data,
            result_message="",
            result_type="info",
            active_tool="smtp",
            active_tab=active_tab,
            selected_config_id=selected_config_id,
            selected_config_name=selected_config_name,
            notice_message=notice_message,
        ),
    )


@app.get("/tools/<tool_name>")
def tool_placeholder(tool_name: str):
    safe_name = tool_name.strip().lower()
    return render_template(
        "index.html",
        **_smtp_page_context(
            form_data=DEFAULT_FORM.copy(),
            result_message=f"Tool '{safe_name}' is coming soon.",
            result_type="info",
            active_tool=safe_name,
            active_tab="smtp",
        ),
    )


@app.post("/smtp/test")
def smtp_test():
    try:
        parsed_form = _parse_form(request.form)
    except ValueError as exc:
        merged = DEFAULT_FORM | request.form.to_dict()
        if _is_htmx_request():
            return render_template(
                "partials/result.html",
                form_data=merged,
                result_message=str(exc),
                result_type="error",
            ), 400

        return render_template(
            "index.html",
            **_smtp_page_context(
                form_data=merged,
                result_message=str(exc),
                result_type="error",
                active_tool="smtp",
                active_tab="smtp",
            ),
        ), 400

    success, message = _send_test_email(parsed_form)
    _record_history(parsed_form, success, message)
    result_type = "success" if success else "error"
    status_code = 200 if success else 502

    form_data = {
        "server": parsed_form.server,
        "port": parsed_form.port,
        "security": parsed_form.security,
        "username": parsed_form.username,
        "password": "",
        "from_email": parsed_form.from_email,
        "to_email": parsed_form.to_email,
    }

    if _is_htmx_request():
        return render_template(
            "partials/result.html",
            form_data=form_data,
            result_message=message,
            result_type=result_type,
        ), status_code

    return render_template(
        "index.html",
        **_smtp_page_context(
            form_data=form_data,
            result_message=message,
            result_type=result_type,
            active_tool="smtp",
            active_tab="smtp",
        ),
    ), status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
