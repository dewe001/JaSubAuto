/**
 * 手动页面的回归测试：node tests/test_ui.mjs
 *
 * 为什么需要它：页面是拼字符串生成的 HTML，出过一次
 *   onclick="browseDir(${JSON.stringify(path)})"
 * ——JSON.stringify 吐双引号，塞进双引号属性里属性被提前截断，整行渲染成垃圾，
 * 而这种错误在 Python 侧一个都测不出来。所以这里真的把 ui.html 的脚本跑一遍，
 * 检查生成的 HTML 里属性值有没有被截断、点击后有没有把路径填对。
 *
 * 没有 jsdom，用一个刚好够跑这段脚本的 DOM 桩。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const UI = join(HERE, "..", "plugins.v2", "jimakutrigger", "core", "ui.html");

// ---------- 最小 DOM 桩 ----------

class El {
  constructor(id = "") {
    this.id = id;
    this.innerHTML = "";
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.open = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  scrollIntoView() {}
}

function makeDom() {
  const els = new Map();
  const listeners = {};
  const doc = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, new El(id));
      return els.get(id);
    },
    querySelector() { return new El(); },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  };
  return { doc, els, listeners };
}

// ---------- 假数据与假 fetch ----------

const SHOWS = [
  // 故意带单引号和括号：真实剧名就长这样（Frieren: Beyond Journey's End）
  { name: "葬送的芙莉莲 (2023)", path: "/媒体/日番/葬送的芙莉莲 (2023)",
    title: "葬送的芙莉莲", year: "2023", tmdb_id: 209867,
    video_count: 4, ja_count: 1, missing: 3, subdir_count: 1, note: "" },
  { name: "Frieren's Journey (2024)", path: "/媒体/日番/Frieren's Journey (2024)",
    title: "", year: "", tmdb_id: null,
    video_count: 0, ja_count: 0, missing: 0, subdir_count: 1,
    note: "没找到 nfo，tmdb_id 需要手动填" },
];

const CALLS = [];      // 记录每次请求，用来断言"确认写入"真的发了 dry_run=false

function fakeFetch(url, opts) {
  const u = new URL(url, "http://x");
  const sent = opts && opts.body ? JSON.parse(opts.body) : null;
  CALLS.push({ path: u.pathname, sent });
  let body;
  if (u.pathname.endsWith("/candidates")) {
    return Promise.resolve({ json: () => Promise.resolve({
      entries: [{ id: 729, name: "Sousou no Frieren", japanese_name: "葬送のフリーレン" }],
      entry_id: 729, file_count: 3, reason: "有 3 个同分候选",
      chosen: null,
      candidates: [{ name: "[A] Show - 12.srt", url: "https://x/1", size: 1024,
                     last_modified: "2026-01-01", lang: "unknown", lang_label: "无语言标记" }] }) });
  }
  if (u.pathname.endsWith("/scan")) {
    const dry = sent.dry_run;
    // 视频路径故意用 Windows 分隔符，验证 basename 两种分隔符都能切
    const ep = {
      video: String.raw`D:\媒体\日番\葬送的芙莉莲 (2023)\Season 1\葬送的芙莉莲 - S01E36 - 第 36 集.mkv`,
      season: 1, library_episode: 36, anilist_id: 182255, anilist_episode: 8,
      status: dry ? "dry_run" : "ok",
      picked: "葬送のフリーレン.S02E08.第36話 立派な最期.WEBRip.Amazon.ja-jp[sdh].srt",
      lang: "纯日语·含听障注释",
      target: String.raw`D:\媒体\日番\葬送的芙莉莲 (2023)\Season 1\葬送的芙莉莲 - S01E36 - 第 36 集.ja.srt`,
      reason: dry ? "dry-run：仅打印，未写盘" : "已写入",
    };
    // 再来一集"自动挑不出来"的，用于测试转手动的入口
    const review = {
      video: "/媒体/日番/某剧/Season 1/某剧 - S01E12 - 第 12 集.mkv",
      season: 1, library_episode: 12, anilist_id: 154587, anilist_episode: 12,
      status: "needs_review", picked: "", lang: "", target: "",
      reason: "有 3 个同分候选，无法自动判定，需人工选择",
    };
    return Promise.resolve({ json: () => Promise.resolve({
      root: "/媒体/日番/葬送的芙莉莲 (2023)", dry_run: dry, total_videos: 2, note: "",
      summary: dry ? { dry_run: 1, needs_review: 1 } : { ok: 1, needs_review: 1 },
      episodes: [ep, review] }) });
  }
  if (u.pathname.endsWith("/health")) {
    body = { ok: true, jimaku_token_configured: true, lang_suffix: "ja", preferred_sources: [] };
  } else if (u.pathname.endsWith("/library")) {
    const q = (u.searchParams.get("q") || "").trim();
    body = q
      ? { roots: [], total: 2, entries: SHOWS.filter(s => s.name.toLowerCase().includes(q.toLowerCase())),
          note: `「${q}」匹配到 N 部` }
      : { roots: [], total: 2, entries: [], note: "番剧库里共 2 部，输入剧名关键字查找（中文名即可）" };
  } else {
    body = {};
  }
  return Promise.resolve({ json: () => Promise.resolve(body) });
}

// ---------- 跑脚本 ----------

const html = readFileSync(UI, "utf8").replace("__API_BASE__", "/api").replace("__BUILD__", "test");
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

const { doc, els, listeners } = makeDom();
const ctx = {
  document: doc, fetch: fakeFetch, location: { search: "" },
  setTimeout, clearTimeout, URLSearchParams, alert() {}, console,
};
// 用 Function 而不是 eval，脚本里的顶层 const 才不会污染这里。
// 末尾 return runScan 是为了让测试能直接触发扫描——页面上那个按钮是静态 onclick，
// 走不了事件委托，测试里没法点。
const ctxRunScan = new Function(
  ...Object.keys(ctx), script + ";\nreturn runScan;")(...Object.values(ctx));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
// 输入框有 300ms 防抖，等久一点
const settle = (ms = 400) => new Promise(r => setTimeout(r, ms));

// ---------- 断言 ----------

/** 抽出某个属性的值，遇到第一个引号就停——正是浏览器解析属性的方式。
 *  属性值被提前截断时，这里拿到的就不是完整路径，测试因此能抓到那个 bug。 */
