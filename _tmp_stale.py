import io

p = "backend/emotion_state.py"
s = io.open(p, encoding="utf-8").read()


def rep(o, n):
    global s
    assert s.count(o) == 1, repr(o[:60])
    s = s.replace(o, n)


rep('"quota_skipped": 0, "built": 0, "failed": 0}',
    '"quota_skipped": 0, "built": 0, "failed": 0, "stale_skipped": 0}')

rep('''    def prompt_line(self, user_id: str) -> str:
        row = self.get(user_id, "baseline")
        return format_baseline(row["data"]) if row else ""''',
    '''    def prompt_line(self, user_id: str, watermark: Optional[float] = None) -> str:
        """基线那行话。给了当前水位就对一下：事实变了就先别拿旧总结说话。

        红线是"用户删掉的记忆不该还在影响措辞"——TTL 只管 30 分钟，基线能挂 7 天。
        """
        row = self.get(user_id, "baseline")
        if not row:
            return ""
        if watermark is not None and abs(row["source_watermark"] - watermark) > 1e-6:
            bump_counter("stale_skipped")
            return ""
        return format_baseline(row["data"])""")

rep('''def topic_plan(store: EmotionStore, user_id: str, topic: str,
               now: Optional[float] = None) -> Optional[Dict[str, Any]]:''',
    '''def topic_plan(store: EmotionStore, user_id: str, topic: str,
               now: Optional[float] = None,
               watermark: Optional[float] = None) -> Optional[Dict[str, Any]]:''')

rep('''    if row["expires_at"] <= now:
        bump_counter("expired_skipped")          # 拦住了才计数，用来证明闸门真的在闸
        return None
    return row["data"]''',
    '''    if row["expires_at"] <= now:
        bump_counter("expired_skipped")          # 拦住了才计数，用来证明闸门真的在闸
        return None
    if watermark is not None and abs(row["source_watermark"] - watermark) > 1e-6:
        bump_counter("stale_skipped")            # 它依据的事实已经变了，这条对策作废
        return None
    return row["data"]''')

rep('''def plan_for(store: EmotionStore, user_id: str, topics: List[str],
             now: Optional[float] = None) -> str:''',
    '''def plan_for(store: EmotionStore, user_id: str, topics: List[str],
             now: Optional[float] = None,
             watermark: Optional[float] = None) -> str:''')

rep('        data = topic_plan(store, user_id, topic, now=now)',
    '        data = topic_plan(store, user_id, topic, now=now, watermark=watermark)')

io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("emotion_state 水位校验加上了")

p2 = "backend/emotion_graph.py"
t = io.open(p2, encoding="utf-8").read()
o2 = '''            if emotion_store is not None:
                # L1：一次本地读，不调模型；没有基线就是空串（新用户就该什么都不加）
                state["baseline_context"] = emotion_store.prompt_line(user_id)
                # L2：上一轮备好的对策（过期或主题不对就不认），以及这一轮该新预热的主题
                line = emotion_state.plan_for(emotion_store, user_id, live["topics"])'''
n2 = '''            if emotion_store is not None:
                # 本地扫一遍拿当前水位（不是上游往返）：用户删过记忆，旧总结和旧对策就该闭嘴
                stamp, _evidence = emotion_state.collect_signals(memory_manager, care_store,
                                                                None, user_id)
                # L1：一次本地读，不调模型；没有基线就是空串（新用户就该什么都不加）
                state["baseline_context"] = emotion_store.prompt_line(user_id, stamp)
                # L2：上一轮备好的对策（过期、主题不对、依据变了都不认），以及该新预热的主题
                line = emotion_state.plan_for(emotion_store, user_id, live["topics"],
                                              watermark=stamp)'''
assert t.count(o2) == 1, "prologue anchor"
io.open(p2, "w", encoding="utf-8", newline="\n").write(t.replace(o2, n2))
print("emotion_graph 接上水位")

p3 = "tools/selftest.py"
u = io.open(p3, encoding="utf-8").read()
anchor = '''        # ③ 依据不足 / 空对策不许留壳'''
add = '''        # ②' 依据的事实变了就要闭嘴：删记忆、改档案都算（红线：删掉的记忆不该还在影响措辞）
        stale_before = ES.plan_stats()["stale_skipped"]
        if ES.plan_for(store, user, ["家人"], now=time.time(), watermark=999999.0):
            bad.append("事实水位变了还在用旧对策")
        if ES.plan_stats()["stale_skipped"] <= stale_before:
            bad.append("水位拦下对策却没计数")
        thin = ES.sanitize_baseline({"tone_default": "轻一点", "baseline_emotion": "sad",
                                     "trigger_topics": [], "landmines": [],
                                     "comfort_style": "", "insufficient": False})
        if not thin or thin["baseline_emotion"] != "难过":
            bad.append(f"只有一句实在话的合法基线被误杀：{thin}")

'''
assert u.count(anchor) == 1
u = u.replace(anchor, add + anchor)
io.open(p3, "w", encoding="utf-8", newline="\n").write(u)
print("门禁加了水位那条")
