"""事件关联图：把"记下的事"和"记得的事实"用人、主题、同场提及连起来。

为什么要有这一层：主动关心触发时，引擎原来只拿得到一条事项的标题（"妈妈生日"），
说出来的话就像日历通知；而"妈妈喜欢养花""上次你说想送她一盆栀子花"其实都在长期记忆里。
关联层负责在不打断心跳的前提下，把这些邻居找出来喂给措辞。

三条设计约束（都是这台机器上踩出来的）：
- **纯规则抽枢纽，不调模型**：中转随时会挂，挂了也不能突然不会关心人。
- **只存"节点 → 枢纽"的有向边**："两件事相关"是查询时沿枢纽走两步得到的，
  所以新记一条事实不需要和全库比一次，边数只跟枢纽数走。
- **到处都出现的枢纽要降权**：像"生日""工作"这种连着几百条的枢纽不能什么都能连，
  否则关联等于噪声发生器。
"""

import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "moz.db")

ITEM = "item"
MEMORY = "memory"
PERSON = "person"
TOPIC = "topic"

REL_ABOUT = "about"        # 节点 → 人
REL_TOPIC = "topic"        # 节点 → 主题
REL_TOGETHER = "together"  # 同一次对话里一起记下的两件事

REL_WEIGHT = {REL_ABOUT: 1.0, REL_TOPIC: 0.55, REL_TOGETHER: 0.8}
# 连着这么多条以上的枢纽当成通用词，直接不参与关联
HUB_DEGREE_CAP = int(os.environ.get("MOZ_CARE_HUB_DEGREE_CAP", "60"))
# 每个枢纽最多往回展开多少邻居，防止一个热枢纽拖垮心跳
HUB_NEIGHBOR_CAP = 60
TOGETHER_WINDOW = 5.0  # 同一次抽取的 created_at 间隔（秒）

# 称谓比名字可靠，优先按长度匹配，命中后不再从这段文字里抽别的
PERSON_WORDS = (
    "姥姥", "奶奶", "爷爷", "外公", "外婆", "爸爸", "妈妈", "父亲", "母亲",
    "嫂子", "姐夫", "妹夫", "弟弟", "哥哥", "姐姐", "妹妹", "表姐", "表妹",
    "堂哥", "表弟", "阿姨", "舅舅", "姑妈", "叔叔", "婆婆", "岳父", "岳母",
    "老公", "老婆", "对象", "媳妇", "丈夫", "爱人", "儿子", "女儿", "孩子",
    "导师", "老师", "老板", "组长", "主管", "同事", "朋友", "闺蜜", "室友",
    "医生", "牙医", "房东", "中介", "客户",
)
# 称谓比名字可靠。名字式模式只在"姓 + 称呼"上认，否则"三个哥哥""小姐姐"都会连出错
_PERSON_STOP = {"小姐", "小憩", "小休", "小伙", "小哥", "大姐", "大哥", "老哥", "老大",
                "老家", "老头", "老人", "老婆婆", "同事们"}
_KIN_ALIAS = re.compile(
    # "我妈生日"里的"妈"要归成"妈妈"：同一个人被劈成两个枢纽，关联就断了
    r"(?:我|咱|你|她|他|咱们)(姥姥|奶奶|外公|外婆|爷爷|阿姨|姑姑|妈|爸|哥|姐|弟|妹|爷|奶|叔|舅)")
_KIN_FULL = {"妈": "妈妈", "爸": "爸爸", "哥": "哥哥", "姐": "姐姐", "弟": "弟弟", "妹": "妹妹",
             "爷": "爷爷", "奶": "奶奶", "叔": "叔叔", "舅": "舅舅"}
_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶"
    "程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾"
    "毛郝龚邵万钱严覃武戴莫孔向汤")
_PERSON_PATTERNS = (
    re.compile(r"[老小阿][\u4e00-\u9fff]"),        # 老郑 / 小林 / 阿伟
    re.compile(r"[\u4e00-\u9fff][总裁工哥姐叔姨婶]"),  # 郑总 / 张工 / 王哥
)

