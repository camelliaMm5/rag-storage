"""Supervisor 多智能体图：意图路由 → 领域子 Agent（ReAct）→ 澄清 / 下单确认。

本次重构与旧版的区别（旧版问题见 docs/重构改动说明.md）：
1. **节点全部异步**：与 ``AsyncSqliteSaver`` 口径统一，从根上消除「同步
   checkpointer 支撑不了 astream」的冲突（旧版因此被迫维护两条链路）。
2. **子 Agent 是真 ReAct 循环**：节点绑定**统一只读工具集**（而非按 route 把
   工具切碎，见下方 READ_TOOLS 的演进记录），LLM 自己决定调哪个工具、要不要
   连续多步；工具返回「查不到」时还能换个工具自我纠正（例如 list_my_orders
   定位订单号 → query_order 取商品明细），而不是把错误文本丢给用户。
3. **会话历史注入 + 指代消解**：子 Agent 能看到前几轮对话；Supervisor 判断意图时
   也带上最近几轮，并对「当前状态 / 到哪了」这类省略句做**确定性的订单号恢复**
   （最近订单号持久化在 ``state.last_order_id``，下单成功即刻回写）—— 否则用户
   下一句省略订单号时，子 Agent 手边没有单号，只能反问「请把订单号发给我」。
4. **澄清闭环**：必填槽位缺失 → clarify 节点反问，意图存进 ``pending_intent``，
   下一轮补参直接续跑，不用用户重述。
5. **下单两阶段**：prepare 只读校验 + 回显，用户确认后 commit 单事务落库，
   不可逆操作有 human-in-the-loop。
6. **图走向由纯函数决定**：``resolve_target`` 不依赖 LLM 输出，条件边永不 KeyError。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from domain.customer_service import prompts as P
from domain.customer_service.react import ReactOutcome, ToolStep, run_react
from domain.customer_service.routing import (
    DEFAULT_ROUTE,
    ORDER_MODE_CART,
    ORDER_MODE_SINGLE,
    ROUTE_TO_NODE,
    clarify_question,
    extract_order_id,
    is_affirmative,
    is_negative,
    merge_slots,
    missing_slots,
    normalize_route,
    promote_status_query,
    recent_order_id,
    resolve_order_mode,
    resolve_target,
)

logger = logging.getLogger(__name__)

MAX_SUB_AGENT_STEPS = int(os.getenv("AGENT_MAX_STEPS", "4"))
HISTORY_WINDOW = int(os.getenv("AGENT_HISTORY_WINDOW", "12"))
# Supervisor 做「指代消解」时喂给 LLM 的历史轮数（比子 Agent 的窗口小：
# 它只需看到「上一句在聊哪笔订单」，不需要全部上下文）。
SUPERVISOR_HISTORY_WINDOW = int(os.getenv("SUPERVISOR_HISTORY_WINDOW", "6"))


# ── State ─────────────────────────────────────────────────────────────
def _append_steps(old: list | None, new: list | None) -> list:
    """累积每一步的工具调用轨迹（用于 trace 与审计），最多保留 60 条。"""
    merged = list(old or []) + list(new or [])
    return merged[-60:]


class MasterState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    route: str                      # 归一化后的白名单路由
    extract: str                    # 主要参数摘要（订单号等），用于展示与 trace
    slots: dict                     # 抽取到的槽位（order_id / product / address ...）
    user_query: str                 # 本轮用户原话
    user_id: str                    # 鉴权主体，用于数据隔离
    pending_intent: dict | None     # 反问后待续跑的意图
    pending_order: dict | None      # 待确认的订单草稿
    order_action: str | None        # prepare / commit / cancel
    order_mode: str                 # single（点名单品）/ cart（购物车整单）
    last_order_id: str              # 最近一笔订单号（跨轮指代消解用）
    tool_steps: Annotated[list[dict], _append_steps]


# ── 工具函数 ───────────────────────────────────────────────────────────
def _last_human(state: MasterState) -> str:
    msgs = state.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return state.get("user_query", "") or ""


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _parse_json_object(text: str) -> dict:
    """从 LLM 文本里抠出第一个 JSON 对象（容忍前后废话与代码块围栏）。"""
    if not text:
        return {}
    cleaned = text.replace("```json", "```").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(cleaned[start:end])
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _task_message(state: MasterState) -> str:
    """交给子 Agent 的任务描述：原话 + 已抽到的参数（含历史上下文里的槽位）。

    显式写入「上文最近订单号」：用户说「当前状态」时本轮原话里没有单号，子 Agent
    若要真的去查，就必须从**这里**拿到它 —— 否则只能反问用户重复一遍。
    """
    query = state.get("user_query") or _last_human(state)
    slots = _as_dict(state.get("slots"))
    hint = "、".join(f"{k}={v}" for k, v in slots.items() if v) or "（无）"
    last_oid = slots.get("order_id") or state.get("last_order_id") or ""
    ctx = f"\n上文最近订单号：{last_oid}（若用户指的是「那笔订单」，就用它）" if last_oid else ""
    return (
        f"用户本轮说：{query}\n"
        f"系统已抽取到的参数：{hint}{ctx}\n"
        f"请调用合适的工具获取真实数据，再回答用户。不要编造数据。"
    )


async def _lookup_product(keyword: str) -> dict | None:
    """确定性商品检索（不用 LLM，避免编造商品/价格）。"""
    from tools.order_search import search_products

    rows = await asyncio.to_thread(search_products, keyword)
    if not rows:
        return None
    for r in rows:
        if str(r.get("name", "")).strip() == keyword.strip():
            return r
    return rows[0]


# ── Supervisor ─────────────────────────────────────────────────────────
def create_supervisor_node(llm):
    """只做「分类 + 抽槽 + 上下文恢复」，不做回答。"""

    async def supervisor_node(state: MasterState) -> dict:
        user_query = _last_human(state)
        route, slots, extract = DEFAULT_ROUTE, {}, ""

        # ① LLM 路由决策（非流式：路由过程不该以 token 形式泄给用户）
        #    带上最近几轮历史：只给本轮原话，「它 / 那笔订单 / 当前状态」这类
        #    指代句会直接掉到 finish（旧版就是这样丢掉上下文的）。
        try:
            history = list(state.get("messages") or [])
            prior = (history[:-1]
                     if history and isinstance(history[-1], HumanMessage) else history)
            convo = [SystemMessage(content=P.SUPERVISOR_SYSTEM_PROMPT)]
            convo.extend(prior[-SUPERVISOR_HISTORY_WINDOW:])
            convo.append(HumanMessage(content=user_query))
            resp = await llm.ainvoke(convo)
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            parsed = _parse_json_object(text)
            route = normalize_route(parsed.get("route"))
            slots = {k: v for k, v in _as_dict(parsed.get("slots")).items()
                     if v is not None and str(v).strip()}
            extract = str(parsed.get("extract") or "").strip()
        except Exception:
            logger.exception("supervisor routing failed, falling back to %s", DEFAULT_ROUTE)

        # ② 与上一轮已抽取的槽位合并（跨轮记忆：用户第二轮只说「它到哪了」，
        #    订单号要能从上文继承过来）
        llm_slots = dict(slots)   # ★ merge 前快照：下单模式只看本轮抽到的 product
        slots = merge_slots(_as_dict(state.get("slots")), slots)

        # ②.5 判定下单模式：点名单品(single) / 结算购物车(cart)。
        #      只依据**本轮**的 product 槽位 —— 上一轮遗留的 product 不能让本轮
        #      「帮我下单」被误判成买单件。
        order_mode = state.get("order_mode") or ORDER_MODE_SINGLE
        if route == "place_order":
            order_mode = resolve_order_mode(llm_slots)

        # ③ 恢复上一轮的反问上下文
        pending_intent = _as_dict(state.get("pending_intent")) or None
        pending_order = _as_dict(state.get("pending_order")) or None
        action: str | None = None

        if pending_intent:
            same_topic = route in (DEFAULT_ROUTE, pending_intent.get("route"))
            if same_topic:
                route = pending_intent.get("route") or route
                slots = merge_slots(pending_intent.get("slots"), slots)
                if pending_intent.get("order_action"):
                    action = pending_intent["order_action"]
                # ★ 续跑时恢复下单模式：clarify 只反问过地址，若这里不回填，
                #   用户补完地址后的 prepare 会退回 single（整单变单品）。
                if pending_intent.get("order_mode"):
                    order_mode = pending_intent["order_mode"]
            # 无论是否续跑，pending 都已消费（需要时会由 clarify 重新写入）
            pending_intent = None

        # ④ 正则兜底补订单号 —— 必须放在「续跑」之后，并且按**最终** route 判断。
        #    否则用户在反问后只回一个 "1001" 时，LLM 往往判成 finish，
        #    走不到这一步，补参就永远补不上。
        if route in ("order", "logistics", "after_sale") and not (
            slots.get("order_id") or slots.get("ticket_id")
        ):
            oid = extract_order_id(user_query)
            if oid:
                slots["order_id"] = oid

        # ④.5 上下文指代恢复（确定性兜底）
        #      「那笔订单我已经支付了」→「当前状态」：本轮既没单号也没关键词，
        #      但上文/上一轮出现过订单号。不恢复的话 route 会停在被 LLM 判出的
        #      finish，用户只能听子 Agent 反问「请把订单号发给我」。
        #      来源优先级：本轮槽位 > 上一轮持久化的 last_order_id > 历史消息文本。
        carried = (
            slots.get("order_id") or slots.get("ticket_id")
            or state.get("last_order_id")
            or recent_order_id(state.get("messages"))
        )
        if carried:
            if route == DEFAULT_ROUTE:
                promoted = promote_status_query(user_query)
                if promoted:
                    logger.info("promote omitted query %r -> %s (order_id=%s)",
                                user_query, promoted, carried)
                    route = promoted
            if route in ("order", "logistics", "after_sale") and not (
                slots.get("order_id") or slots.get("ticket_id")
            ):
                slots["order_id"] = str(carried)

        # ⑤ 下单两阶段状态机
        if action is None:
            if route == "place_order":
                if pending_order:
                    if is_negative(user_query):
                        action = "cancel"
                    elif is_affirmative(user_query):
                        action = "commit"
                    else:
                        action = "prepare"
                else:
                    action = "prepare"
            elif pending_order and is_negative(user_query):
                action = "cancel"
            elif pending_order and is_affirmative(user_query):
                # 用户只回了「好的」，LLM 可能判成 finish —— 这里补回来
                route, action = "place_order", "commit"
            elif pending_order:
                # 话题转移 → 丢弃草稿，避免挂着一个过期确认
                logger.info("dropping stale order draft (topic changed)")
                pending_order = None

        # ★ 进入 commit/cancel 时，模式必须取自**草稿**：用户回「确认」的那一轮
        #   既没有商品名也没有「购物车」字样，按本轮判定会退回 single，
        #   从而把整单结算错误地降级成单品下单。
        if action in ("commit", "cancel") and pending_order:
            order_mode = pending_order.get("mode") or order_mode

        # 用户只说「取消」但没有草稿 → 当作闲聊
        if action == "cancel" and not pending_order:
            action = None

        if not extract:
            extract = str(slots.get("order_id") or slots.get("ticket_id")
                          or slots.get("product") or "")

        return {
            "route": route,
            "slots": slots,
            "extract": extract,
            "user_query": user_query,
            "order_action": action,
            "order_mode": order_mode,
            "pending_intent": pending_intent,
            "pending_order": pending_order,
            # 持久化最近订单号：下一轮用户省略单号时靠它恢复
            "last_order_id": str(
                slots.get("order_id") or state.get("last_order_id") or ""),
        }

    return supervisor_node


def create_selector(valid_nodes: frozenset[str]):
    """条件边选择器：结果必定是图中真实节点名，因此永不 KeyError。"""

    def select(state: MasterState) -> str:
        target = resolve_target(state)
        if target not in valid_nodes:
            logger.warning("resolved target %r not in graph %s -> finish",
                           target, sorted(valid_nodes))
            return "finish"
        return target

    return select


# ── 领域子 Agent（ReAct） ──────────────────────────────────────────────
def create_react_node(*, node_name: str, llm, tools_by_name: dict, prompt: str,
                      max_steps: int = MAX_SUB_AGENT_STEPS):
    """把「工具集 + prompt」包成一个 ReAct 子 Agent 节点。"""

    async def node(state: MasterState) -> dict:
        history = list(state.get("messages") or [])
        prior = history[:-1] if history and isinstance(history[-1], HumanMessage) else history
        messages = prior + [HumanMessage(content=_task_message(state))]

        try:
            outcome: ReactOutcome = await run_react(
                llm=llm, tools_by_name=tools_by_name, system_prompt=prompt,
                messages=messages, max_steps=max_steps, window=HISTORY_WINDOW,
                # 服务端可信身份：注入到声明了 user_id 的工具上，LLM 无法伪造
                user_context={"user_id": state.get("user_id", "")},
            )
        except Exception:
            logger.exception("sub-agent %s failed", node_name)
            outcome = ReactOutcome("抱歉，查询过程出现异常，请稍后再试一次。")

        return {
            "messages": [AIMessage(content=outcome.content or "抱歉，没有查到相关信息。")],
            "tool_steps": outcome.tool_calls(),
        }

    return node


def create_finish_node(llm):
    """闲聊 / 兜底：无工具，直接用 astream 产出（这样 token 能流到前端）。"""

    async def finish_node(state: MasterState) -> dict:
        history = list(state.get("messages") or [])
        prior = history[:-1] if history and isinstance(history[-1], HumanMessage) else history
        user_query = state.get("user_query") or _last_human(state)
        messages = prior[-HISTORY_WINDOW:] + [HumanMessage(content=user_query)]

        content = ""
        try:
            aggregated = None
            convo = [SystemMessage(content=P.FINISH_PROMPT), *messages]
            async for chunk in llm.astream(convo):
                aggregated = chunk if aggregated is None else aggregated + chunk
            content = (aggregated.content if aggregated is not None else "") or ""
        except Exception:
            logger.exception("finish node failed")
        if not content:
            content = (
                "您好，我是智能家居商城的客服小智 👋\n"
                "我可以帮您：查订单 / 查物流 / 查售后进度 / 看购物车 / 帮忙下单 / "
                "解答产品与保修问题。\n请告诉我您需要什么帮助～"
            )
        return {"messages": [AIMessage(content=content)]}

    return finish_node


# ── 澄清 ───────────────────────────────────────────────────────────────
def create_clarify_node():
    """缺必填槽位 → 反问 + 把意图存进 state，下一轮续跑。"""

    async def clarify_node(state: MasterState) -> dict:
        route = state.get("route") or DEFAULT_ROUTE
        action = state.get("order_action")
        mode = state.get("order_mode") or ""
        slots = _as_dict(state.get("slots"))
        missing = missing_slots(route, slots, mode)
        question = clarify_question(route, missing)

        pending = {
            "route": route,
            "order_action": action,
            "order_mode": mode,
            "slots": slots,
            "missing": [s.key for s in missing],
        }
        logger.info("clarify: route=%s missing=%s", route, pending["missing"])
        return {
            "messages": [AIMessage(content=question)],
            "pending_intent": pending,
            "order_action": action,
        }

    return clarify_node


# ── 下单：两阶段 ───────────────────────────────────────────────────────
def create_place_order_nodes(llm):
    """返回 (prepare, commit, cancel) 三个节点。

    prepare：只读校验 + 生成草稿回显（不写库）
    commit ：用户确认后单事务落库（OrderService.create_direct）
    cancel ：丢弃草稿
    """

    async def _resolve_uid(user_id: str) -> int | None:
        from tools.order_search import _resolve_user_id

        return await asyncio.to_thread(_resolve_user_id, user_id)

    async def prepare_node(state: MasterState) -> dict:
        slots = _as_dict(state.get("slots"))
        uid = await _resolve_uid(state.get("user_id", ""))
        if uid is None:
            return {"messages": [AIMessage(content=P.LOGIN_REQUIRED_MSG)],
                    "pending_order": None, "order_action": None}

        address = str(slots.get("address") or "").strip()
        recipient = str(slots.get("recipient") or "").strip() or "本人"

        # ★ 分支一：购物车整单结算。商品来自购物车，**不做关键词匹配**。
        #   旧版没有这条分支，用户加购 3 件后说「帮我下单」只能按单个商品
        #   关键词下一件，其余商品被静默忽略。
        if (state.get("order_mode") or "") == ORDER_MODE_CART:
            return await _prepare_cart(uid, address, recipient)

        keyword = str(slots.get("product") or "").strip()
        try:
            quantity = max(1, int(slots.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1

        # ★ 确定性检索与校验（不让 LLM 参与金额/库存计算）
        try:
            matched = await _lookup_product(keyword)
        except Exception:
            logger.exception("product lookup failed")
            matched = None

        if not matched:
            return {
                "messages": [AIMessage(content=(
                    f"没有找到与「{keyword}」匹配的在售商品。\n"
                    f"可以换一个关键词，或者告诉我商品编号，我再帮您找找～"
                ))],
                "pending_order": None, "order_action": None,
            }

        stock = int(matched.get("stock") or 0)
        price = float(matched.get("price") or 0)
        if stock < quantity:
            return {
                "messages": [AIMessage(content=(
                    f"「{matched['name']}」当前库存不足（剩余 {stock} 件），"
                    f"暂时没法下单。要不要看看别的商品？"
                ))],
                "pending_order": None, "order_action": None,
            }

        draft = {
            "mode": ORDER_MODE_SINGLE,
            "product_id": int(matched["id"]),
            "name": str(matched["name"]),
            "price": price,
            "quantity": quantity,
            "address": address,
            "recipient": recipient,
            "total": round(price * quantity, 2),
            "stock": stock,
        }
        text = P.ORDER_DRAFT_TEMPLATE.format(
            name=draft["name"], price=draft["price"], quantity=draft["quantity"],
            total=draft["total"], recipient=draft["recipient"], address=draft["address"],
        )
        logger.info("order draft prepared: %s x%s", draft["name"], draft["quantity"])
        return {
            "messages": [AIMessage(content=text)],
            "pending_order": draft,
            "order_action": None,
            "extract": draft["name"],
            # 把「用户口语名」规范化成**库里的真实商品名**回写槽位：
            # 用户说「X1 门锁」被模糊匹配到「X1 智能门锁」后，后续轮不必再猜。
            "slots": {**slots, "product": draft["name"]},
        }

    async def _prepare_cart(uid: int, address: str, recipient: str) -> dict:
        """购物车整单：读购物车 → 校验在售/库存 → 生成**多商品**草稿（只读，不写库）。

        金额与库存全部由服务端算，不经 LLM —— 与单品直购同一条纪律。
        """
        from domain.shop.cart import CartService

        try:
            rows = await asyncio.to_thread(CartService.list_items, uid)
        except Exception:
            logger.exception("cart lookup failed")
            return {"messages": [AIMessage(content="读取购物车时出错了，请稍后再试一次。")],
                    "pending_order": None, "order_action": None}

        rows = [r for r in (rows or []) if r]
        if not rows:
            return {"messages": [AIMessage(content=P.CART_EMPTY_MSG)],
                    "pending_order": None, "order_action": None}

        lines: list[str] = []
        detail: list[dict] = []
        total = 0.0
        for it in rows:
            name = str(it.get("product_name") or "")
            price = float(it.get("price") or 0)
            qty = max(1, int(it.get("quantity") or 1))
            stock = int(it.get("stock") or 0)
            if str(it.get("status") or "") != "on_sale":
                return {"messages": [AIMessage(content=(
                    f"购物车里的「{name}」已经下架了，没法一起下单。\n"
                    f"请先把它从购物车移除，我再帮您结算其余商品～"))],
                    "pending_order": None, "order_action": None}
            if stock < qty:
                return {"messages": [AIMessage(content=(
                    f"「{name}」库存不足（剩余 {stock} 件，购物车里放了 {qty} 件）。\n"
                    f"可以先去购物车调小数量，或把它移除后再让我结算～"))],
                    "pending_order": None, "order_action": None}
            subtotal = round(price * qty, 2)
            total += subtotal
            lines.append(f"· {name}  ¥{price:.2f} × {qty} = ¥{subtotal:.2f}")
            detail.append({
                "product_id": int(it.get("product_id") or 0),
                "name": name, "price": price, "quantity": qty,
            })

        total = round(total, 2)
        draft = {
            "mode": ORDER_MODE_CART,
            "items": detail,
            "address": address,
            "recipient": recipient,
            "total": total,
        }
        text = P.CART_ORDER_DRAFT_TEMPLATE.format(
            lines="\n".join(lines), total=total,
            recipient=recipient, address=address,
        )
        logger.info("cart order draft prepared: %d kind(s), total=%s", len(detail), total)
        return {
            "messages": [AIMessage(content=text)],
            "pending_order": draft,
            "order_action": None,
            "extract": f"购物车整单（{len(detail)} 种商品）",
        }

    async def commit_node(state: MasterState) -> dict:
        from domain.shop.order import OrderService

        slots = _as_dict(state.get("slots"))
        draft = _as_dict(state.get("pending_order"))
        if not draft:
            return {"messages": [AIMessage(content=P.ORDER_CANCELLED_MSG)],
                    "order_action": None}

        uid = await _resolve_uid(state.get("user_id", ""))
        if uid is None:
            return {"messages": [AIMessage(content=P.LOGIN_REQUIRED_MSG)],
                    "pending_order": None, "order_action": None}

        mode = draft.get("mode") or ORDER_MODE_SINGLE

        if mode == ORDER_MODE_CART:
            # 确认期间购物车可能被改动（前端删了商品 / 改了数量）→ 先比对再落库，
            # 避免「和用户确认的是 A，实际下单成 B」。
            from domain.shop.cart import CartService

            try:
                current = await asyncio.to_thread(CartService.list_items, uid)
            except Exception:
                logger.exception("cart re-check failed")
                current = None
            if current is not None:
                want = {int(i["product_id"]): int(i["quantity"])
                        for i in (draft.get("items") or [])}
                now = {int(r["product_id"]): int(r["quantity"])
                       for r in current if r}
                if want != now:
                    logger.info("cart changed during confirmation: %s -> %s", want, now)
                    return {
                        "messages": [AIMessage(content=(
                            "购物车内容刚刚发生了变化（商品或数量被改动过）。\n"
                            "为避免下单结果和您确认的不一致，这次结算我先取消了 —— "
                            "请确认购物车后再说一次「结算购物车」即可～"))],
                        "pending_order": None, "order_action": None,
                    }

        try:
            if mode == ORDER_MODE_CART:
                # 整单：OrderService.create 单事务锁购物车行 → 扣库存 → 建单 → 清空购物车
                order = await asyncio.to_thread(
                    OrderService.create,
                    user_id=uid,
                    address=draft["address"],
                    recipient=draft["recipient"],
                )
            else:
                order = await asyncio.to_thread(
                    OrderService.create_direct,
                    user_id=uid,
                    product_id=draft["product_id"],
                    quantity=draft["quantity"],
                    address=draft["address"],
                    recipient=draft["recipient"],
                )
        except Exception as e:
            logger.exception("order commit failed")
            return {
                "messages": [AIMessage(content=f"下单失败：{e}\n可以稍后再试，或联系人工客服。")],
                "pending_order": None, "order_action": None,
            }

        total = float(order.get("total_amount") or draft["total"])
        if mode == ORDER_MODE_CART:
            items = draft.get("items") or []
            lines = "\n".join(
                f"· {i['name']}  ¥{i['price']:.2f} × {i['quantity']}"
                f" = ¥{i['price'] * i['quantity']:.2f}"
                for i in items)
            text = P.CART_ORDER_COMMIT_TEMPLATE.format(
                order_id=order["id"], count=len(items),
                units=sum(int(i["quantity"]) for i in items),
                lines=lines, total=total, address=draft["address"],
            )
        else:
            text = P.ORDER_COMMIT_TEMPLATE.format(
                order_id=order["id"], name=draft["name"], quantity=draft["quantity"],
                total=total, address=draft["address"],
            )
        logger.info("order committed: id=%s", order["id"])
        return {
            "messages": [AIMessage(content=text)],
            "pending_order": None,
            "order_action": None,
            "extract": str(order["id"]),
            # ★ 关键：把新订单号回写槽位并持久化为「最近订单号」。
            #   没有这一条，下一轮「当前状态 / 到哪了」就无从继承单号，
            #   子 Agent 只能反问用户（旧版实测症状）。
            "slots": {**slots, "order_id": str(order["id"])},
            "last_order_id": str(order["id"]),
        }

    async def cancel_node(state: MasterState) -> dict:
        return {
            "messages": [AIMessage(content=P.ORDER_CANCELLED_MSG)],
            "pending_order": None,
            "order_action": None,
        }

    return prepare_node, commit_node, cancel_node


# ── 图装配 ─────────────────────────────────────────────────────────────
# 工具绑定策略：**统一只读工具集 + prompt 软引导**（不是按 route 把工具切碎）。
#
# 演进记录：早期是「route → 该域专属的 1~3 把工具」硬绑定。问题在于工具宇宙
# 在 LLM 看到之前就被 route 切死了 —— 子 Agent 只能「盒内自治」，无法跨步编排。
# 典型症状：list_orders 只挂了 list_my_orders，拿到订单列表后想再查商品明细时
# 手边没有第二把工具，只能把「接口没返回明细」抛回给用户。
#
# 现在改为：每个查询类子 Agent 都拿到**同一套只读工具**，领域差异交给 prompt
# （「优先用 X，必要时换 Y」）表达。理由：
# - 安全性不来自「切工具」，而来自 ①只读工具无副作用 ②写操作走确定性两阶段
#   ③身份由服务端注入 —— 放开只读工具集**不动**这三条边界；
# - 路由的价值仍在（抽槽 / 澄清 / 写操作闸门 / 白名单 / trace），它靠的是
#   确定性控制面，不靠工具划分。
# 代价：单节点工具变多，LLM 选择准确率略降 —— 用 prompt 软引导 + max_steps
# 兜底。若要按域收窄，另行裁剪 READ_TOOLS 的子集即可。
READ_TOOLS: tuple[str, ...] = (
    "search_faq", "query_order", "list_my_orders",
    "query_logistics", "query_after_sale", "query_cart",
    "search_products", "list_products",
)

AGENT_SPECS: dict[str, dict] = {
    "faq": {"prompt": P.FAQ_AGENT_PROMPT, "tools": READ_TOOLS},
    "order": {"prompt": P.ORDER_AGENT_PROMPT, "tools": READ_TOOLS},
    "logistics": {"prompt": P.LOGISTICS_AGENT_PROMPT, "tools": READ_TOOLS},
    "after_sale": {"prompt": P.AFTER_SALE_AGENT_PROMPT, "tools": READ_TOOLS},
    "cart_query": {"prompt": P.CART_QUERY_PROMPT, "tools": READ_TOOLS},
    "list_orders": {"prompt": P.LIST_ORDERS_PROMPT, "tools": READ_TOOLS},
    "product": {"prompt": P.PRODUCT_AGENT_PROMPT, "tools": READ_TOOLS},
}

PLACE_ORDER_TOOL = "search_products"


def build_master_graph(
    *,
    llm,
    tools_by_name: dict,
    checkpointer=None,
    max_steps: int = MAX_SUB_AGENT_STEPS,
):
    """装配 Supervisor 图。

    ``tools_by_name`` 是 ``{tool_name: Tool}``。缺失的领域会被自动跳过，
    因此离线测试可以只注入需要用到的桩工具。
    """
    workflow = StateGraph(MasterState)
    valid_nodes: set[str] = {"finish", "clarify"}

    workflow.add_node("supervisor", create_supervisor_node(llm))
    workflow.add_node("finish", create_finish_node(llm))
    workflow.add_node("clarify", create_clarify_node())

    for route, spec in AGENT_SPECS.items():
        node_name = ROUTE_TO_NODE[route]
        available = [tools_by_name[n] for n in spec["tools"] if n in tools_by_name]
        if not available:
            logger.info("route %s skipped (no tools available)", route)
            continue
        bound_llm = llm.bind_tools([t.to_langchain_tool() for t in available])
        workflow.add_node(node_name, create_react_node(
            node_name=node_name, llm=bound_llm,
            tools_by_name={t.name: t for t in available},
            prompt=spec["prompt"], max_steps=max_steps,
        ))
        valid_nodes.add(node_name)

    # 下单两阶段的装配开关：以 PLACE_ORDER_TOOL（== search_products）是否在场为
    # 信号。注意这条开关同时管着**购物车整单**（cart 模式并不用 search_products，
    # 商品来自购物车）—— 裁剪只读工具集时要留意，删掉 search_products 会把整单
    # 结算一起关掉（离线测试若只注入桩工具，也必须注入它才能走到下单节点）。
    if PLACE_ORDER_TOOL in tools_by_name:
        prepare, commit, cancel = create_place_order_nodes(llm)
        workflow.add_node("place_order_prepare", prepare)
        workflow.add_node("place_order_commit", commit)
        workflow.add_node("place_order_cancel", cancel)
        valid_nodes.update({"place_order_prepare", "place_order_commit",
                            "place_order_cancel"})

    workflow.set_entry_point("supervisor")
    path_map = {n: n for n in valid_nodes}
    workflow.add_conditional_edges("supervisor", create_selector(frozenset(valid_nodes)),
                                   path_map)
    for n in valid_nodes:
        workflow.add_edge(n, END)

    logger.info("master graph compiled with nodes: %s", sorted(valid_nodes))
    return workflow.compile(checkpointer=checkpointer)
