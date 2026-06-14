"""CustomerServiceAgent — ReAct Agent with search_faq + Finish actions."""
import json
import re
from dataclasses import dataclass, field

from llm import ChatService
from utils import ConversationManager
from tools.base import Tool
from domain.customer_service.prompts import SYSTEM_PROMPT


@dataclass
class AgentResponse:
    type: str  # "final_answer" or "ask_user"
    content: str
    conversation_id: str
    metadata: dict | None = None


# Regex to extract Thought and Action from LLM output
_PARSE_PATTERN = re.compile(
    r"Thought:\s*(.*?)\n\s*Action:\s*(.*)", re.DOTALL
)

# Regex to parse Action: search_faq[...] or Finish[...]
_ACTION_PATTERN = re.compile(r"(\w+)\s*\[(.*)\]", re.DOTALL)


class CustomerServiceAgent:
    def __init__(
        self,
        llm: ChatService,
        conversation_manager: ConversationManager,
        tools: list[Tool],
        system_prompt: str | None = None,
        max_steps: int = 5,
    ):
        self.llm = llm
        self.conversation_manager = conversation_manager
        self.tools = tools
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.max_steps = max_steps

    def _find_tool(self, name: str) -> Tool | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def _build_tools_description(self) -> str:
        return "\n".join(t.to_prompt_desc() for t in self.tools)

    def _format_context(self, context: list[dict]) -> str:
        if not context:
            return "（新对话，无历史记录）"
        lines = []
        for msg in context:
            role_label = "用户" if msg["role"] == "user" else "客服"
            lines.append(f"{role_label}: {msg['content']}")
        return "\n".join(lines)

    def _build_prompt(
        self,
        user_input: str,
        context: list[dict],
        step_history: list[dict],
    ) -> str:
        tools_desc = self._build_tools_description()
        context_str = self._format_context(context)

        history_lines = []
        for step in step_history:
            history_lines.append(f"Thought: {step['thought']}")
            history_lines.append(f"Action: {step['action']}")
            if "observation" in step:
                history_lines.append(f"Observation: {step['observation']}")
            history_lines.append("")
        history_str = "\n".join(history_lines) if history_lines else "（开始分析）"

        return self.system_prompt.format(
            tools=tools_desc,
            context=context_str,
            user_input=user_input,
            history=history_str,
        )

    def _parse_output(self, text: str) -> tuple[str, str] | None:
        """Parse LLM output into (thought, action) tuple."""
        m = _PARSE_PATTERN.search(text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return None

    def _parse_action(self, action_text: str) -> tuple[str, str] | None:
        """Parse Action text into (action_type, arg). E.g. 'search_faq[退货]'."""
        m = _ACTION_PATTERN.match(action_text.strip())
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return None

    def _dispatch_tool(self, tool_name: str, arg_text: str) -> str:
        """Map raw text to tool params (3-level priority) and execute. Returns Observation."""
        tool = self._find_tool(tool_name)
        if tool is None:
            return f"错误: 未找到工具 '{tool_name}'"

        params = self._map_params(tool, arg_text)
        try:
            return tool.run(params)
        except Exception as e:
            return f"工具调用失败: {e}"

    def _map_params(self, tool: Tool, text: str) -> dict:
        """Map raw input text to params dict (JSON → single-param → error)."""
        # 1. Try JSON parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

        # 2. Single required param → use text as its value
        required_params = [p for p in tool.parameters if p.required]
        if len(required_params) == 1:
            return {required_params[0].name: text}

        # 3. Fallback: error
        raise ValueError(
            f"Tool '{tool.name}' requires structured parameters. "
            f"Required: {[p.name for p in required_params]}. "
            f"Got text: '{text}'"
        )

    def run(self, user_input: str, conversation_id: str = "") -> AgentResponse:
        # ── 1. Ensure conversation exists ──
        if not conversation_id:
            conversation_id = self.conversation_manager.create(user_id="default")

        # ── 2. Persist user message ──
        self.conversation_manager.add_message(
            conversation_id, "user", user_input
        )

        # ── 3. Get conversation context ──
        context = self.conversation_manager.get_context(conversation_id)
        # Exclude the just-added user message from context (it's in {user_input})
        if context and context[-1]["role"] == "user":
            context = context[:-1]

        # ── 4. ReAct loop ──
        step_history: list[dict] = []

        for step in range(self.max_steps):
            prompt = self._build_prompt(user_input, context, step_history)
            llm_output = self.llm.chat(prompt)

            if llm_output is None:
                # LLM unavailable
                agent_response = AgentResponse(
                    type="final_answer",
                    content="抱歉，AI 服务暂时不可用，请稍后重试。",
                    conversation_id=conversation_id,
                )
                self.conversation_manager.add_message(
                    conversation_id, "assistant", agent_response.content,
                    {"action_type": "fallback"},
                )
                return agent_response

            parsed = self._parse_output(llm_output)
            if parsed is None:
                # Parse failed, let LLM retry next step
                step_history.append({
                    "thought": "解析失败",
                    "action": llm_output.strip(),
                    "observation": "输出格式错误，请按 Thought/Action 格式重新输出",
                })
                continue

            thought, action_text = parsed
            action_parsed = self._parse_action(action_text)

            if action_parsed is None:
                step_history.append({
                    "thought": thought,
                    "action": action_text,
                    "observation": "Action 格式错误，请使用 ActionName[参数] 格式",
                })
                continue

            action_type, action_arg = action_parsed

            if action_type == "Finish":
                agent_response = AgentResponse(
                    type="final_answer",
                    content=action_arg,
                    conversation_id=conversation_id,
                )
                self.conversation_manager.add_message(
                    conversation_id, "assistant", action_arg,
                    {"action_type": "final_answer"},
                )
                return agent_response

            if action_type == "AskUser":
                agent_response = AgentResponse(
                    type="ask_user",
                    content=action_arg,
                    conversation_id=conversation_id,
                )
                self.conversation_manager.add_message(
                    conversation_id, "assistant", action_arg,
                    {"action_type": "ask_user"},
                )
                return agent_response

            # Tool dispatch
            observation = self._dispatch_tool(action_type, action_arg)
            step_history.append({
                "thought": thought,
                "action": action_text,
                "observation": observation,
            })

        # ── 5. Fallback on max_steps ──
        fallback_content = "抱歉，我暂时无法确定您需要什么帮助，请再详细描述一下您的问题。"
        agent_response = AgentResponse(
            type="final_answer",
            content=fallback_content,
            conversation_id=conversation_id,
        )
        self.conversation_manager.add_message(
            conversation_id, "assistant", fallback_content,
            {"action_type": "fallback"},
        )
        return agent_response
