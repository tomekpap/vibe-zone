"""Deliberately slow, correct SQLite baseline. See README before optimizing."""
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "social.sqlite3"
NAME = re.compile(r"[a-zA-Z0-9_]{1,32}\Z")

with sqlite3.connect(DB_PATH) as db:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS likes (post_id INTEGER NOT NULL, user TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_likes_post_id ON likes (post_id);
    """)


def connection():
    if "db" not in g:
        # Intentionally: one connection per request, DELETE journal, FULL sync.
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_connection(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def valid_name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


@app.errorhandler(ValueError)
def bad_input(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(error=error.description), 400 if error.code == 413 else error.code


@app.get("/health")
def health():
    connection().execute("SELECT 1")
    return jsonify(status="ok")


@app.post("/posts")
def create_post():
    value = body()
    author, content = value.get("author"), value.get("content")
    if not valid_name(author):
        raise ValueError("author must match [a-zA-Z0-9_]{1,32}")
    if not isinstance(content, str) or not 1 <= len(content) <= 280 or not content.strip():
        raise ValueError("content must contain 1..280 characters and not be blank")
    # Keep supplied content verbatim; trim only tests for whitespace-only input.
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("content must be valid UTF-8") from None
    db = connection()
    created = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db:
        cursor = db.execute("INSERT INTO posts(author, content, created_at) VALUES (?, ?, ?)",
                            (author, content, created))
    return jsonify(id=cursor.lastrowid, author=author, content=content, created_at=created, likes=0), 201


@app.post("/like")
def like():
    value = body()
    post_id, user = value.get("post_id"), value.get("user")
    if type(post_id) is not int or post_id < 1 or post_id > 9223372036854775807 or not valid_name(user):
        raise ValueError("Expected a positive integer post_id and a valid user")
    db = connection()
    # Serialize the check + insert without adding an index. Correct even if workers change.
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone() is None:
            return jsonify(error="Post not found"), 404
        if db.execute("SELECT 1 FROM likes WHERE post_id = ? AND user = ?", (post_id, user)).fetchone() is None:
            db.execute("INSERT INTO likes VALUES (?, ?)", (post_id, user))
        count = db.execute("SELECT COUNT(*) FROM likes WHERE post_id = ?", (post_id,)).fetchone()[0]
    return jsonify(post_id=post_id, likes=count, liked=True)


@app.get("/feed")
def feed():
    db = connection()
    query = """
        SELECT id, author, content, created_at,
               (SELECT COUNT(*) FROM likes WHERE post_id = posts.id) AS likes
        FROM posts
        ORDER BY id DESC
        LIMIT 20
    """
    posts = [dict(row) for row in db.execute(query).fetchall()]
    return jsonify(posts=posts)
