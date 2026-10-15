"""本地 Mock LLM（OpenAI 兼容）—— 离线验证整条 Agent 流式链路。

为什么需要它：
- 真实 LLM 要 Key、要花钱、结果不确定，不适合做「每次改完都能跑」的端到端验证；
- 脚本化模型能验证图结构，但验证不了 **HTTP → SSE → 前端** 这一段的 token 流式；
- 这个 Mock 按请求内容判断该「路由 / 调工具 / 出答案」，因此可以把整套
  Supervisor → 子 Agent → 工具 → 流式回答 真跑一遍。

用法：
    python scripts/mock_llm_server.py            # 监听 127.0.0.1:8099
    # 另开终端启动应用：
    set LLM_API_KEY=mock
    set LLM_BASE_URL=http://127.0.0.1:8099/v1
    set LLM_MODEL=mock-model
    python main.py
"""
import json
import re
import sys
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Mock OpenAI-compatible LLM")

ORDER_ID_RE = re.compile(r"\d{4,}")

# 顺序敏感：先匹配更具体的意图
ROUTE_RULES: list[tuple[tuple[str, ...], str]] = [
    # 购物车整单结算必须排在「购物车 → cart_query」**之前**：措辞里同时含
    # 「购物车」和「结算/下单」时，真实 LLM 也是判下单而不是只看购物车。
    (("结算购物车", "购物车结算", "把购物车", "购物车里的都", "购物车都",
      "一起结算", "全部结算", "整单结算"), "place_order"),
    (("我有哪些订单", "订单列表", "我的订单"), "list_orders"),
    (("购物车",), "cart_query"),
    (("有什么商品", "有哪些商品", "商品列表", "在售", "卖什么"), "product"),
    (("售后", "退款", "工单", "退货进度", "换货"), "after_sale"),
    (("快递", "物流", "包裹", "到哪了", "运输"), "logistics"),
    (("下单", "购买", "我要买", "帮我买"), "place_order"),
    (("退货政策", "保修", "政策", "怎么用", "故障", "连不上", "怎么办", "参数",
      "电池", "续航"), "faq"),
    (("订单",), "order"),
]

TOOL_BY_PROMPT: list[tuple[str, str]] = [
    ("FAQ 客服", "search_faq"),
    ("物流查询客服", "query_logistics"),
    ("售后客服", "query_after_sale"),
    ("购物车查询客服", "query_cart"),
    ("订单列表客服", "list_my_orders"),
    ("商品浏览客服", "list_products"),
    ("订单查询客服", "query_order"),
    ("导购选品助手", "search_products"),
]


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return ""


def _system_text(messages: list[dict]) -> str:
    return "\n".join(m.get("content", "") for m in messages
                     if m.get("role") == "system" and isinstance(m.get("content"), str))


def _tool_outputs(messages: list[dict]) -> list[str]:
    return [m.get("content", "") for m in messages if m.get("role") == "tool"]


def pick_route(text: str) -> tuple[str, dict]:
    for keywords, route in ROUTE_RULES:
        if any(k in text for k in keywords):
            slots = {}
            if route in ("order", "logistics"):
                m = ORDER_ID_RE.search(text)
                if m:
                    slots["order_id"] = m.group(0)
            elif route == "after_sale":
                m = ORDER_ID_RE.search(text)
                if m:
                    slots["order_id"] = m.group(0)
            elif route == "place_order":
                # 购物车整单：**故意不填 product** —— 服务端据此判定
                # order_mode="cart"，商品由购物车提供。一旦填了 product，
                # 就会退回单品直购（本场景要守的正是这条回归）。
                if any(k in text for k in ("购物车", "一起结算", "全部结算", "整单")):
                    pass
                elif "智能门锁" in text:
                    slots["product"] = "智能门锁"
                else:
                    # 兜底：从「帮我买 X1 门锁」里抽出用户的口语名，**原样**交给
                    # 服务端 —— 归一化（漏字/连写模糊匹配）由 tools 层负责，
                    # mock 不该替它把名字补全，否则测不出模糊匹配。
                    m = re.search(r"(?:帮我买|我要买|购买|下单|买)\s*([^，,。]+?)(?=[，,。]|$)",
                                  text)
                    if m:
                        slots["product"] = m.group(1).strip()
                addr = re.search(r"(寄到|地址|送到)([^，,。]*)", text)
                if addr:
                    slots["address"] = addr.group(2).strip()
            return route, slots
    return "finish", {}


