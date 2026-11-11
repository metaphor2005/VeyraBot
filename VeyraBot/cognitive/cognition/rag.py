"""
RAG 兜底插件（本地语料检索）。

定位：这是"最后才启用"的补充手段，不是主链路。
- 只有在判决器认为记忆不足时才会被调用；
- 检索对象是本地 corpus 目录下的资料（.md / .txt），不联网；
- 若将来接入外部搜索，只需实现同样的 search 接口并注册到 RAGRegistry。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from cognitive.storage.vector_store import _term_counts, tokenize


# 单块最大字符数
CHUNK_SIZE: int = 400

# 单块重叠字符数，避免切断关键句
CHUNK_OVERLAP: int = 60

# Markdown 标题行（用于分节，保证标题与正文不被切散）
_HEADING_SPLIT_PATTERN = re.compile(r"\n(?=#{1,6}\s)")


def _truncate_at_boundary(text: str, limit: int) -> str:
    """
    按语义边界截断文本，避免切在句子中间。

    优先在段落/句末/分句处收尾；找不到合适边界才硬切。

    :param text: 原始文本
    :param limit: 最大字符数
    :return: 截断后的文本（超长时以省略号收尾）
    """
    if len(text) <= limit:
        return text

    cut = text[:limit]

    # 从最粗的边界往细的找，且要求切点不能太靠前（至少保留 60%）
    for separator in ("\n", "。", "；", "！", "？", "，", " "):
        index = cut.rfind(separator)
        if index >= int(limit * 0.6):
            return cut[: index + 1].rstrip() + "…"

    return cut.rstrip() + "…"


def _split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """
    把长文本切为带重叠的块。

    策略：
    1. 先按 Markdown 标题分节，保证「标题 + 正文」在同一个块里，
       否则检索只会命中孤立的标题行，返回内容没有信息量；
    2. 单节超长时再按字符滑窗切分，并给每块补回标题前缀。

    :param text: 原始文本
    :param chunk_size: 单块最大字符数
    :param overlap: 相邻块重叠字符数
    :return: 块列表
    """
    chunks: List[str] = []

    for section in _HEADING_SPLIT_PATTERN.split(text):
        section = section.strip()
        if not section:
            continue

        if len(section) <= chunk_size:
            chunks.append(section)
            continue

        lines = section.splitlines()
        heading = lines[0].strip() if lines and lines[0].lstrip().startswith("#") else ""
        body = "\n".join(lines[1:]).strip() if heading else section

        step = max(chunk_size - overlap, 1)
        for start in range(0, len(body), step):
            piece = body[start:start + chunk_size].strip()
            if not piece:
                continue
            chunks.append(f"{heading}\n{piece}".strip() if heading else piece)

    return chunks


class LocalCorpusRAG:
    """本地语料 RAG。"""

    def __init__(self, corpus_dir: Optional[Path] = None) -> None:
        """
        :param corpus_dir: 语料目录，缺省读 config
        """
        self.dir = Path(corpus_dir or config.RAG_CORPUS_DIR)
        self._chunks: Optional[List[Dict[str, object]]] = None

    # ------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------
    def _load(self) -> List[Dict[str, object]]:
        """
        加载语料并切块（惰性，首次检索时执行）。

        :return: 块列表，每项含 source / text / counts
        """
        if self._chunks is not None:
            return self._chunks

        chunks: List[Dict[str, object]] = []

        if not self.dir.exists():
            self._chunks = []
            return self._chunks

        for path in sorted(self.dir.glob("*")):
            if path.suffix.lower() not in (".md", ".txt"):
                continue

            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for chunk in _split_into_chunks(text):
                chunks.append(
                    {
                        "source": path.name,
                        "text": chunk,
                        "counts": _term_counts(tokenize(chunk)),
                    }
                )

        self._chunks = chunks
        return chunks

    # ------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------
    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ) -> List[Tuple[str, float, str]]:
        """
        检索最相关的语料块。

        :param query: 查询词
        :param top_k: 返回条数上限
        :param min_score: 最低相似度
        :return: [(文本, 分数, 来源文件)]，按分数降序
        """
        top_k = top_k or config.RAG_TOP_K
        min_score = config.RAG_MIN_SCORE if min_score is None else min_score

        chunks = self._load()
        if not chunks or not query:
            return []

        # 计算 idf
        doc_count = len(chunks)
        df: Dict[str, int] = {}
        for chunk in chunks:
            for term in chunk["counts"]:
                df[term] = df.get(term, 0) + 1
        idf = {term: math.log((1 + doc_count) / (1 + freq)) + 1.0 for term, freq in df.items()}

        # 查询向量
        query_counts = _term_counts(tokenize(query))
        query_vec = self._to_vector(query_counts, idf)

        scored: List[Tuple[str, float, str]] = []
        for chunk in chunks:
            doc_vec = self._to_vector(chunk["counts"], idf)
            score = sum(value * doc_vec.get(term, 0.0) for term, value in query_vec.items())
            if score >= min_score:
                text = _truncate_at_boundary(str(chunk["text"]), config.RAG_MAX_CHARS)
                scored.append((text, score, str(chunk["source"])))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _to_vector(counts: Dict[str, int], idf: Dict[str, float]) -> Dict[str, float]:
        """
        构造归一化的 TF-IDF 向量。

        :param counts: 词频
        :param idf: 全库 idf
        :return: 稀疏向量
        """
        vector = {term: (1 + math.log(freq)) * idf.get(term, 1.0) for term, freq in counts.items()}
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        return {term: value / norm for term, value in vector.items()}


class RAGRegistry:
    """
    RAG 提供者注册表，支持多来源（本地语料、将来的外部搜索）。

    主链路只依赖 search() 这一层抽象。
    """

    def __init__(self) -> None:
        self._providers: Dict[str, object] = {}
        self.register("local", LocalCorpusRAG())

    def register(self, name: str, provider: object) -> None:
        """
        注册一个检索提供者（需实现 search(query, top_k) 方法）。

        :param name: 提供者名称
        :param provider: 提供者实例
        """
        self._providers[name] = provider

    def search(self, query: str, top_k: Optional[int] = None) -> List[str]:
        """
        聚合所有提供者的检索结果。

        :param query: 查询词
        :param top_k: 每个提供者返回条数
        :return: 去重后的文本片段列表
        """
        results: List[str] = []
        for name, provider in self._providers.items():
            try:
                hits = provider.search(query, top_k=top_k)  # type: ignore[attr-defined]
            except Exception:
                # 单个提供者失败不影响整体
                continue
            for hit in hits:
                text = hit[0] if isinstance(hit, tuple) else str(hit)
                if text and text not in results:
                    results.append(text)
        return results
