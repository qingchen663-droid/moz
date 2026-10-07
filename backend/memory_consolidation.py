"""
Memory consolidation service.

Periodically merges related low-grade memories into coherent higher-grade
entries: the system reflects on accumulated fragments and produces a distilled
summary memory, marking the originals as consolidated.
"""

import os
import time
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Minimum number of related memories before triggering consolidation
MIN_CLUSTER_SIZE = 3
# Embedding similarity threshold for grouping related memories
CLUSTER_SIMILARITY = 0.55


class MemoryConsolidator:
    def __init__(self, memory_manager):
        self.memory_manager = memory_manager

    def find_clusters(self, user_id: str) -> List[List]:
        """Group active, unconsolidated memories by semantic similarity.

        逐对调 `EmbeddingService.cosine_similarity` 在 1200 条上要跑 719,400 次
        「新建两个 np.array + 两次求模 + 一次点积」（实测单次 59µs，整轮 42.5 秒），
        而同一件事用一次归一化矩阵乘就够。这里保留原有的取样顺序与判据
        （seed 之后、未分组、sim >= CLUSTER_SIMILARITY），只换算法。
        """
        import numpy as np

        user_memories = self.memory_manager._get_user_memories(user_id)
        candidates = [
            m for m in user_memories.values()
            if m.active()
            and not m.is_consolidated
            and m.embedding is not None
            and not m.locked
        ]
        if len(candidates) < MIN_CLUSTER_SIZE:
            return []

        matrix = np.array([m.embedding for m in candidates], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        unit = matrix / norms[:, None]

        assigned = set()
        clusters = []
        for i, seed in enumerate(candidates):
            if seed.id in assigned:
                continue
            # 一个 seed 一次行点积：O(n) 次 BLAS，而不是 O(n^2) 次 numpy 小调用
            sims = unit @ unit[i]
            cluster = [seed]
            for j in range(i + 1, len(candidates)):
                other = candidates[j]
                if other.id in assigned or other.embedding is None:
                    continue
                if float(sims[j]) >= CLUSTER_SIMILARITY:
                    cluster.append(other)
            if len(cluster) >= MIN_CLUSTER_SIZE:
                clusters.append(cluster)
                assigned.update(m.id for m in cluster)

        return clusters

    def consolidate_user(self, user_id: str) -> int:
        """Find and merge related memory clusters. Returns number of consolidations."""
        from llm_config import get_llm_client
        from langchain_core.messages import HumanMessage

        clusters = self.find_clusters(user_id)
        if not clusters:
            return 0

        llm = get_llm_client(temperature=0.3, use_thinking=False)
        count = 0

        for cluster in clusters[:5]:  # Limit per run to avoid latency spikes
            contents = "\n".join(f"- {m.content}" for m in cluster)
            prompt = f"""以下是一组相关的记忆片段，请将它们合并为一条简洁、完整的记忆总结（100字以内）。
只输出合并后的内容，不要解释。

{contents}"""

            try:
                response = llm.invoke([HumanMessage(content=prompt)])
                merged_text = response.content.strip()
                if not merged_text or len(merged_text) < 10:
                    continue

                # Store the consolidated memory with boosted importance
                avg_importance = sum(m.importance for m in cluster) / len(cluster)
                max_emotion_intensity = max(m.emotion_intensity for m in cluster)

                self.memory_manager.add_memory(
                    user_id=user_id,
                    content=f"[整合记忆] {merged_text}",
                    category=cluster[0].category,
                    emotion=cluster[0].emotion,
                    emotion_intensity=max_emotion_intensity,
                    importance=min(1.0, avg_importance + 0.15),
                    source_type="consolidation",
                )

                # Mark originals as consolidated
                for m in cluster:
                    self.memory_manager.consolidate_memory(user_id, m.id)

                logger.info(
                    "[记忆巩固] user=%s 合并 %d 条 -> '%s...'",
                    user_id, len(cluster), merged_text[:50]
                )
                count += 1
            except Exception as e:
                logger.warning("[记忆巩固] 合并失败: %s", e)
                break

        return count
