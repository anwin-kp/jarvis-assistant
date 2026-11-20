"""Understanding by meaning: picks tool groups by comparing what you said with every tool's description
(local embedding model on the CPU, ~20 ms). Complements the keyword matching in tool_groups."""

import re

import numpy as np
from loguru import logger

_COMPLEX = re.compile(
    r"\b(why|compare|comparison|plan (?:my|a|the)|analy[sz]e|step[- ]by[- ]step|pros and cons|difference between|"
    r"what if|solve|calculate|debug|strategy|brainstorm|think (?:hard|carefully|it through)|reason through)\b", re.I)


def needs_thinking(text: str) -> bool:
    """Complex requests get the model's slower 'thinking' mode; quick commands stay fast."""
    return len(text.split()) > 25 or bool(_COMPLEX.search(text))


class SemanticGroups:
    def __init__(self, reg, embed, top_tools: int = 4, min_score: float = 0.55):
        self.reg, self.embed = reg, embed
        self.top_tools, self.min_score = top_tools, min_score
        self._names: list[str] = []
        self._matrix: np.ndarray | None = None

    def _build(self) -> None:
        names = self.reg.names()
        texts = [f"search_document: {n.replace('_', ' ')}: {self.reg.get(n).description}" for n in names]
        m = self.embed(texts)
        self._matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        self._names = names

    def pick(self, text: str) -> set[str]:
        try:
            if self._matrix is None or len(self._names) != len(self.reg.names()):
                self._build()
            q = self.embed([f"search_query: {text}"])[0]
            scores = self._matrix @ (q / (np.linalg.norm(q) + 1e-9))
            best = np.argsort(-scores)[: self.top_tools]
            return {self.reg.get(self._names[i]).group for i in best if scores[i] >= self.min_score}
        except Exception as e:
            logger.debug("Semantic tool selection unavailable: {}", e)
            return set()
