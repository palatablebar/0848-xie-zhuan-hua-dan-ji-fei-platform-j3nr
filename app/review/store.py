import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS refund_entries(
              entry_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              call_id TEXT NOT NULL,
              seconds INTEGER NOT NULL,
              subscriber TEXT NOT NULL DEFAULT '',
              direction TEXT NOT NULL DEFAULT '',
              evidence TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              UNIQUE(run_id, entry_id)
            );
            ''')

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def review(self, run_id, decision, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                       (decision, comment, run_id))
        return self.get(run_id), None

    def refund_entries(self, run_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM refund_entries WHERE run_id=? ORDER BY created_at, rowid", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_refund_entry(self, run_id, call_id, seconds, subscriber, direction, evidence, note):
        """补证按 (批次, 话单号, 证据) 幂等：同一证据重试返回既有记录，不重复入账。"""
        entry_id = __import__("hashlib").sha256(
            "\x1f".join([run_id, call_id, evidence]).encode("utf-8")
        ).hexdigest()[:24]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM refund_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if existing:
                return dict(existing), False
            created = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO refund_entries(entry_id,run_id,created_at,call_id,seconds,subscriber,direction,evidence,note)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (entry_id, run_id, created, call_id, int(seconds), subscriber, direction, evidence, note),
            )
        with self.connect() as db:
            row = db.execute("SELECT * FROM refund_entries WHERE entry_id=?", (entry_id,)).fetchone()
        return dict(row), True