TOPIC_WORDS = (
    "生日", "纪念日", "过生", "体检", "复诊", "复查", "手术", "打针", "疫苗", "吃药",
    "牙医", "看牙", "答辩", "面试", "考试", "笔试", "汇报", "述职", "上线", "交付",
    "项目", "截止", "deadline", "搬家", "租房", "续约", "合同", "换证", "驾照",
    "社保", "公积金", "居住证", "签证", "护照", "银行卡", "缴费", "还贷款", "车险",
    "孩子上学", "入园", "毕业", "论文", "婚礼", "相亲", "聚会", "旅行", "出差",
    "入职", "离职", "跳槽", "升职", "涨薪", "养花", "猫", "狗", "宠物", "健身",
    "复查", "开庭", "维权", "投诉", "报修", "快递", "生日蛋糕", "体检报告",
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())


def extract_persons(text: str) -> List[str]:
    """从一句话里挑出"这件事关于谁"。宁可漏，别乱连——连错了比连不上更糟。"""
    if not text:
        return []
    found: List[str] = []

    def push(token: str) -> None:
        if token and token not in _PERSON_STOP and token not in found:
            found.append(token)

    for word in PERSON_WORDS:
        start = 0
        while True:
            at = text.find(word, start)
            if at < 0:
                break
            # "小姐姐""大哥哥"里的"姐姐/哥哥"不是同一个人
            if not at or text[at - 1] not in "小大阿老":
                push(word)
                break
            start = at + 1
    for m in _KIN_ALIAS.finditer(text):
        push(_KIN_FULL.get(m.group(1), m.group(1)))
    for pattern in _PERSON_PATTERNS:
        for m in pattern.finditer(text):
            token = m.group(0)
            if pattern.pattern.startswith(r"[\u4e00") and token[0] not in _SURNAMES:
                continue  # 只认"姓 + 称呼"，否则"个哥""位总"也算人
            push(token)
    return found[:3]


def extract_topics(text: str) -> List[str]:
    if not text:
        return []
    lowered = (text or "").lower()
    return [w for w in TOPIC_WORDS if w in lowered][:4]


def tags_for(text: str) -> List[Tuple[str, str, str, float]]:
    """→ [(关系, 枢纽类型, 枢纽 id, 权重)]"""
    tags = [(REL_ABOUT, PERSON, p, REL_WEIGHT[REL_ABOUT]) for p in extract_persons(text)]
    tags += [(REL_TOPIC, TOPIC, t, REL_WEIGHT[REL_TOPIC]) for t in extract_topics(text)]
    return tags


