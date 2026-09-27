"""把 UI 验收要用的数据种进一份隔离沙箱库，绝不碰 backend/moz.db。

用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/seed_ui_sandbox.py <沙箱 backend 目录>
"""
import os
import sys

work = os.path.abspath(sys.argv[1])
db = os.path.join(work, "moz.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from care_graph import CareGraph  # noqa: E402
from care_store import CareStore  # noqa: E402
import care_extractor  # noqa: E402
from memory_manager import MemoryCategory, MemoryManager  # noqa: E402

USER = "web_user_001"
store = CareStore(db)
graph = CareGraph(db)
touched = care_extractor.harvest(
    store, USER,
    "我妈生日是10月5号，她喜欢养花；我下周三要去做项目答辩，答辩之后一周出结果",
    "", None, graph=graph,
)
print("事项/链:", touched)
mm = MemoryManager(storage_path=os.path.join(work, "memory_store"), db_path=db)
mm.embedding_service.get_embedding = lambda t: None
for text in ["[关于用户] 用户的妈妈喜欢养花", "[关于用户] 用户的项目在总部三楼评审"]:
    mm.add_memory(USER, text, category=MemoryCategory.FACT)
graph.sync_all(store, mm, USER)
print("items:", [(i["id"], i["title"]) for i in store.list_items(USER, status=None)])
print("graph stats:", graph.stats(USER))
for item in store.list_items(USER, status=None):
    print("  related", item["title"], "->", [(r["label"], r["via"]) for r in graph.related(USER, "item", item["id"])])