function attrValues(htmlStr, attr) {
  return [...htmlStr.matchAll(new RegExp(`${attr}="([^"]*)"`, "g"))].map(m => unesc(m[1]));
}
const unesc = s => s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
                    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");

test("进页面不铺内容，只报总数", async () => {
  await settle();
  const out = doc.getElementById("browseOut").innerHTML;
  assert.match(out, /番剧库里共 2 部/);
  assert.equal(out.includes("选它"), false, "空关键字时不应列出任何剧");
});

test("输入关键字后列出候选，且每行都有「选它」按钮", async () => {
  doc.getElementById("showQ").value = "芙莉莲";
  listeners.input.forEach(fn => fn({ target: { id: "showQ" } }));
  await settle();
  const out = doc.getElementById("browseOut").innerHTML;
  assert.match(out, /葬送的芙莉莲/);
  assert.equal((out.match(/data-act="pick"/g) || []).length, 1, "应有且仅有 1 个「选它」按钮");
  assert.match(out, /tmdb=209867/);
  assert.match(out, /缺 3 集/);
});

test("路径属性没有被引号截断（今天那个 bug 的回归测试）", async () => {
  doc.getElementById("showQ").value = "Frieren";
  listeners.input.forEach(fn => fn({ target: { id: "showQ" } }));
  await settle();
  const out = doc.getElementById("browseOut").innerHTML;
  const paths = attrValues(out, "data-path");
  const want = "/媒体/日番/Frieren's Journey (2024)";
  // 每行两处 data-path：剧名链接 + 「选它」按钮。少一处说明有一个标签写坏了
  assert.equal(paths.length, 2, `每行应有 2 处 data-path，实际 ${paths.length}：${out}`);
  assert.ok(paths.every(p => p === want), `路径被截断或转义错了：${JSON.stringify(paths)}`);
});

test("没有 tmdb_id 的剧同样可选（不能因为缺统计/缺 id 就藏按钮）", async () => {
  const out = doc.getElementById("browseOut").innerHTML;
  assert.match(out, /data-act="pick"/);
  assert.match(out, /data-tmdb=""/, "没有 tmdb_id 时应是空串而不是漏掉按钮");
});