class CareGraph:
    """线程安全的关联存储（与关心库同一个 moz.db，连接按线程复用）。"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        self._conn().executescript(
            """
            CREATE TABLE IF NOT EXISTS care_links (
                user_id TEXT NOT NULL,
                src_type TEXT NOT NULL,
                src_id TEXT NOT NULL,
                rel TEXT NOT NULL,
                dst_type TEXT NOT NULL,
                dst_id TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                label TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(user_id, src_type, src_id, rel, dst_type, dst_id)
            );
            CREATE INDEX IF NOT EXISTS idx_care_links_src ON care_links(user_id, src_type, src_id);
            CREATE INDEX IF NOT EXISTS idx_care_links_dst ON care_links(user_id, dst_type, dst_id);

            CREATE TABLE IF NOT EXISTS care_graph_state (
                user_id TEXT PRIMARY KEY,
                item_at REAL NOT NULL DEFAULT 0,
                memory_at REAL NOT NULL DEFAULT 0,
                synced_at REAL NOT NULL DEFAULT 0
            );
            """
        )
        self._conn().commit()

    # ── 写入 ─────────────────────────────────────────────
    def _replace_edges(self, user_id: str, src_type: str, src_id: str,
                       rows: Sequence[Tuple[str, str, str, float]], label: str, kind: str) -> int:
        """一个节点的所有边整批重写：枢纽词表以后改了，老边会跟着换掉而不是留下对的。"""
        conn = self._conn()
        conn.execute(
            "DELETE FROM care_links WHERE user_id = ? AND src_type = ? AND src_id = ? AND rel != ?",
            (user_id, src_type, src_id, REL_TOGETHER),
        )
        now = time.time()
        for rel, dst_type, dst_id, weight in rows:
            conn.execute(
                "INSERT OR REPLACE INTO care_links"
                "(user_id,src_type,src_id,rel,dst_type,dst_id,weight,label,kind,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user_id, src_type, src_id, rel, dst_type, dst_id, weight, label[:80], kind, now),
            )
        conn.commit()
        return len(rows)

    def link_item_by_time(self, user_id: str, item_id: str, created_at: float) -> int:
        """同一句话里一次记下的几件事本来就有语境关系：连一条直连边。"""
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO care_links"
            " (user_id,src_type,src_id,rel,dst_type,dst_id,weight,label,kind,created_at)"
            " SELECT ?,?,?,?,?, 'item', o.id, o.title, o.kind, ? FROM care_items o"
            " WHERE o.user_id = ? AND o.source = 'auto' AND o.id <> ?"
            "   AND o.status = 'active' AND o.created_at BETWEEN ? AND ?",
            (user_id, ITEM, item_id, REL_TOGETHER, REL_WEIGHT[REL_TOGETHER], time.time(),
             user_id, item_id, created_at - TOGETHER_WINDOW, created_at + TOGETHER_WINDOW),
        )
        conn.commit()
        return max(cur.rowcount, 0)

    def sync_item(self, user_id: str, item: Dict[str, Any]) -> int:
        text = f"{item.get('title', '')} {item.get('detail', '')}"
        return self._replace_edges(
            user_id, ITEM, item["id"], tags_for(text),
            label=item.get("title", ""), kind=item.get("kind", "event"),
        )

    def sync_memory(self, user_id: str, memory_id: str, content: str) -> int:
        """一条事实进图时只抽它自己的枢纽，不和全库比。"""
        return self._replace_edges(
            user_id, MEMORY, memory_id, tags_for(content),
            label=_norm(content)[:40], kind="fact",
        )

    def forget(self, user_id: str, src_type: str, src_id: str) -> int:
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM care_links WHERE user_id = ? AND src_type = ? AND src_id = ?",
            (user_id, src_type, src_id),
        )
        conn.execute(
            "DELETE FROM care_links WHERE user_id = ? AND dst_type = ? AND dst_id = ?",
            (user_id, src_type, src_id),
        )
        conn.commit()
        return max(cur.rowcount, 0)

    def clear_user(self, user_id: str) -> int:
        """「清空我记下的事」要连关联一起清掉，并把水位归零。"""
        conn = self._conn()
        cur = conn.execute("DELETE FROM care_links WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM care_graph_state WHERE user_id = ?", (user_id,))
        conn.commit()
        return max(cur.rowcount, 0)

    def reap(self, user_id: str) -> int:
        """删掉/归档/被替代的节点不能继续当邻居：不然 moz 会引用用户已经抹掉的事。

        归档和软删只改 JSON 里的 status，没有单独的表可查，所以直接 json_extract 比对。
        中途出错必须回滚：留着半个写事务的话，同库的其它写入会被"database is locked"挡死。
        """
        conn = self._conn()
        has_memories = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
        before = conn.execute("SELECT COUNT(*) FROM care_links WHERE user_id = ?",
                              (user_id,)).fetchone()[0]
        statements = [
            ("DELETE FROM care_links WHERE user_id = ? AND src_type = ? AND src_id NOT IN"
             " (SELECT id FROM care_items WHERE user_id = ?)", (user_id, ITEM, user_id)),
            ("DELETE FROM care_links WHERE user_id = ? AND rel = ? AND dst_type = ? AND dst_id NOT IN"
             " (SELECT id FROM care_items WHERE user_id = ?)", (user_id, REL_TOGETHER, ITEM, user_id)),
        ]
        if has_memories:
            statements += [
                ("DELETE FROM care_links WHERE user_id = ? AND src_type = ? AND src_id NOT IN"
                 " (SELECT memory_id FROM memories WHERE user_id = ?)", (user_id, MEMORY, user_id)),
                ("DELETE FROM care_links WHERE user_id = ? AND src_type = ? AND src_id IN"
                 " (SELECT memory_id FROM memories WHERE user_id = ?"
                 "   AND json_extract(data, '$.status') <> 'active')", (user_id, MEMORY, user_id)),
            ]
        try:
            for sql, args in statements:
                conn.execute(sql, args)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        after = conn.execute("SELECT COUNT(*) FROM care_links WHERE user_id = ?",
                             (user_id,)).fetchone()[0]
        return max(before - after, 0)

    def _watermark(self, user_id: str) -> Dict[str, float]:
        row = self._conn().execute(
            "SELECT item_at, memory_at FROM care_graph_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        return {"item_at": float(row["item_at"]) if row else 0.0,
                "memory_at": float(row["memory_at"]) if row else 0.0}

    def _set_watermark(self, user_id: str, item_at: Optional[float] = None,
                       memory_at: Optional[float] = None) -> None:
        cur = self._watermark(user_id)
        self._conn().execute(
            "INSERT OR REPLACE INTO care_graph_state (user_id, item_at, memory_at, synced_at)"
            " VALUES (?,?,?,?)",
            (user_id, item_at if item_at is not None else cur["item_at"],
             memory_at if memory_at is not None else cur["memory_at"], time.time()),
        )
        self._conn().commit()

    def sync_all(self, store, memory_manager, user_id: str, *, batch: int = 400) -> Dict[str, int]:
        """按水位增量补边：改过的节点才重抽，心跳里可以放心调。"""
        conn = self._conn()
        stamps = self._watermark(user_id)
        items = conn.execute(
            "SELECT * FROM care_items WHERE user_id = ? AND updated_at > ?"
            " ORDER BY updated_at ASC LIMIT ?",
            (user_id, stamps["item_at"], batch),
        ).fetchall()
        edges = 0
        max_item_at = stamps["item_at"]
        for row in items:
            item = dict(row)
            edges += self.sync_item(user_id, item)
            if item.get("source") == "auto":
                edges += self.link_item_by_time(user_id, item["id"], float(item["created_at"]))
            max_item_at = max(max_item_at, float(item["updated_at"]))
        if items:
            self._set_watermark(user_id, item_at=max_item_at)

        if memory_manager is not None:
            max_memory_at = stamps["memory_at"]
            touched = 0
            for memory_id, memory in memory_manager.get_user_memory_items(user_id):
                if float(getattr(memory, "updated_at", 0) or 0) <= stamps["memory_at"]:
                    continue
                edges += self.sync_memory(user_id, memory_id, memory.content)
                max_memory_at = max(max_memory_at, float(memory.updated_at))
                touched += 1
                if touched >= batch:
                    break
            if touched:
                self._set_watermark(user_id, memory_at=max_memory_at)
        self.reap(user_id)
        if memory_manager is not None:
            return {"items": len(items), "memories": touched, "edges": edges}
        return {"items": len(items), "memories": 0, "edges": edges}

    # ── 读 ──────────────────────────────────────────────
    def _hubs(self, user_id: str, node_type: str, node_id: str) -> List[sqlite3.Row]:
        return self._conn().execute(
            "SELECT rel, dst_type, dst_id, weight FROM care_links"
            " WHERE user_id = ? AND src_type = ? AND src_id = ? AND rel != ?",
            (user_id, node_type, node_id, REL_TOGETHER),
        ).fetchall()

    def _degree(self, user_id: str, hub_type: str, hub_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM care_links WHERE user_id = ? AND dst_type = ? AND dst_id = ?",
            (user_id, hub_type, hub_id),
        ).fetchone()
        return int(row[0] or 0)

    def related(self, user_id: str, node_type: str, node_id: str, *,
                limit: int = 4, min_score: float = 0.15) -> List[Dict[str, Any]]:
        """沿枢纽走两步找邻居 + 同场直连，按"是不是只有它能连上"打分。"""
        conn = self._conn()
        me = f"{node_type}:{node_id}"
        scores: Dict[str, Dict[str, Any]] = {}

        def add(key: str, value: Dict[str, Any], score: float) -> None:
            """同一个邻居可能被好几个枢纽连上：最强的一条算数，其余按零头加成。"""
            got = scores.get(key)
            if got is None:
                scores[key] = dict(value, best=score, total=score)
            else:
                got["best"] = max(got["best"], score)
                got["total"] += score

        for hub in self._hubs(user_id, node_type, node_id):
            degree = self._degree(user_id, hub["dst_type"], hub["dst_id"])
            if not degree or degree > HUB_DEGREE_CAP:
                continue  # 到处都有的词，连上了也不说明什么
            # 越稀有的共同枢纽越有意义：1/log(degree+1) 让"就这一个词重合"不值钱
            quality = hub["weight"] / (1.0 + math.log(degree))
            rows = conn.execute(
                "SELECT src_type, src_id, label, kind, rel, weight FROM care_links"
                " WHERE user_id = ? AND dst_type = ? AND dst_id = ? LIMIT ?",
                (user_id, hub["dst_type"], hub["dst_id"], HUB_NEIGHBOR_CAP + 1),
            ).fetchall()
            for row in rows:
                other = f"{row['src_type']}:{row['src_id']}"
                if other == me or row["rel"] == REL_TOGETHER:
                    continue
                add(other, {
                    "type": row["src_type"], "id": row["src_id"],
                    "label": row["label"], "kind": row["kind"],
                    "via": f"{hub['dst_type']}:{hub['dst_id']}",
                }, quality * row["weight"])

        for row in conn.execute(
            "SELECT dst_type, dst_id, weight FROM care_links"
            " WHERE user_id = ? AND src_type = ? AND src_id = ? AND rel = ?",
            (user_id, node_type, node_id, REL_TOGETHER),
        ).fetchall():
            if row["dst_type"] == ITEM:
                item = conn.execute("SELECT title, kind FROM care_items WHERE id = ?",
                                    (row["dst_id"],)).fetchone()
                if item is None:
                    continue
                add(f"item:{row['dst_id']}", {
                    "type": ITEM, "id": row["dst_id"], "label": item["title"],
                    "kind": item["kind"], "via": REL_TOGETHER,
                }, row["weight"])
        ranked = []
        for got in scores.values():
            best = got.pop("best")
            total = got.pop("total")
            got["score"] = round(best + 0.3 * max(total - best, 0.0), 4)
            if got["score"] >= min_score:
                ranked.append(got)
        ranked.sort(key=lambda x: (-x["score"], x["type"], x["id"]))
        return ranked[:limit]

    def context_lines(self, user_id: str, node_type: str, node_id: str, *,
                      limit: int = 3) -> List[str]:
        """给措辞用的一行行事实，形如「关于妈妈：用户的妈妈喜欢养花」。"""
        lines = []
        for rel in self.related(user_id, node_type, node_id, limit=limit):
            kind, _, hub = rel["via"].partition(":")
            if rel["via"] == REL_TOGETHER:
                prefix = "同一句话里还记着"
            elif kind == PERSON:
                prefix = f"关于{hub}"
            else:
                prefix = f"和「{hub}」相关"
            lines.append(f"{prefix}：{rel['label']}")
        return lines

    def describe(self, user_id: str, *, limit: int = 400) -> Dict[str, Any]:
        rows = self._conn().execute(
            "SELECT src_type, src_id, rel, dst_type, dst_id, weight, label, kind FROM care_links"
            " WHERE user_id = ? ORDER BY dst_type, dst_id LIMIT ?",
            (user_id, limit),
        ).fetchall()
        edges = [dict(r) for r in rows]
        hubs: Dict[str, int] = {}
        for edge in edges:
            if edge["rel"] == REL_TOGETHER:
                continue
            key = f"{edge['dst_type']}:{edge['dst_id']}"
            hubs[key] = hubs.get(key, 0) + 1
        return {"edges": edges, "hubs": hubs, "state": self._watermark(user_id)}

    def stats(self, user_id: str) -> Dict[str, Any]:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n, SUM(rel != ?) AS hub_edges FROM care_links WHERE user_id = ?",
            (REL_TOGETHER, user_id),
        ).fetchone()
        return {"edges": int(row["n"] or 0), "hub_edges": int(row["hub_edges"] or 0),
                "degree_cap": HUB_DEGREE_CAP, **self._watermark(user_id)}
