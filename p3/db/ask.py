"""Ask: the passage index (passages, embeddings and BM25 terms) and the
conversations (threads and rated answers). See p3/ask.py."""

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .search import Search


class Ask(Search):
    # ------------------------------------------------------------------
    # Ask index: passages, embeddings and BM25 terms
    # ------------------------------------------------------------------

    def replace_chunks(
        self,
        episode_id: int,
        chunks: List[Dict[str, Any]],
        embeddings: Optional[List[List[float]]],
        embed_model: Optional[str],
    ) -> int:
        """Replace an episode's passages (from ``chunker.build_chunks``) and
        their BM25 terms. ``embeddings`` match ``chunks`` in order; None
        stores keyword-searchable passages only. Returns the count stored."""
        from p3.chunker import term_counts

        # One transaction: a kill midway must not leave passages without
        # their terms (they would look indexed but never match a search).
        with self._chunk_lock, self.transaction():
            return self._replace_chunks(
                episode_id, chunks, embeddings, embed_model, term_counts
            )

    def _replace_chunks(
        self,
        episode_id: int,
        chunks: List[Dict[str, Any]],
        embeddings: Optional[List[List[float]]],
        embed_model: Optional[str],
        term_counts: Any,
    ) -> int:
        self.delete_chunks(episode_id)
        # Erased while it was being indexed (purge takes the same lock).
        exists = self.conn.execute(
            "SELECT 1 FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if not chunks or not exists:
            return 0
        # Ids are never reused (stored answers refer to them): the next one is
        # past both the current rows and the highest id ever handed out.
        high = int(self.get_state("chunk_id_high") or 0)
        first = (
            max(
                high,
                self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM chunks").fetchone()[
                    0
                ],
            )
            + 1
        )
        self.set_state("chunk_id_high", str(first + len(chunks) - 1))
        term_rows: Tuple[List[int], List[str], List[int]] = ([], [], [])
        rows = []
        for i, chunk in enumerate(chunks):
            counts = term_counts(f"{chunk.get('prefix') or ''} {chunk['text']}")
            for term, tf in counts.items():
                term_rows[0].append(first + i)
                term_rows[1].append(term)
                term_rows[2].append(tf)
            rows.append(
                (
                    first + i,
                    episode_id,
                    chunk["seq"],
                    chunk["kind"],
                    chunk.get("start_s"),
                    chunk.get("end_s"),
                    chunk["text"],
                    chunk.get("prefix"),
                    chunk.get("language"),
                    sum(counts.values()),
                    embeddings[i] if embeddings else None,
                    embed_model if embeddings else None,
                    chunk.get("speakers"),
                )
            )
        self.conn.executemany(
            "INSERT INTO chunks (id, episode_id, seq, kind, start_s, end_s, "
            "text, prefix, language, n_terms, embedding, embed_model, speakers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        if term_rows[0]:
            # One statement for every term: unnest zips the lists.
            self.conn.execute(
                "INSERT INTO chunk_terms SELECT unnest(?::INTEGER[]), ?, "
                "unnest(?::VARCHAR[]), unnest(?::INTEGER[])",
                [term_rows[0], episode_id, term_rows[1], term_rows[2]],
            )
        return len(rows)

    def delete_chunks(self, episode_id: int):
        self.conn.execute("DELETE FROM chunk_terms WHERE episode_id = ?", (episode_id,))
        self.conn.execute("DELETE FROM chunks WHERE episode_id = ?", (episode_id,))

    def episodes_to_index(self, embed_model: Optional[str]) -> List[int]:
        """Processed episodes whose passages are missing, or were embedded with
        another model (or not at all, when ``embed_model`` is given)."""
        # Show notes stored after the episode was indexed also make it stale.
        notes_missing = (
            "(ANY_VALUE(e.description) IS NOT NULL AND ANY_VALUE(e.description) <> ''"
            " AND COUNT(*) FILTER (c.kind = 'notes') = 0)"
        )
        if embed_model:
            stale = (
                "COUNT(c.id) = 0 OR COUNT(*) FILTER "
                f"(c.embed_model IS DISTINCT FROM ?) > 0 OR {notes_missing}"
            )
            params: List[Any] = [embed_model]
        else:
            stale = f"COUNT(c.id) = 0 OR {notes_missing}"
            params = []
        rows = self.conn.execute(
            f"""
            SELECT e.id FROM episodes e LEFT JOIN chunks c ON c.episode_id = e.id
            WHERE e.status = 'processed'
            GROUP BY e.id HAVING {stale}
            ORDER BY e.id
        """,
            params,
        ).fetchall()
        return [r[0] for r in rows]

    def index_stats(self, username: Optional[str] = None) -> Dict[str, Any]:
        """Passage counts for a user's library (everyone's without one)."""
        join, params = self._user_episode_join(username)
        row = self.conn.execute(
            "SELECT COUNT(c.id), COUNT(DISTINCT c.episode_id), "
            "COUNT(c.id) FILTER (c.embedding IS NOT NULL) "
            f"FROM chunks c JOIN episodes e ON e.id = c.episode_id {join}",
            params,
        ).fetchone()
        models = self.conn.execute(
            "SELECT c.embed_model, COUNT(*) "
            f"FROM chunks c JOIN episodes e ON e.id = c.episode_id {join} "
            "GROUP BY 1 ORDER BY 2 DESC",
            params,
        ).fetchall()
        return {
            "chunks": row[0],
            "episodes": row[1],
            "embedded": row[2],
            "models": {m or "none": n for m, n in models},
            "processed_episodes": self.conn.execute(
                f"SELECT COUNT(*) FROM episodes e {join} WHERE e.status = 'processed'",
                params,
            ).fetchone()[0],
        }

    def _chunk_scope(
        self, username: Optional[str], filters: Optional[Dict[str, Any]]
    ) -> Tuple[str, List[Any]]:
        """JOIN/WHERE text limiting ``c`` (chunks) to a user's episodes and
        the optional filters: ``podcast_ids``, ``after`` / ``before`` (dates,
        inclusive), ``language``, ``kinds``. ``e`` is the episode."""
        sql = "JOIN episodes e ON e.id = c.episode_id "
        params: List[Any] = []
        if username is not None:
            sql += "JOIN user_episodes ue ON ue.episode_id = e.id AND ue.username = ? "
            params.append(username)
        where = ["1 = 1"]
        filters = filters or {}
        # Keys other than the ones below (e.g. "chosen") are ignored.
        if filters.get("podcast_ids"):
            where.append("e.podcast_id IN (SELECT unnest(?::INTEGER[]))")
            params.append(list(filters["podcast_ids"]))
        if filters.get("after"):
            where.append("e.date >= ?::DATE")
            params.append(str(filters["after"]))
        if filters.get("before"):
            where.append("e.date < ?::DATE + INTERVAL 1 DAY")
            params.append(str(filters["before"]))
        if filters.get("language"):
            where.append("e.language = ?")
            params.append(filters["language"])
        if filters.get("kinds"):
            where.append("c.kind IN (SELECT unnest(?::VARCHAR[]))")
            params.append(list(filters["kinds"]))
        return sql + "WHERE " + " AND ".join(where), params

    def bm25_search(
        self,
        terms: List[str],
        username: Optional[str],
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> List[Tuple[int, float]]:
        """(chunk id, BM25 score) of the best passages for ``terms`` (already
        tokenized), best first, within the user's library. Term statistics
        are computed over that same library and filters, so the ranking
        reveals nothing about other users' passages."""
        terms = sorted(set(terms))
        if not terms:
            return []
        scope, params = self._chunk_scope(username, filters)
        rows = self.conn.execute(
            f"""
            WITH q AS (SELECT unnest(?::VARCHAR[]) AS term),
            lib AS (SELECT c.id, c.n_terms FROM chunks c {scope}),
            stats AS (SELECT COUNT(*) AS n, AVG(n_terms) AS avgdl FROM lib),
            hits AS (
                SELECT t.chunk_id, t.term, t.tf, l.n_terms FROM chunk_terms t
                JOIN q ON q.term = t.term
                JOIN lib l ON l.id = t.chunk_id
            ),
            df AS (SELECT term, COUNT(*) AS df FROM hits GROUP BY term)
            SELECT h.chunk_id, SUM(
                LN(1 + (s.n - df.df + 0.5) / (df.df + 0.5))
                * (h.tf * (? + 1))
                / (h.tf + ? * (1 - ? + ? * h.n_terms / s.avgdl))
            ) AS score
            FROM hits h
            JOIN df ON df.term = h.term
            CROSS JOIN stats s
            GROUP BY h.chunk_id
            ORDER BY score DESC, h.chunk_id
            LIMIT ?
        """,
            [terms, *params, k1, k1, b, b, limit],
        ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def mention_counts(
        self,
        variants: List[List[str]],
        username: Optional[str],
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Every episode of the user's library whose transcript mentions any
        of ``variants`` (each a list of tokenized words, all of which must be
        in the same passage), with how many of its transcript passages do,
        newest first. For "how often" and "which episodes" questions, which
        the top passages can't answer."""
        pairs = [(i, t) for i, words in enumerate(variants) for t in set(words)]
        if not pairs:
            return []
        scope, params = self._chunk_scope(
            username, {**(filters or {}), "kinds": ["transcript"]}
        )
        cursor = self.conn.execute(
            f"""
            WITH v AS (
                SELECT unnest(?::INTEGER[]) AS vid, unnest(?::VARCHAR[]) AS term
            ),
            need AS (SELECT vid, COUNT(*) AS n FROM v GROUP BY vid),
            lib AS (SELECT c.id, c.episode_id FROM chunks c {scope}),
            hits AS (
                SELECT t.chunk_id, v.vid, COUNT(DISTINCT v.term) AS got
                FROM chunk_terms t
                JOIN v ON v.term = t.term
                JOIN lib l ON l.id = t.chunk_id
                GROUP BY 1, 2
            ),
            matched AS (
                SELECT DISTINCT h.chunk_id FROM hits h
                JOIN need ON need.vid = h.vid WHERE h.got = need.n
            )
            SELECT l.episode_id, COUNT(*) AS passages,
                   e.title AS episode_title, e.date AS episode_date,
                   e.podcast_id, p.title AS podcast_title
            FROM matched m
            JOIN lib l ON l.id = m.chunk_id
            JOIN episodes e ON e.id = l.episode_id
            JOIN podcasts p ON p.id = e.podcast_id
            GROUP BY l.episode_id, e.title, e.date, e.podcast_id, p.title
            ORDER BY e.date DESC NULLS LAST, l.episode_id DESC
        """,
            [[i for i, _ in pairs], [t for _, t in pairs], *params],
        )
        return self._fetchall_as_dicts(cursor)

    def vector_search(
        self,
        vector: List[float],
        embed_model: str,
        username: Optional[str],
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
    ) -> List[Tuple[int, float]]:
        """(chunk id, cosine similarity) of the passages nearest ``vector``,
        best first, among those embedded with ``embed_model``."""
        scope, params = self._chunk_scope(username, filters)
        rows = self.conn.execute(
            f"""
            SELECT c.id, list_cosine_similarity(c.embedding, ?::FLOAT[]) AS score
            FROM chunks c {scope} AND c.embed_model = ?
            ORDER BY score DESC, c.id
            LIMIT ?
        """,
            [vector, *params, embed_model, limit],
        ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def get_chunks(self, chunk_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Passages by id, with their episode and podcast for citations."""
        if not chunk_ids:
            return {}
        cursor = self.conn.execute(
            """
            SELECT c.id, c.episode_id, c.seq, c.kind, c.start_s, c.end_s, c.text,
                   c.language, c.speakers, e.title AS episode_title,
                   e.date AS episode_date, e.podcast_id, p.title AS podcast_title
            FROM chunks c
            JOIN episodes e ON e.id = c.episode_id
            JOIN podcasts p ON p.id = e.podcast_id
            WHERE c.id IN (SELECT unnest(?::INTEGER[]))
        """,
            [list(chunk_ids)],
        )
        return {row["id"]: row for row in self._fetchall_as_dicts(cursor)}

    def recent_topics(self, username: Optional[str], limit: int = 3) -> List[str]:
        """The first key topic of the user's latest digests, distinct, for
        example questions."""
        join, params = self._user_episode_join(username)
        rows = self.conn.execute(
            f"""
            SELECT s.key_topics FROM summaries s
            JOIN episodes e ON e.id = s.episode_id {join}
            ORDER BY s.created_at DESC LIMIT 30
        """,
            params,
        ).fetchall()
        topics: List[str] = []
        for (raw,) in rows:
            try:
                values = json.loads(raw) if isinstance(raw, str) else raw or []
            except ValueError:
                continue
            for topic in values[:1]:
                topic = str(topic).strip()
                if 3 <= len(topic) <= 60 and topic.lower() not in {
                    t.lower() for t in topics
                }:
                    topics.append(topic)
            if len(topics) >= limit:
                break
        return topics

    def summary_chunks(self, episode_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Each listed episode's summary passage (as ``get_chunks`` returns
        it), by episode id."""
        if not episode_ids:
            return {}
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE kind = 'summary' "
                "AND episode_id IN (SELECT unnest(?::INTEGER[]))",
                [list(episode_ids)],
            ).fetchall()
        ]
        return {c["episode_id"]: c for c in self.get_chunks(ids).values()}

    # ------------------------------------------------------------------
    # Ask conversations
    # ------------------------------------------------------------------

    def create_ask_thread(self, username: str, title: str) -> str:
        thread_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO ask_threads (id, username, title) VALUES (?, ?, ?)",
            (thread_id, username, title[:200]),
        )
        return thread_id

    def get_ask_thread(self, thread_id: str, username: str) -> Optional[Dict[str, Any]]:
        """A user's thread with its messages in order, or None (also for
        someone else's thread)."""
        thread = self._fetchone_as_dict(
            self.conn.execute(
                "SELECT * FROM ask_threads WHERE id = ? AND username = ?",
                (thread_id, username),
            )
        )
        if not thread:
            return None
        messages = self._fetchall_as_dicts(
            self.conn.execute(
                "SELECT * FROM ask_messages WHERE thread_id = ? "
                "ORDER BY created_at, id",
                (thread_id,),
            )
        )
        for m in messages:
            for key in ("citations", "retrieval", "timings", "models"):
                if isinstance(m.get(key), str):
                    m[key] = json.loads(m[key])
        thread["messages"] = messages
        return thread

    def list_ask_threads(self, username: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._fetchall_as_dicts(
            self.conn.execute(
                """
                SELECT t.id, t.title, t.created_at,
                       MAX(m.created_at) AS updated_at, COUNT(m.id) AS message_count
                FROM ask_threads t LEFT JOIN ask_messages m ON m.thread_id = t.id
                WHERE t.username = ?
                GROUP BY t.id, t.title, t.created_at
                ORDER BY COALESCE(MAX(m.created_at), t.created_at) DESC
                LIMIT ?
            """,
                (username, limit),
            )
        )

    def add_ask_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        citations: Optional[list] = None,
        retrieval: Optional[dict] = None,
        timings: Optional[dict] = None,
        models: Optional[dict] = None,
    ) -> str:
        message_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO ask_messages (id, thread_id, role, content, citations, "
            "retrieval, timings, models) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                thread_id,
                role,
                content,
                json.dumps(citations) if citations is not None else None,
                json.dumps(retrieval, default=str) if retrieval is not None else None,
                json.dumps(timings) if timings is not None else None,
                json.dumps(models) if models is not None else None,
            ),
        )
        return message_id

    def close_unanswered_ask_turns(self, note: str) -> int:
        """Give every question left without a reply (the server stopped while
        answering it) a stored reply saying so, so a thread never holds two
        user turns in a row. Returns how many were closed."""
        rows = self.conn.execute("""
            SELECT thread_id FROM (
                SELECT thread_id, role,
                       ROW_NUMBER() OVER (
                           PARTITION BY thread_id ORDER BY created_at DESC, id DESC
                       ) AS rn
                FROM ask_messages
            ) WHERE rn = 1 AND role = 'user'
        """).fetchall()
        for (thread_id,) in rows:
            self.add_ask_message(
                thread_id, "assistant", note, retrieval={"mode": "interrupted"}
            )
        return len(rows)

    def delete_ask_thread(self, thread_id: str, username: str) -> bool:
        if not self.get_ask_thread(thread_id, username):
            return False
        self.conn.execute("DELETE FROM ask_messages WHERE thread_id = ?", (thread_id,))
        self.conn.execute("DELETE FROM ask_threads WHERE id = ?", (thread_id,))
        return True

    def rate_ask_message(self, message_id: str, username: str, rating: int) -> bool:
        """Set the thumbs up (1) / down (-1) / none (0) of one of the user's
        answers. False if it isn't theirs."""
        owned = self.conn.execute(
            "SELECT 1 FROM ask_messages m JOIN ask_threads t ON t.id = m.thread_id "
            "WHERE m.id = ? AND t.username = ? AND m.role = 'assistant'",
            (message_id, username),
        ).fetchone()
        if not owned:
            return False
        self.conn.execute(
            "UPDATE ask_messages SET rating = ? WHERE id = ?",
            (rating or None, message_id),
        )
        return True

    def rated_ask_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        rows = self.rated_ask_messages(1, message_id)
        return rows[0] if rows else None

    def rated_ask_messages(
        self, limit: int = 50, message_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Answers users rated, newest first, each with the question asked
        just before it (for the Admin page and the evaluation set)."""
        cursor = self.conn.execute(
            """
            SELECT m.id, m.thread_id, t.username, m.content AS answer, m.rating,
                   m.citations, m.created_at,
                   (SELECT q.content FROM ask_messages q
                    WHERE q.thread_id = m.thread_id AND q.role = 'user'
                      AND q.created_at <= m.created_at
                    ORDER BY q.created_at DESC LIMIT 1) AS question
            FROM ask_messages m JOIN ask_threads t ON t.id = m.thread_id
            WHERE m.role = 'assistant' AND m.rating IS NOT NULL
              AND (? IS NULL OR m.id = ?)
            ORDER BY m.created_at DESC
            LIMIT ?
        """,
            (message_id, message_id, limit),
        )
        rows = self._fetchall_as_dicts(cursor)
        for row in rows:
            if isinstance(row.get("citations"), str):
                row["citations"] = json.loads(row["citations"])
        return rows
