"""LLM service: OpenAI-compatible API wrapper with chat + stream support."""
import os
from dotenv import load_dotenv

load_dotenv()


class ChatService:
    def __init__(self):
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def client(self):
        if self._client is None and self.available:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30,
                max_retries=1,
            )
        return self._client

    def _call(self, messages: list[dict], max_tokens: int = 1024) -> str | None:
        if not self.available:
            return None
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"[LLM Error] {type(e).__name__}: {e}")
            return None

    def call_raw(self, messages: list[dict], tools: list[dict] | None = None,
                 max_tokens: int = 1024):
        """Call LLM with raw OpenAI-format messages. Returns full API response.
        Used by LangGraph adapter to access tool_calls."""
        if not self.available:
            raise RuntimeError("LLM not available (check LLM_API_KEY)")
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=0.3,
            max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
        return self.client.chat.completions.create(**kwargs)

    def chat(self, prompt: str, system_prompt: str | None = None) -> str | None:
        """Non-streaming chat. Returns LLM response text."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self._call(messages)

    def stream(self, prompt: str, system_prompt: str | None = None):
        """Streaming chat — placeholder for future use."""
        raise NotImplementedError("stream() not yet implemented")

    def rewrite_query(self, question: str) -> str | None:
        """Rewrite colloquial query into precise search terms (legacy)."""
        return self._call([
            {"role": "system", "content": (
                "将用户口语化问题改写为适合知识库检索的规范查询。"
                "去除无关描述（手机品牌、App名称等），提取核心问题。只输出改写后的查询。"
            )},
            {"role": "user", "content": f"改写：{question}"},
        ], max_tokens=200)

    def synthesize(self, question: str, chunks: list[str]) -> str | None:
        """Synthesize multiple retrieved chunks into one complete answer (legacy)."""
        if not chunks:
            return None
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            context_parts.append(f"[参考片段 {i}]\n{chunk}")
        context = "\n\n".join(context_parts)
        return self._call([
            {"role": "system", "content": (
                "你是一个智能家居品牌的 AI 客服。根据提供的知识库内容，回答用户问题。\n"
                "要求：\n"
                "1. 综合所有相关片段，给出一个完整、连贯的答案\n"
                "2. 如果问题涉及多个方面（如故障排查+保修政策），逐一回应，不要遗漏\n"
                "3. 只基于提供的知识库内容回答，不要编造信息\n"
                "4. 如果知识库内容不足以回答某部分，诚实说明"
            )},
            {"role": "user", "content": (
                f"知识库内容：\n{context}\n\n用户问题：{question}\n请回答："
            )},
        ], max_tokens=800)


chat_service = ChatService()