def decide(messages: list[dict]) -> dict:
    """返回 {"content": str} 或 {"tool": name, "args": {...}}。"""
    system = _system_text(messages)
    user_text = _last_user_text(messages)

    # 1) Supervisor 路由
    if "路由调度器" in system:
        route, slots = pick_route(user_text)
        return {"content": json.dumps({"route": route, "slots": slots,
                                       "extract": user_text[:40]},
                                      ensure_ascii=False)}

    # 2) 已有工具结果 → 出最终回答（会被流式切分送出）
    outputs = _tool_outputs(messages)
    if outputs:
        snippet = outputs[-1].strip().replace("\n", "；")[:160]
        return {"content": f"已为您查到：{snippet}。还需要其他帮助吗？"}

    # 3) 子 Agent 首次调用 → 发起工具调用
    for marker, tool in TOOL_BY_PROMPT:
        if marker in system:
            args: dict = {}
            if tool in ("query_order", "query_logistics", "query_after_sale"):
                m = ORDER_ID_RE.search(user_text)
                args = {"order_id": m.group(0)} if m else {"order_id": ""}
            elif tool == "search_faq":
                # 故意复现真实 LLM 的坏习惯：把问句压成「空格分隔的关键词堆」
                # （实测 DeepSeek 会把「它的电池能续航多久」写成
                #  「X1 智能门锁 电池 续航」，向量检索因而跑偏）。服务端的多路召回
                # 必须有兜底，否则正确答案被 by-source 去重挤掉、答「没有资料」。
                if "电池" in user_text:
                    args = {"query": "X1 智能门锁 电池 续航"}
                else:
                    args = {"query": user_text[:40]}
            elif tool == "search_products":
                m = re.search(r"product=([^、\n]+)", user_text)
                args = {"keyword": (m.group(1) if m else user_text)[:40]}
            return {"tool": tool, "args": args}

    # 4) finish 节点 → 闲聊回复
    return {"content": "您好，我是智能家居商城客服小智，可以帮您查订单、物流、售后，"
                       "也能帮您直接下单～"}


def _chunks(text: str):
    """把回答切成小片，模拟 token 流。"""
    for ch in text:
        yield ch


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))
    decision = decide(messages)
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    created = int(time.time())

    if not stream:
        if "tool" in decision:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_" + uuid.uuid4().hex[:8],
                    "type": "function",
                    "function": {"name": decision["tool"],
                                 "arguments": json.dumps(decision["args"],
                                                         ensure_ascii=False)},
                }],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": decision["content"]}
            finish = "stop"
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created,
            "model": body.get("model", "mock-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
        })

    def sse() -> str:
        base = {"id": cid, "object": "chat.completion.chunk", "created": created,
                "model": body.get("model", "mock-model")}
        if "tool" in decision:
            # 工具调用也走增量格式，验证适配器能否正确拼接 arguments 片段
            args = json.dumps(decision["args"], ensure_ascii=False)
            yield "data: " + json.dumps({**base, "choices": [{
                "index": 0, "finish_reason": None,
                "delta": {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": decision["tool"], "arguments": ""}}]},
            }]}, ensure_ascii=False) + "\n\n"
            for i in range(0, len(args), 8):
                yield "data: " + json.dumps({**base, "choices": [{
                    "index": 0, "finish_reason": None,
                    "delta": {"tool_calls": [{"index": 0,
                                              "function": {"arguments": args[i:i + 8]}}]},
                }]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({**base, "choices": [{
                "index": 0, "finish_reason": "tool_calls", "delta": {}}]},
                ensure_ascii=False) + "\n\n"
        else:
            for piece in _chunks(decision["content"]):
                yield "data: " + json.dumps({**base, "choices": [{
                    "index": 0, "finish_reason": None,
                    "delta": {"content": piece}}]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({**base, "choices": [{
                "index": 0, "finish_reason": "stop", "delta": {}}]},
                ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"Mock LLM listening on http://127.0.0.1:{port}/v1")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
