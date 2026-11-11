"""
轻量向量存储（TF-IDF + 余弦相似度，纯本地、零外部服务）。

为什么不用 chromadb / 外部 embedding：
- DeepSeek 未提供 embedding 接口；
- 本地 embedding 模型体积大、首次下载不可控；
- 记忆量级（几百到几千条主题摘要）用 TF-IDF 已足够，且完全可解释、可离线。

切词策略：
- 中文：字符 n-gram（默认 2-gram），无需分词库
- 英文/数字：按单词切分并小写
- 混合文本同时保留两种特征
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from cognitive.storage.synonyms import expand_terms


# 英文/数字词元
_WORD_PATTERN = re.compile(r"[a-zA-Z0-9]+")

# 中文连续片段（随后按字切 n-gram）
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str, ngram: Optional[int] = None) -> List[str]:
    """
    把文本切为检索词元。

    :param text: 原始文本
    :param ngram: 中文字符 n-gram 的 n，缺省读 config
    :return: 词元列表
    """
    n = ngram or config.VECTOR_NGRAM
    tokens: List[str] = []

    for word in _WORD_PATTERN.findall(text or ""):
        tokens.append(word.lower())

    for run in _CJK_RUN_PATTERN.findall(text or ""):
        if len(run) <= n:
            tokens.append(run)
        else:
            for i in range(len(run) - n + 1):
                tokens.append(run[i : i + n])

    return tokens


def _term_counts(tokens: List[str]) -> Dict[str, int]:
    """统计词元频次。"""
    counts: Dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


class VectorStore:
    """基于 TF-IDF 的向量检索（无外部依赖）。"""

    def __init__(self, space_id: str) -> None:
        self.space_id = space_id
        self.path = config.VECTOR_DIR / f"{space_id}.json"
        # 全新安装时 data/vectors/ 可能还不存在：这里主动创建，
        # 否则首次 --seed-persona 会在写索引时失败（父目录不存在）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._docs: List[Dict[str, Any]] = []
        # 注意：缓存字典不能叫 self._idf —— 会遮蔽同名方法 self._idf()，
        # 使检索时 `idf = self._idf()` 变成"调用一个 dict"而整体崩溃
        self._idf_cache: Dict[str, float] = {}
        self._idf_cache_ts = 0  # IDF缓存时间戳
        self._idf_cache_ttl = 300  # IDF缓存有效期（秒）
        self._load()

    # ------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------
    def _load(self) -> None:
        """从磁盘加载索引，文件不存在则初始化为空。"""
        if not self.path.exists():
            self._docs = []
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._docs = payload.get("docs", [])
        except (json.JSONDecodeError, OSError):
            # 索引损坏时重建为空索引，不阻塞主流程
            self._docs = []

    def _save(self) -> None:
        """写回磁盘（原子替换，避免写入中断导致索引损坏）。"""
        payload = {"space_id": self.space_id, "docs": self._docs}
        temp_path = self.path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
        temp_path.replace(self.path)

    # ------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------
    def add(
        self, doc_id: str, text: str, meta: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        新增或覆盖一条文档索引。

        :param doc_id: 文档唯一 id（通常是 topic_id）
        :param text: 用于检索的文本（通常是主题标题 + 摘要 + 关键词）
        :param meta: 附带的元数据
        """
        counts = _term_counts(
            tokenize(f"{text} {' '.join((meta or {}).get('keywords', []))}")
        )

        # 已存在同 id 时先移除，保证覆盖语义
        self._docs = [d for d in self._docs if d.get("id") != doc_id]
        self._docs.append(
            {
                "id": doc_id,
                "text": text,
                "meta": meta or {},
                "counts": counts,
            }
        )
        self._save()
        # 文档集变了，IDF 缓存立即失效，避免新文档在 TTL 窗口内检索不到
        self._idf_cache_ts = 0

    def has(self, doc_id: str) -> bool:
        """判断文档是否已索引。"""
        return any(d.get("id") == doc_id for d in self._docs)

    # ------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------
    def _idf(self) -> Dict[str, float]:
        """
        计算当前全库的 IDF 权重（带缓存机制）。

        :return: 词元 -> idf 的映射
        """
        import time

        # 检查缓存是否有效
        now = time.time()
        if self._idf_cache and (now - self._idf_cache_ts) < self._idf_cache_ttl:
            return self._idf_cache

        # 重新计算IDF
        doc_count = len(self._docs)
        df: Dict[str, int] = {}
        for doc in self._docs:
            for term in doc.get("counts", {}):
                df[term] = df.get(term, 0) + 1

        # 采用平滑 idf，避免除零
        idf_result = {
            term: math.log((1 + doc_count) / (1 + freq)) + 1.0
            for term, freq in df.items()
        }

        # 更新缓存
        self._idf_cache = idf_result
        self._idf_cache_ts = now

        return idf_result

    @staticmethod
    def _vectorize(counts: Dict[str, int], idf: Dict[str, float]) -> Dict[str, float]:
        """
        TF-IDF 向量化并做 L2 归一化。

        :param counts: 词元频次
        :param idf: 全库 idf
        :return: 归一化后的稀疏向量
        """
        vector: Dict[str, float] = {}
        for term, freq in counts.items():
            weight = (1 + math.log(freq)) * idf.get(term, 1.0)
            vector[term] = weight

        norm = math.sqrt(sum(v * v for v in vector.values()))
        if norm == 0:
            norm = 1.0
        return {term: v / norm for term, v in vector.items()}

    @staticmethod
    def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
        """计算两个稀疏向量的余弦相似度。"""
        if len(a) > len(b):
            a, b = b, a
        return sum(value * b.get(term, 0.0) for term, value in a.items())

    def _query_vector(
        self, query: str, idf: Dict[str, float], origin: str = ""
    ) -> Dict[str, float]:
        """
        构造查询向量：字面词元 + 同义扩展词元（打折）。

        :param query: 检索查询
        :param idf: 全库 idf
        :param origin: 原始用户消息（可选，扩大同义命中面）
        :return: L2 归一化后的查询向量
        """
        base = self._vectorize(_term_counts(tokenize(query)), idf)

        if not config.VECTOR_EXPAND_ENABLED:
            return base

        extra_terms = expand_terms(query, origin)
        if not extra_terms:
            return base

        # 扩展词本身也要切成与索引一致的词元（中文 2-gram），否则匹配不上
        extra = self._vectorize(_term_counts(tokenize(" ".join(extra_terms))), idf)

        merged: Dict[str, float] = dict(base)
        for term, value in extra.items():
            merged[term] = merged.get(term, 0.0) + value * config.VECTOR_EXPAND_WEIGHT

        norm = math.sqrt(sum(v * v for v in merged.values())) or 1.0
        return {term: v / norm for term, v in merged.items()}

    @staticmethod
    def _keyword_boost(
        query: str, meta: Dict[str, Any], hints: Optional[List[str]] = None
    ) -> float:
        """
        关键词直接命中加成。

        主题的关键词是抽取模型给出的短词，比 2-gram 更"整"。
        查询里原样出现某个关键词（或它的同义扩展词）时，说明这条命中很实 ——
        给它额外加分，并允许它绕过 min_score（见 search 里的豁免逻辑）。

        :param query: 检索查询
        :param meta: 文档元数据（含 keywords）
        :param hints: 同义扩展词（「妈」扩展出「妈妈」后再去比关键词）
        :return: 加分量（未命中为 0）
        """
        for keyword in (meta or {}).get("keywords", []) or []:
            text = str(keyword)
            if text and (text in query or (hints and text in hints)):
                return config.VECTOR_KEYWORD_BOOST
        return 0.0

    def search(
        self, query: str, top_k: Optional[int] = None, min_score: Optional[float] = None
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        检索最相关的文档。

        打分 = 余弦相似度 + 关键词命中加成。
        关键词命中时**豁免** min_score：像「你养的那只猫叫什么」这种改写，
        字面余弦常常为 0，但它明确命中了「猫」这个关键词，值得召回。

        :param query: 查询文本
        :param top_k: 返回条数上限
        :param min_score: 最低相似度（关键词命中时豁免）
        :return: [(doc_id, score, meta)] 列表，按分数降序
        """
        top_k = top_k or config.TOPIC_TOP_K
        min_score = config.TOPIC_MIN_SCORE if min_score is None else min_score

        if not query or not self._docs:
            return []

        hints = expand_terms(query) if config.VECTOR_EXPAND_ENABLED else []
        idf = self._idf()
        query_vec = self._query_vector(query, idf)

        scored: List[Tuple[str, float, Dict[str, Any]]] = []
        for doc in self._docs:
            doc_vec = self._vectorize(doc.get("counts", {}), idf)
            cosine = self._cosine(query_vec, doc_vec)
            boost = self._keyword_boost(query, doc.get("meta", {}), hints)

            if cosine < min_score and boost <= 0:
                continue

            scored.append((doc["id"], min(1.0, cosine + boost), doc.get("meta", {})))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def count(self) -> int:
        """返回索引中的文档数量。"""
        return len(self._docs)
