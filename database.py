from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


def strip_special_tokens(text: str) -> str:
    for token in SPECIAL_TOKENS:
        text = text.replace(token, "")
    return text


def longest_common_part(a: str, b: str) -> str:
    """最长公共子串，用于判断散页释文与现有对齐是否重叠。"""
    best_len = best_end = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, best_end = cur[j], i
        prev = cur
    return a[best_end - best_len:best_end]


class CollationDB:
    """SQLite-backed textual collation service with optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id, revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recovered_leaves (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              leaf_no TEXT NOT NULL,
              image_ref TEXT NOT NULL,
              transcription TEXT NOT NULL,
              overlap_text TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending_review'
                CHECK(status IN ('pending_recollate','pending_review','applied','rejected')),
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              reviewed_by INTEGER REFERENCES users(id),
              reviewed_at TEXT,
              UNIQUE(witness_id,leaf_no)
            );
            CREATE TABLE IF NOT EXISTS alignment_archives (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              leaf_id INTEGER NOT NULL REFERENCES recovered_leaves(id) ON DELETE CASCADE,
              passage_id INTEGER NOT NULL,
              witness_id INTEGER NOT NULL,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              variants_json TEXT NOT NULL DEFAULT '[]',
              archived_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        variant_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(variants)")}
        if "status" not in variant_cols:
            self.conn.execute("ALTER TABLE variants ADD COLUMN status TEXT NOT NULL DEFAULT 'current'")
            self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)
        self.register_recovered_leaf(w2, passage, "叶二", "IMG-2026-017", "春水东流，故人南去。", editor)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, lock = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            if self._pending_leaves(passage_id, witness_id):
                raise DomainError("该版本存在待重校的散页回补，暂不能新增异文")
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), user_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), passage_id))
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            if variant["status"] != "current":
                raise DomainError("该异文已随散页回补留档，不能再修订")
            passage, _ = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, datetime.now().isoformat(), variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
        return revision

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, None

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return revision

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        if self._pending_leaves(passage_id):
            raise DomainError("段落含待重校内容，不能生成定本")
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )

    def register_recovered_leaf(self, witness_id: int, passage_id: int, leaf_no: str, image_ref: str,
                                transcription: str, user_id: int) -> int:
        """登记找回的散页：版本、叶序、影像编号、释文。与现有对齐重叠的部分先留在待重校。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能登记散页回补")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if not leaf_no.strip() or not image_ref.strip():
            raise DomainError("叶序与影像编号不能为空")
        text = validate_transcription(transcription)
        existing = self.conn.execute(
            "SELECT aligned_text FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)
        ).fetchone()
        overlap = longest_common_part(strip_special_tokens(existing["aligned_text"]), strip_special_tokens(text)) if existing else ""
        status = "pending_recollate" if overlap else "pending_review"
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO recovered_leaves(work_id,passage_id,witness_id,leaf_no,image_ref,transcription,overlap_text,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (passage["work_id"], passage_id, witness_id, leaf_no.strip(), image_ref.strip(), text, overlap, status, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本此叶已有回补登记") from exc
        return int(cur.lastrowid)

    def review_recovered_leaf(self, leaf_id: int, user_id: int, approve: bool = True) -> str:
        """负责人复核：通过才替换该叶，原对齐和旧结论留档。"""
        leaf = self.conn.execute("SELECT * FROM recovered_leaves WHERE id=?", (leaf_id,)).fetchone()
        if not leaf:
            raise DomainError("散页回补记录不存在")
        self._require_owner(leaf["work_id"], user_id)
        if leaf["status"] not in {"pending_recollate", "pending_review"}:
            raise DomainError("该散页已完成复核，不能重复处理")
        now = datetime.now().isoformat()
        with self.transaction():
            if not approve:
                self.conn.execute(
                    "UPDATE recovered_leaves SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?",
                    (user_id, now, leaf_id),
                )
                return "rejected"
            alignment = self.conn.execute(
                "SELECT * FROM alignments WHERE passage_id=? AND witness_id=?", (leaf["passage_id"], leaf["witness_id"])
            ).fetchone()
            variants = [dict(r) for r in self.conn.execute(
                "SELECT * FROM variants WHERE passage_id=? AND witness_id=? AND status='current' ORDER BY id",
                (leaf["passage_id"], leaf["witness_id"]),
            ).fetchall()]
            if alignment:
                self.conn.execute(
                    "INSERT INTO alignment_archives(leaf_id,passage_id,witness_id,aligned_text,sort_order,note,created_by,created_at,variants_json,archived_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (leaf_id, leaf["passage_id"], leaf["witness_id"], alignment["aligned_text"], alignment["sort_order"],
                     alignment["note"], alignment["created_by"], alignment["created_at"], json.dumps(variants, ensure_ascii=False), now),
                )
                self.conn.execute(
                    "UPDATE alignments SET aligned_text=?,note=? WHERE id=?",
                    (leaf["transcription"], f"散页回补{leaf['leaf_no']}替换", alignment["id"]),
                )
            else:
                sort_order = int(self.conn.execute(
                    "SELECT COALESCE(MAX(sort_order),0)+1 FROM alignments WHERE passage_id=?", (leaf["passage_id"],)
                ).fetchone()[0])
                self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (leaf["passage_id"], leaf["witness_id"], leaf["transcription"], sort_order, f"散页回补{leaf['leaf_no']}", user_id, now),
                )
            if variants:
                self.conn.execute(
                    "UPDATE variants SET status='archived' WHERE passage_id=? AND witness_id=? AND status='current'",
                    (leaf["passage_id"], leaf["witness_id"]),
                )
            self.conn.execute(
                "UPDATE recovered_leaves SET status='applied',reviewed_by=?,reviewed_at=? WHERE id=?",
                (user_id, now, leaf_id),
            )
        return "applied"

    def _pending_leaves(self, passage_id: int, witness_id: int | None = None) -> list[dict]:
        sql = ("SELECT l.*,w.siglum,w.source_note FROM recovered_leaves l JOIN witnesses w ON w.id=l.witness_id "
               "WHERE l.passage_id=? AND l.status IN ('pending_recollate','pending_review')")
        params: list = [passage_id]
        if witness_id is not None:
            sql += " AND l.witness_id=?"
            params.append(witness_id)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY l.id", params).fetchall()]

    def _leaf_blockers(self, passage_id: int) -> list[dict]:
        blockers = []
        for leaf in self._pending_leaves(passage_id):
            reason = "散页释文与现有对齐重叠，待重校" if leaf["status"] == "pending_recollate" else "散页回补登记待负责人复核"
            blockers.append({
                "leaf_id": leaf["id"],
                "leaf_no": leaf["leaf_no"],
                "source": f"{leaf['siglum']}（{leaf['source_note'] or '来源未注明'}）",
                "image_ref": leaf["image_ref"],
                "reason": reason,
            })
        return blockers

    def export_definitive(self, work_id: int, user_id: int) -> dict:
        """生成定本：含待重校内容的段落被阻挡，并给出叶序、来源和阻挡原因。"""
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        passages = []
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            blockers = self._leaf_blockers(passage["id"])
            blocked = bool(blockers)
            variants = [dict(r) for r in self.conn.execute(
                "SELECT * FROM variants WHERE passage_id=? AND status='current' ORDER BY witness_id,layer,id", (passage["id"],)
            ).fetchall()]
            alignments = [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage["id"],),
            ).fetchall()]
            passages.append({
                "id": passage["id"],
                "label": passage["label"],
                "status": passage["status"],
                "blocked": blocked,
                "blockers": blockers,
                "definitive_text": None if blocked else passage["base_text"],
                "alignments": alignments,
                "variants": variants,
            })
        return {"work": dict(work), "can_generate": all(not p["blocked"] for p in passages), "passages": passages}

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        gaps = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? AND status='current' ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            archived_variants = [dict(r) for r in self.conn.execute(
                "SELECT * FROM variants WHERE passage_id=? AND status='archived' ORDER BY id", (passage["id"],)
            ).fetchall()]
            leaves = [dict(r) for r in self.conn.execute(
                "SELECT l.*,w.siglum FROM recovered_leaves l JOIN witnesses w ON w.id=l.witness_id WHERE l.passage_id=? ORDER BY l.id",
                (passage["id"],),
            ).fetchall()]
            passages.append({**dict(passage), "alignments": alignments, "variants": variants,
                             "archived_variants": archived_variants, "recovered_leaves": leaves})
        return {"work": dict(work), "witnesses": witnesses, "passages": passages, "gap_count": gaps}

    def snapshot(self) -> dict:
        archives = []
        for row in self.conn.execute("SELECT * FROM alignment_archives ORDER BY id"):
            item = dict(row)
            item["variants"] = json.loads(item.pop("variants_json"))
            archives.append(item)
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "recovered_leaves": [dict(r) for r in self.conn.execute("SELECT * FROM recovered_leaves ORDER BY id")],
            "alignment_archives": archives,
        }
