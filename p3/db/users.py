"""Users, their sessions, and what is in each user's library."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .jobs import Jobs


class Users(Jobs):
    # Users, subscriptions and episode links
    def ensure_user(
        self,
        username: str,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the user, creating them on first sign-in.

        The first user ever is an admin and takes over everything already in
        the library (from before users existed): every podcast and episode.
        """
        user = self.get_user(username)
        if user is None:
            first = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            self.conn.execute(
                "INSERT INTO users (username, display_name, email, role, settings) "
                "VALUES (?, ?, ?, ?, '{}')",
                (username, display_name, email, "admin" if first else "user"),
            )
            if first:
                self.conn.execute(
                    "INSERT INTO user_podcasts (username, podcast_id) "
                    "SELECT ?, id FROM podcasts",
                    (username,),
                )
                self.conn.execute(
                    "INSERT INTO user_episodes (username, episode_id) "
                    "SELECT ?, id FROM episodes",
                    (username,),
                )
            user = self.get_user(username)
        elif (display_name and display_name != user["display_name"]) or (
            email and email != user["email"]
        ):
            self.conn.execute(
                "UPDATE users SET display_name = COALESCE(?, display_name), "
                "email = COALESCE(?, email) WHERE username = ?",
                (display_name, email, username),
            )
            user = self.get_user(username)
        assert user is not None
        return user

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get a user, with ``settings`` decoded to a dict."""
        row = self._fetchone_as_dict(
            self.conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        )
        if row is not None:
            row["settings"] = json.loads(row["settings"] or "{}")
            row.pop("password_hash", None)
        return row

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Get every user, oldest first, with ``settings`` decoded."""
        rows = self._fetchall_as_dicts(
            self.conn.execute("SELECT * FROM users ORDER BY created_at, username")
        )
        for row in rows:
            row["settings"] = json.loads(row["settings"] or "{}")
            row.pop("password_hash", None)
        return rows

    def count_users(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def get_owner(self) -> Optional[str]:
        """The first admin: the user the CLI and config-file feeds act as."""
        row = self.conn.execute(
            "SELECT username FROM users WHERE role = 'admin' "
            "ORDER BY created_at, username LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def set_user_role(self, username: str, role: str):
        """Make a user an 'admin' or a 'user'."""
        if role not in ("admin", "user"):
            raise ValueError(f"Unknown role: {role}")
        self.conn.execute(
            "UPDATE users SET role = ? WHERE username = ?", (role, username)
        )

    def update_user_settings(self, username: str, settings: Dict[str, Any]):
        """Replace a user's own settings."""
        self.conn.execute(
            "UPDATE users SET settings = ? WHERE username = ?",
            (json.dumps(settings), username),
        )

    def delete_user(self, username: str):
        """Erase a user and everything only they had: podcasts nobody else
        follows and episodes nobody else has, with their transcripts,
        summaries, jobs and audio. Shared ones stay.

        Not one transaction: DuckDB rejects deleting a row and what references
        it in the same transaction. The user's row goes last, so a failure
        partway can simply be retried."""
        episode_ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT episode_id FROM user_episodes WHERE username = ?", (username,)
            ).fetchall()
        ]
        podcast_ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT podcast_id FROM user_podcasts WHERE username = ?", (username,)
            ).fetchall()
        ]
        for table in ("jobs", "sessions", "user_deleted_episodes", "user_episodes"):
            self.conn.execute(f"DELETE FROM {table} WHERE username = ?", (username,))
        # Per-user keys end in ":<username>"; usernames can't contain ":".
        self.conn.execute(
            "DELETE FROM app_state WHERE ends_with(key, ?)", (f":{username}",)
        )
        for podcast_id in podcast_ids:
            if self.unsubscribe(username, podcast_id) == 0:
                for episode in self.get_episodes_by_podcast(podcast_id):
                    self.remove_audio(episode["id"])
                self.delete_podcast(podcast_id)
        for episode_id in episode_ids:
            nobody_has_it = (
                self.conn.execute(
                    "SELECT 1 FROM user_episodes WHERE episode_id = ?", (episode_id,)
                ).fetchone()
                is None
            )
            if nobody_has_it and self.get_episode_by_id(episode_id):
                self.purge_episode(episode_id)
        self.conn.execute("DELETE FROM users WHERE username = ?", (username,))

    def get_password_hash(self, username: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row[0] if row else None

    def set_password_hash(self, username: str, password_hash: str):
        self.conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )

    def create_session(self, token_hash: str, username: str, expires_at: datetime):
        """Store a new session. Expired ones, everyone's, are cleared out on
        the way, so the table doesn't grow for ever."""
        self.conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(),)
        )
        self.conn.execute(
            "INSERT INTO sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
            (token_hash, username, expires_at),
        )

    def get_session_username(self, token_hash: str, now: datetime) -> Optional[str]:
        """The user a session belongs to, or None if unknown or expired."""
        row = self.conn.execute(
            "SELECT username FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
        return row[0] if row else None

    def delete_session(self, token_hash: str):
        self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, username: str, keep: Optional[str] = None):
        """Sign a user out everywhere, except the session ``keep``."""
        self.conn.execute(
            "DELETE FROM sessions WHERE username = ? AND token_hash != ?",
            (username, keep or ""),
        )

    def subscribe(self, username: str, podcast_id: int):
        """Add a podcast to a user's library."""
        self.conn.execute(
            "INSERT OR IGNORE INTO user_podcasts (username, podcast_id) VALUES (?, ?)",
            (username, podcast_id),
        )

    def unsubscribe(self, username: str, podcast_id: int) -> int:
        """Remove a podcast and its episodes from a user's library. Returns
        how many users still subscribe to it."""
        self.conn.execute(
            "DELETE FROM user_podcasts WHERE username = ? AND podcast_id = ?",
            (username, podcast_id),
        )
        self.conn.execute(
            "DELETE FROM user_episodes WHERE username = ? AND episode_id IN "
            "(SELECT id FROM episodes WHERE podcast_id = ?)",
            (username, podcast_id),
        )
        return self.conn.execute(
            "SELECT COUNT(*) FROM user_podcasts WHERE podcast_id = ?", (podcast_id,)
        ).fetchone()[0]

    def is_subscribed(self, username: str, podcast_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM user_podcasts WHERE username = ? AND podcast_id = ?",
                (username, podcast_id),
            ).fetchone()
            is not None
        )

    def link_episode(self, username: str, episode_id: int) -> bool:
        """Add an episode to a user's library, undoing an earlier delete.
        Returns True if it was not there already."""
        self.conn.execute(
            "DELETE FROM user_deleted_episodes WHERE username = ? AND url IN "
            "(SELECT url FROM episodes WHERE id = ?)",
            (username, episode_id),
        )
        if self.user_has_episode(username, episode_id):
            return False
        self.conn.execute(
            "INSERT INTO user_episodes (username, episode_id) VALUES (?, ?)",
            (username, episode_id),
        )
        return True

    def user_has_episode(self, username: str, episode_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM user_episodes WHERE username = ? AND episode_id = ?",
                (username, episode_id),
            ).fetchone()
            is not None
        )

    def hide_episode(self, username: str, episode_id: int) -> int:
        """Delete an episode from a user's library, for good: their fetches
        skip it from now on (the URL is remembered), unless they hand-pick it.
        Once nobody has the episode it is erased with its transcript, summary,
        jobs and audio. Returns how many users still have it."""
        row = self.conn.execute(
            "SELECT e.url, e.title, p.title FROM episodes e "
            "LEFT JOIN podcasts p ON p.id = e.podcast_id WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        self.conn.execute(
            "DELETE FROM user_episodes WHERE username = ? AND episode_id = ?",
            (username, episode_id),
        )
        if row:
            self.conn.execute(
                "INSERT OR REPLACE INTO user_deleted_episodes "
                "(username, url, title, podcast_title, deleted_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (username, *row),
            )
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM user_episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()[0]
        if remaining == 0 and row:
            self.purge_episode(episode_id)
        return remaining

    def get_deleted_episodes(self, username: str) -> List[Dict[str, Any]]:
        """The episodes a user deleted, newest deletion first (those from
        before dates were kept last)."""
        cursor = self.conn.execute(
            "SELECT url, title, podcast_title, deleted_at FROM user_deleted_episodes "
            "WHERE username = ? ORDER BY deleted_at DESC NULLS LAST, title",
            (username,),
        )
        return self._fetchall_as_dicts(cursor)

    def allow_deleted_episode(self, username: str, url: str) -> bool:
        """Forget that the user deleted this episode, so fetches may bring it
        back. Returns False if it was not on their list."""
        if not self.is_url_deleted(username, url):
            return False
        self.conn.execute(
            "DELETE FROM user_deleted_episodes WHERE username = ? AND url = ?",
            (username, url),
        )
        return True

    def is_url_deleted(self, username: str, url: str) -> bool:
        """True if the user deleted the episode with this URL from their library."""
        return (
            self.conn.execute(
                "SELECT 1 FROM user_deleted_episodes WHERE username = ? AND url = ?",
                (username, url),
            ).fetchone()
            is not None
        )
