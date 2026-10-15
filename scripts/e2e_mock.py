"""端到端验证：真实 HTTP 应用 + 本地 Mock LLM。

与其它测试的分工：
- tests/test_agent_offline.py —— 进程内、异步、离线用例，验证「图 + 节点 + 工具」逻辑；
- scripts/scenario_test.py   —— 脚本化模型回放多轮剧本，验证「跨轮状态机」；
- 本脚本                     —— 打真实 uvicorn 端口，验证 **HTTP → LangGraph →
  工具 → SSE token → 前端** 这一整条链，尤其是流式适配器与前端契约。

前置：
    1) python scripts/mock_llm_server.py            # 8099
    2) LLM_API_KEY=mock LLM_BASE_URL=http://127.0.0.1:8099/v1 LLM_MODEL=mock-model \
       python -m uvicorn main:app --port 8131

用法：
    python scripts/e2e_mock.py [base_url]
"""
import json
import re
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8131"

FALLBACK_GREETING = "我是智能家居商城客服小智"

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {extra}")


def _req(method: str, path: str, token: str | None = None, body: dict | None = None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def api(method, path, token=None, body=None):
    status, payload = _req(method, path, token, body)
    return status, payload


def sse(path: str, token: str, body: dict):
    """读取 SSE 流，返回事件列表。"""
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    events = []
    with urllib.request.urlopen(req, timeout=90) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def token_for(uid: str) -> str:
    st, p = api("POST", "/api/token", body={"user_id": uid})
    assert st == 200, p
    return p["access_token"]


def traces_of(user_id: str, conv_id: str) -> list[dict]:
    st, p = api("GET", "/api/traces", token=token_for(user_id))
    assert st == 200, p
    return [it for it in p.get("items", []) if it["conversation_id"] == conv_id]


def chat(token: str, conv: str, msg: str) -> dict:
    st, p = api("POST", "/api/chat", token=token, body={"conversation_id": conv, "message": msg})
    assert st == 200, (st, p)
    return p


def main() -> int:
    print("=" * 68)
    print("端到端验证（真实 HTTP + Mock LLM）")
    print("=" * 68)

    # ── 0. 健康检查 ──
    print("\n场景 0  健康检查")
    st, h = api("GET", "/api/health")
    check("应用存活", st == 200, str(h))
    check("LLM 可用", h.get("llm_available") is True)
    check("Agent 就绪", h.get("agent_ready") is True)
    check("checkpointer 为异步", h.get("checkpointer") == "AsyncSqliteSaver", str(h.get("checkpointer")))

    # ── 1. 物流查询（含工具调用） ──
    print("\n场景 1  物流查询：订单1001到哪了")
    t2 = token_for("2")
    st, c = api("POST", "/api/conversations", token=t2)
    conv1 = c["conversation_id"]
    ans = chat(t2, conv1, "订单1001到哪了")
    check("返回 final_answer", ans["type"] == "final_answer", ans["type"])
    check("不再是降级闲聊", FALLBACK_GREETING not in ans["content"], ans["content"][:60])
    check("回答非空", len(ans["content"].strip()) > 0)
    tr = traces_of("2", conv1)
    check("trace 已落库", len(tr) >= 1, str(len(tr)))
    if tr:
        names = {t.get("name") for t in (tr[0].get("tool_calls") or [])}
        check("route 非 finish（真路由成功）", tr[0]["route"] != "finish", tr[0]["route"])
        check("调用了 query_logistics 工具", "query_logistics" in names, str(names))
        check("工具拿到了 user_id（不再要求登录）",
              "请先登录" not in ans["content"], ans["content"][:80])
        check("记录了耗时", (tr[0].get("duration_ms") or 0) > 0, str(tr[0].get("duration_ms")))

    # ── 2. 澄清闭环（跨轮补参） ──
    print("\n场景 2  澄清闭环：先缺订单号，再补上")
    conv2 = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    a1 = chat(t2, conv2, "帮我查订单")
    check("首轮发起澄清（要求补充订单号）",
          ("订单号" in a1["content"] or "提供" in a1["content"] or "编号" in a1["content"]),
          a1["content"][:80])
    a2 = chat(t2, conv2, "1001")
    check("次轮补参后给出结果", FALLBACK_GREETING not in a2["content"] and len(a2["content"]) > 0,
          a2["content"][:80])
    tr2 = traces_of("2", conv2)
    check("第二轮 route 恢复为 order/logistics（跨轮记忆生效）",
          any(it["route"] in ("order", "logistics") for it in tr2),
          str([it["route"] for it in tr2]))

    # ── 3. FAQ（RAG 检索） ──
    print("\n场景 3  FAQ：X1 智能门锁连不上怎么办")
    conv3 = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    a3 = chat(t2, conv3, "X1智能门锁连不上怎么办")
    check("走 faq 路由且有回答", FALLBACK_GREETING not in a3["content"] and len(a3["content"]) > 0,
          a3["content"][:80])
    tr3 = traces_of("2", conv3)
    check("调用了 search_faq",
          any(t.get("name") == "search_faq"
              for it in tr3 for t in (it.get("tool_calls") or [])),
          str([it.get("tool_calls") for it in tr3]))

    # ── 3B. 商品浏览（product 路由 + list_products） ──
    print("\n场景 3B  商品浏览：你当前有什么商品在售")
    convp = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    ap = chat(t2, convp, "你当前有什么商品在售")
    check("商品浏览有回答、非降级闲聊",
          FALLBACK_GREETING not in ap["content"] and len(ap["content"].strip()) > 0,
          ap["content"][:80])
    check("回答里出现了在售商品（不是「无法查询」）",
          any(k in ap["content"] for k in ("X1", "智能门锁", "在售", "商品")),
          ap["content"][:120])
    trp = traces_of("2", convp)
    check("route 为 product（未被兜底成 finish）",
          any(it["route"] == "product" for it in trp), str([it["route"] for it in trp]))
    check("调用了 list_products 工具",
          any(t.get("name") == "list_products"
              for it in trp for t in (it.get("tool_calls") or [])),
          str([it.get("tool_calls") for it in trp]))

    # ── 3C. 物流演进：下单 → 虚拟支付 → 物流记录生成（读时推演） ──
    print("\n场景 3C  物流演进：下单 → 虚拟支付 → 查物流")
    st, lg = api("POST", "/api/shop/auth/login",
                 body={"email": "zhangsan@shop.local", "password": "user123"})
    check("商城登录成功（拿到 shop token）",
          st == 200 and lg.get("code") == 0, str(lg)[:100])
    shop_tok = (lg.get("data") or {}).get("access_token", "") if st == 200 else ""
    st, crt = api("POST", "/api/shop/cart", token=shop_tok,
                  body={"product_id": 1, "quantity": 1})
    check("加入购物车", st == 200 and crt.get("code") == 0, str(crt)[:100])
    st, od = api("POST", "/api/shop/orders", token=shop_tok,
                 body={"address": "广东省广州市天河区体育西路123号"})
    check("下单成功（初始为「待支付」）", st == 200 and od.get("code") == 0, str(od)[:120])
    oid = (od.get("data") or {}).get("id") if st == 200 else None
    st, _lg0 = api("GET", f"/api/shop/logistics/{oid}", token=shop_tok)
    check("未支付时没有物流记录（404）", st == 404, str(st))
    st, pay = api("POST", f"/api/shop/orders/{oid}/pay", token=shop_tok)
    check("虚拟支付成功", st == 200 and pay.get("code") == 0, str(pay)[:120])
    st, lgx = api("GET", f"/api/shop/logistics/{oid}", token=shop_tok)
    lgd = (lgx.get("data") or {}) if st == 200 else {}
    check("支付后生成物流记录（有运单号）",
          st == 200 and bool(lgd.get("tracking_number")), str(lgx)[:140])
    check("物流状态是已知枚举（可被推演推进）",
          lgd.get("status") in ("picked_up", "in_transit",
                                "out_for_delivery", "delivered"),
          str(lgd.get("status")))
    check("物流轨迹非空（至少「已揽件」）",
          isinstance(lgd.get("timeline"), list) and len(lgd["timeline"]) >= 1,
          str(lgd.get("timeline")))
    # Agent 侧走同一份推演 —— 否则「物流永远不动」的观感会回来
    convl = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    ansl = chat(t2, convl, f"订单{oid}的快递到哪了")
    check("Agent 能查到这笔新单的物流（非降级、非「未找到」）",
          FALLBACK_GREETING not in ansl["content"]
          and "未找到" not in ansl["content"]
          and any(k in ansl["content"] for k in
                  ("物流", "快递", "揽件", "运输", "派送", "签收")),
          ansl["content"][:140])

    # ── 3D. 商品检索：漏字关键词也能下单（模糊匹配） ──
    print("\n场景 3D  商品检索：漏字「X1 门锁」也能匹配到 X1 智能门锁")
    conv3d = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    ad = chat(t2, conv3d, "帮我买 X1 门锁，寄到广州市天河区体育西路1号")
    check("漏字商品名也被匹配到（不是「没有找到」）",
          "没有找到" not in ad["content"]
          and any(k in ad["content"] for k in ("X1 智能门锁", "智能门锁")),
          ad["content"][:160])
    check("给出下单草稿待确认（prepare 阶段）",
          ("确认" in ad["content"] or "回复" in ad["content"]), ad["content"][:80])

    # ── 3E. FAQ 关键词堆兜底（LLM 写坏 query，服务端多路召回兜住） ──
    print("\n场景 3E  FAQ 兜底：LLM 把问句写成关键词堆仍能召回正确条目")
    conv3e = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    a3e = chat(t2, conv3e, "它的电池能续航多久")
    tr3e = traces_of("2", conv3e)
    faq_calls = [t for it in tr3e for t in (it.get("tool_calls") or [])
                 if t.get("name") == "search_faq"]
    check("走了 faq 路由并调用了 search_faq", bool(faq_calls),
          str([it.get("route") for it in tr3e]))
    check("Mock 确实把问句压成了关键词堆（复现真实 LLM 的坏习惯）",
          any(" " in str((t.get("args") or {}).get("query", "")) for t in faq_calls),
          str([t.get("args") for t in faq_calls]))
    check("服务端兜底成功：回答含正确答案的独有内容「6 个月」",
          "6 个月" in a3e["content"], a3e["content"][:160])
    check("不是降级闲聊、也不是「没有找到」",
          FALLBACK_GREETING not in a3e["content"] and "没有找到" not in a3e["content"],
          a3e["content"][:120])

    # ── 4. 下单两阶段（human-in-the-loop） ──
    print("\n场景 4  下单两阶段：先确认草稿，再落库")
    conv4 = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    d1 = chat(t2, conv4, "我要买智能门锁1个，寄到广州市天河区体育西路1号")
    check("prepare 阶段要求确认", ("确认" in d1["content"] or "回复" in d1["content"]),
          d1["content"][:80])
    d2 = chat(t2, conv4, "确认")
    check("commit 阶段下单成功", ("订单号" in d2["content"] or "成功" in d2["content"]),
          d2["content"][:80])
    check("两次交互均非降级闲聊", FALLBACK_GREETING not in d1["content"] + d2["content"])

    # ── 4B. 跨轮指代：下单后直接问「当前状态」（省略订单号） ──
    print("\n场景 4B  跨轮上下文：下单后问「当前状态」（不重复订单号）")
    a4b = chat(t2, conv4, "当前状态")
    check("不再反问用户要订单号（自动接上最近订单）",
          not any(k in a4b["content"][:40]
                  for k in ("麻烦把订单号", "请把订单号", "把订单号发给")),
          a4b["content"][:100])
    check("给出该订单的状态类回答（非降级闲聊）",
          FALLBACK_GREETING not in a4b["content"]
          and any(k in a4b["content"] for k in ("已支付", "待支付", "状态", "订单")),
          a4b["content"][:120])

    # ── 4C. 购物车整单：加购多件 → 「结算购物车」一张单全带走 ──
    print("\n场景 4C  购物车整单：加购 2 件后由 AI 一次结算（旧版只下一件）")
    # 先清空购物车，避免受前面场景残留影响
    st, cur = api("GET", "/api/shop/cart", token=shop_tok)
    for it in ((cur.get("data") or {}).get("items") or []):
        api("DELETE", f"/api/shop/cart/{it['product_id']}", token=shop_tok)
    for pid in (1, 2):
        st, _c = api("POST", "/api/shop/cart", token=shop_tok,
                     body={"product_id": pid, "quantity": 1})
        check(f"加购商品 {pid}", st == 200, str(st))
    st, cart_before = api("GET", "/api/shop/cart", token=shop_tok)
    n_before = len(((cart_before.get("data") or {}).get("items") or []))
    check("购物车里有 2 种商品", n_before == 2, str(n_before))

    conv4c = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    c1 = chat(t2, conv4c, "帮我结算购物车，寄到广州市天河区体育西路1号")
    check("给出整单草稿待确认（走 prepare 而非单品失败）",
          ("确认" in c1["content"] or "回复" in c1["content"]), c1["content"][:120])
    check("草稿列出购物车中的多件商品",
          c1["content"].count("·") >= 2 and "¥" in c1["content"], c1["content"][:220])
    c2 = chat(t2, conv4c, "确认")
    check("整单下单成功", ("订单号" in c2["content"] or "成功" in c2["content"]),
          c2["content"][:120])
    m4c = re.search(r"订单号[：:\s]*(\d+)", c2["content"])
    oid4c = int(m4c.group(1)) if m4c else None
    check("能从回答里解析出订单号", oid4c is not None, c2["content"][:120])
    if oid4c:
        st, det = api("GET", f"/api/shop/orders/{oid4c}", token=shop_tok)
        items4c = ((det.get("data") or {}).get("items") or []) if st == 200 else []
        check("该订单含全部 2 件商品（旧版只会下 1 件）", len(items4c) == 2,
              str([i.get("product_name") for i in items4c]))
    st, cart_after = api("GET", "/api/shop/cart", token=shop_tok)
    n_after = len(((cart_after.get("data") or {}).get("items") or []))
    check("整单下单后购物车被清空", n_after == 0, str(n_after))

    # ── 5. SSE 流式（打字机数据源） ──
    print("\n场景 5  SSE 流式：token 逐字 + 事件序列")
    conv5 = api("POST", "/api/conversations", token=t2)[1]["conversation_id"]
    ev = sse("/api/chat/stream", t2, {"conversation_id": conv5, "message": "订单1001到哪了"})
    kinds = [e.get("event") for e in ev]
    check("包含 routing 事件", "routing" in kinds, str(kinds[:8]))
    toks = "".join(e.get("content", "") for e in ev if e.get("event") == "token")
    check("收到 token 增量事件", len(toks) > 0, f"tokens={len(toks)}")
    check("以 done 收尾", bool(kinds) and kinds[-1] == "done", str(kinds[-3:]))
    # 与会话历史里的最终回答比对：验证「逐字渲染」不丢字、不重复
    st, hist = api("GET", f"/api/conversations/{conv5}", token=t2)
    assistant = [m for m in hist.get("messages", []) if m.get("role") == "assistant"]
    if assistant:
        stored = assistant[-1].get("content", "")
        check("token 拼接 == 落库回答（打字机无重复/无丢字）", toks == stored,
              f"tokens_len={len(toks)} stored_len={len(stored)}")
    else:
        check("SSE 至少送出了 token", len(toks) > 0)

    # ── 6. 数据隔离（IDOR） ──
    print("\n场景 6  越权防护：他人会话 / 他人订单")
    t3 = token_for("3")
    st, _ = api("GET", f"/api/conversations/{conv1}", token=t3)
    check("跨用户读会话被拒（403/404）", st in (403, 404), str(st))
    conv6 = api("POST", "/api/conversations", token=t3)[1]["conversation_id"]
    a6 = chat(t3, conv6, "订单1001到哪了")   # 1001 属于 user 2
    check("跨用户查订单拿不到数据",
          ("未找到" in a6["content"] or "不存在" in a6["content"] or "无权" in a6["content"]),
          a6["content"][:80])

    # ── 7. traces 用户隔离 ──
    print("\n场景 7  可观测性：traces 按用户隔离")
    st, p3 = api("GET", "/api/traces", token=t3)
    check("traces 接口可用", st == 200)
    check("只看到自己的 trace",
          all(it["user_id"] == "3" for it in p3.get("items", [])),
          str(sorted({it["user_id"] for it in p3.get("items", [])})))

    # ── 8. 鉴权加固 ──
    print("\n场景 8  鉴权加固：内部接口 / 无效 JWT")

    def _raw(path: str, headers: dict):
        r = urllib.request.Request(BASE + path, method="GET")
        for k, v in headers.items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, {}

    st, _ = _raw("/internal/orders?user_id=2", {})
    check("内部接口缺 token 被拒（401/422）", st in (401, 422), str(st))
    st, _ = _raw("/internal/orders?user_id=2", {"X-Internal-Token": "wrong"})
    check("内部接口错误 token → 401", st == 401, str(st))
    st, _ = _raw("/internal/orders?user_id=2",
                 {"X-Internal-Token": "dev-internal-token"})
    check("内部接口正确 token 放行（非生产默认值）", st == 200, str(st))

    st, _ = api("GET", "/api/conversations", token="garbage.jwt.value")
    check("伪造 JWT 被拒（401）", st == 401, str(st))
    st, _ = api("GET", "/api/conversations")
    check("缺失 JWT 被拒（401/403）", st in (401, 403), str(st))

    print("\n" + "=" * 68)
    print(f"结果：{_passed}/{_passed + _failed} 通过，{_failed} 失败")
    print("=" * 68)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
