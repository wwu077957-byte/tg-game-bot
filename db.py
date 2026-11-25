"""SQLite 持久化：娱乐积分、用户是否私聊过机器人、个人最佳记录。

积分仅供娱乐，不可兑换任何东西。
"""
import sqlite3
import threading
import time


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scores(
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    game TEXT NOT NULL, points INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, user_id, game));
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, name TEXT,
                    started INTEGER NOT NULL DEFAULT 0, updated REAL);
                CREATE TABLE IF NOT EXISTS stats(
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    key TEXT NOT NULL, value INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, user_id, key));
                """
            )

    def close(self):
        with self.lock:
            self.conn.close()

    def touch_user(self, user_id, name, started=None):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO users(user_id,name,started,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, updated=excluded.updated",
                (user_id, name, 1 if started else 0, time.time()),
            )
            if started is not None:
                self.conn.execute(
                    "UPDATE users SET started=? WHERE user_id=?", (1 if started else 0, user_id)
                )

    def set_started(self, user_id, started):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO users(user_id,started,updated) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET started=excluded.started",
                (user_id, 1 if started else 0, time.time()),
            )

    def is_started(self, user_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT started FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        return bool(row and row[0])

    def add_points(self, chat_id, user_id, name, game, points):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO scores(chat_id,user_id,game,points) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id,user_id,game) DO UPDATE SET points=points+excluded.points",
                (chat_id, user_id, game, points),
            )
            self.conn.execute(
                "INSERT INTO users(user_id,name,updated) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                (user_id, name, time.time()),
            )

    def top(self, chat_id, game=None, limit=10):
        sql = (
            "SELECT s.user_id, COALESCE(u.name, s.user_id), SUM(s.points) AS p "
            "FROM scores s LEFT JOIN users u ON u.user_id=s.user_id WHERE s.chat_id=?"
        )
        args = [chat_id]
        if game:
            sql += " AND s.game=?"
            args.append(game)
        sql += " GROUP BY s.user_id HAVING p>0 ORDER BY p DESC, s.user_id LIMIT ?"
        args.append(limit)
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def points(self, chat_id, user_id, game=None):
        sql = "SELECT COALESCE(SUM(points),0) FROM scores WHERE chat_id=? AND user_id=?"
        args = [chat_id, user_id]
        if game:
            sql += " AND game=?"
            args.append(game)
        with self.lock:
            return self.conn.execute(sql, args).fetchone()[0]

    def stat_max(self, chat_id, user_id, key, value):
        """记录最大值，返回更新后的最佳值。"""
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO stats(chat_id,user_id,key,value) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id,user_id,key) DO UPDATE SET value=MAX(value, excluded.value)",
                (chat_id, user_id, key, value),
            )
            return self.conn.execute(
                "SELECT value FROM stats WHERE chat_id=? AND user_id=? AND key=?",
                (chat_id, user_id, key),
            ).fetchone()[0]

    def get_stat(self, chat_id, user_id, key):
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM stats WHERE chat_id=? AND user_id=? AND key=?",
                (chat_id, user_id, key),
            ).fetchone()
        return row[0] if row else 0
