/**
 * 前端请求层离线验证（**只读**，不修改任何业务数据）。
 *
 * 把 static/index.html 里 <script> 的「请求 + 反馈」段落抽出来，在最小 DOM 桩上
 * 执行，再打真实后端。验证旧版的两个真实缺陷已修：
 *  1. 后端失败走 HTTPException → 响应体是 {"detail": ...}，旧版只读 d.message，
 *     所有失败原因都被吞成「下单失败」；现在必须原样透传。
 *  2. JWT 过期（默认 60 分钟）后接口全返 401，旧版所有 catch 静默 → 页面毫无提示，
 *     表现就是「购物车空了 / 结算按钮不见了 / 点了没反应」。
 *
 * 用法：node _scratch/test_frontend_req.mjs [base_url]
 */
import fs from 'node:fs';
import vm from 'node:vm';

const BASE = process.argv[2] || 'http://127.0.0.1:8000';
let PASS = 0, FAIL = 0;
function check(name, cond, extra = '') {
  if (cond) { PASS++; console.log(`  [PASS] ${name}`); }
  else { FAIL++; console.log(`  [FAIL] ${name}  ${extra}`); }
}

// ── 最小 DOM 桩：够 helpers 段定义并跑起来，不做真实渲染 ──────────────
function fakeEl() {
  return {
    style: {}, dataset: {}, classList: { add() {}, remove() {}, contains: () => false },
    value: '', textContent: '', innerHTML: '', className: '', placeholder: '',
    disabled: false, onclick: null, onkeydown: null, parentNode: null,
    appendChild() {}, remove() {}, focus() {}, select() {},
    querySelector: () => fakeEl(), querySelectorAll: () => [], closest: () => fakeEl(),
  };
}
const els = {};
const ctx = {
  console,
  setTimeout, clearTimeout,
  fetch: (p, o) => fetch(BASE + p, o),   // 页面里是相对路径，这里补 base
  document: {
    getElementById: (id) => (els[id] ||= fakeEl()),
    createElement: () => fakeEl(),
    querySelector: () => null,
    querySelectorAll: () => [],
    body: { appendChild() {} },
  },
};

// ── 从 index.html 抽出「请求 + 反馈」段落（api → withBusy） ───────────
const html = fs.readFileSync('static/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const start = script.indexOf('function api(path, opts={})');
const end = script.indexOf('// ═══════════════════════════ Auth');
if (start < 0 || end < 0) { console.log('无法定位 helpers 段'); process.exit(1); }
const helpers = script.slice(start, end)
  + '\n;globalThis.__msgOf = msgOf; globalThis.__req = req;'
  + '\n;globalThis.__setToken = (t) => { authToken = t; };'
  + '\n;globalThis.__getToken = () => authToken;';

ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext('var authToken = ""; var currentUser = null; var currentConvId = "";', ctx);
vm.runInContext(helpers, ctx);

console.log(`目标服务：${BASE}\n`);

// ── 1. 错误信息透传（旧版丢 detail，只剩「下单失败」） ────────────────
console.log('— 错误信息透传 msgOf —');
const msgOf = ctx.__msgOf;
check('FastAPI detail 被读出来（旧版只读 message → 原因丢失）',
      msgOf({ detail: '请填写收货地址' }) === '请填写收货地址');
check('业务 message 优先', msgOf({ message: '下单成功' }) === '下单成功');
check('detail 为数组（422 校验错误）时能展开',
      msgOf({ detail: [{ msg: 'field required' }] }) === 'field required',
      String(msgOf({ detail: [{ msg: 'field required' }] })));
check('空响应返回空串（不抛）', msgOf(null) === '' && msgOf({}) === '');

// ── 2. 真实接口 401：伪造 token → 必须显式提示登录过期 ────────────────
console.log('\n— 真实接口 401：登录过期要看得见 —');
ctx.__setToken('eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoyLCJleHAiOjF9.invalid-signature');
let err401 = '';
try { await ctx.__req('/api/shop/cart'); } catch (e) { err401 = e.message; }
check('401 抛出「登录已过期」（旧版静默 → 点了没反应）',
      err401.includes('登录已过期'), `msg=${err401}`);
check('401 后 token 被清空（不再让用户继续白点）',
      ctx.__getToken() === '', `token=${ctx.__getToken()}`);

// ── 3. 真实接口 422：空地址下单 → 必须给出后端真实原因（只读，不落库） ──
console.log('\n— 真实接口 422：下单失败要给出具体原因 —');
const lr = await fetch(BASE + '/api/shop/auth/login', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: 'zhangsan@shop.local', password: 'user123' }),
});
const ld = await lr.json();
const token = ld?.data?.access_token || '';
check('拿到商城 token', !!token, JSON.stringify(ld).slice(0, 120));
ctx.__setToken(token);

let err422 = '';
try {
  await ctx.__req('/api/shop/orders', { method: 'POST', body: JSON.stringify({ address: '' }) });
} catch (e) { err422 = e.message; }
check('422 抛出后端真实原因「请填写收货地址」（旧版只会说「下单失败」）',
      err422.includes('请填写收货地址'), `msg=${err422}`);

// ── 4. 正常路径未被破坏：购物车与订单列表能读 ────────────────────────
console.log('\n— 正常路径回归（只读） —');
try {
  const cart = await ctx.__req('/api/shop/cart');
  check('购物车可读（返回 items 数组）',
        Array.isArray(cart?.data?.items), JSON.stringify(cart).slice(0, 100));
} catch (e) { check('购物车可读（返回 items 数组）', false, e.message); }
try {
  const orders = await ctx.__req('/api/shop/orders');
  check('订单列表可读（返回 items 数组）',
        Array.isArray(orders?.data?.items), JSON.stringify(orders).slice(0, 100));
} catch (e) { check('订单列表可读（返回 items 数组）', false, e.message); }

console.log(`\n==================================================================`);
console.log(`结果：${PASS}/${PASS + FAIL} 通过，${FAIL} 失败`);
console.log(`==================================================================`);
process.exit(FAIL ? 1 : 0);