test("点「选它」把路径填进批量补扫的输入框", async () => {
  const el = {
    dataset: { act: "pick" },
    getAttribute: k => ({ "data-path": "/媒体/日番/葬送的芙莉莲 (2023)",
                          "data-tmdb": "209867" }[k]),
    closest: () => el,
  };
  listeners.click.forEach(fn => fn({ target: el, preventDefault() {} }));
  assert.equal(doc.getElementById("scanDir").value, "/媒体/日番/葬送的芙莉莲 (2023)");
  assert.equal(doc.getElementById("scanTmdb").value, 209867);
});

test("试运行结果里显示将写入的字幕文件名，且只取文件名不带路径", async () => {
  doc.getElementById("scanDir").value = "/媒体/日番/葬送的芙莉莲 (2023)";
  doc.getElementById("scanTmdb").value = "209867";
  doc.getElementById("scanDry").checked = true;
  await runScanFromPage();
  const out = doc.getElementById("scanOut").innerHTML;
  assert.match(out, /将写入/);
  assert.match(out, /葬送的芙莉莲 - S01E36 - 第 36 集\.ja\.srt/);
  assert.equal(out.includes("D:"), false, "只该显示文件名，不该带整条路径");
});

test("试运行后给出「确认写入」按钮，点了才真写", async () => {
  const out = doc.getElementById("scanOut").innerHTML;
  assert.match(out, /data-act="commit"/, "试运行后必须有确认写入的入口");
  assert.match(out, /确认写入这 1 集字幕/);

  CALLS.length = 0;
  const btn = { dataset: { act: "commit" }, getAttribute: () => null, closest: () => btn };
  listeners.click.forEach(fn => fn({ target: btn, preventDefault() {} }));
  await settle(50);
  const scan = CALLS.find(c => c.path.endsWith("/scan"));
  assert.ok(scan, "点确认后应重新请求 /scan");
  assert.equal(scan.sent.dry_run, false, "确认写入必须发 dry_run=false");
  assert.match(doc.getElementById("scanOut").innerHTML, /已写入 1 集/);
});

test("「待人工」的集给出「手动挑」入口", async () => {
  const out = doc.getElementById("scanOut").innerHTML;
  assert.match(out, /⚠ 待人工/);
  assert.match(out, /data-act="manual"/, "自动挑不出来的集必须能转手动，否则用户走不下去");
});

test("点「手动挑」填好参数并自动查候选", async () => {
  const el = {
    dataset: { act: "manual" },
    getAttribute: k => ({ "data-path": "/媒体/日番/某剧/Season 1/某剧 - S01E12 - 第 12 集.mkv",
                          "data-libep": "12", "data-anilist": "154587", "data-ep": "12" }[k]),
    closest: () => el,
  };
  CALLS.length = 0;
  listeners.click.forEach(fn => fn({ target: el, preventDefault() {} }));
  await settle(50);
  assert.equal(doc.getElementById("videoPath").value,
               "/媒体/日番/某剧/Season 1/某剧 - S01E12 - 第 12 集.mkv");
  assert.equal(doc.getElementById("anilistId").value, "154587");
  assert.equal(doc.getElementById("manualBox").open, true, "手动区应自动展开");
  assert.ok(CALLS.some(c => c.path.endsWith("/candidates")), "应自动查候选，不用手点");
  assert.match(doc.getElementById("cands").innerHTML, /data-act="dl"/);
});

test("已删除「找剧」「查候选字幕」两个独立模块", () => {
  assert.equal(/id="tabTitle"|id="paneTmdb"|function switchTab/.test(html), false,
    "按标题搜索/按 TMDB 解析的模块应已移除");
  assert.equal(/<h2>2 · 查候选字幕<\/h2>/.test(html), false);
});

test("页面里不再有拼接动态数据的内联 onclick", () => {
  const bad = [...script.matchAll(/onclick=["'][^"']*\$\{/g)];
  assert.equal(bad.length, 0,
    `内联 onclick 里不许拼模板变量（路径/文件名带引号就会炸）：${bad.map(b => b[0])}`);
});

/** 页面里的 runScan 是脚本内部函数，测试通过点击「开始扫描」触发不了（那是静态 onclick），
 *  所以直接复用点击委托：先跑一次试运行，再验证确认按钮。 */
async function runScanFromPage() {
  ctxRunScan();          // 不传参 = 用页面上「试运行」复选框的值
  await settle(50);
}

// ---------- 跑 ----------

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (err) {
    failed++;
    console.log(`  FAIL  ${name}\n        ${err.message}`);
  }
}
console.log(failed ? `\n${failed} 个失败` : `\n全部 ${tests.length} 项通过`);
process.exit(failed ? 1 : 0);
