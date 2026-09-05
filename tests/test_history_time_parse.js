// 离线单测：time_parse.js 的自然语言时间解析（不依赖 NR / HA）。
// 运行：node tests/test_history_time_parse.js
const assert = require("assert");
const tp = require("../src/autoflow_gateway/nr_subflows/history/time_parse.js");

// 固定"现在" = 2026-07-20 周一 20:20:00（本地时区，与运行时 TZ=Asia/Shanghai 一致）
const NOW = new Date(2026, 6, 20, 20, 20, 0, 0); // 月份 0 索引：6=7月

function eq(label, got, exp) {
  const a = got.getTime(), b = exp.getTime();
  assert.ok(Math.abs(a - b) < 1000, `${label}\n  期望 ${exp.toISOString()}\n  实得 ${got.toISOString()}`);
}

let n = 0;
function ok(label, cond) {
  assert.ok(cond, `❌ ${label}`);
  n++;
  console.log(`✅ ${label}`);
}

// ── 日期词 + 时刻 ──
eq("昨晚23:12", tp.parseNaturalTime("昨晚23:12", NOW), new Date(2026, 6, 19, 23, 12, 0));
eq("昨天11:00", tp.parseNaturalTime("昨天11:00", NOW), new Date(2026, 6, 19, 11, 0, 0));
eq("昨天11：00（全角冒号）", tp.parseNaturalTime("昨天11：00", NOW), new Date(2026, 6, 19, 11, 0, 0));
eq("前天08:30", tp.parseNaturalTime("前天08:30", NOW), new Date(2026, 6, 18, 8, 30, 0));
eq("今天09:15", tp.parseNaturalTime("今天09:15", NOW), new Date(2026, 6, 20, 9, 15, 0));
eq("明早08:00", tp.parseNaturalTime("明早08:00", NOW), new Date(2026, 6, 21, 8, 0, 0));

// 时段修饰
eq("今晚23:12", tp.parseNaturalTime("今晚23:12", NOW), new Date(2026, 6, 20, 23, 12, 0));
eq("今天下午3点(15时)", tp.parseNaturalTime("今天下午3:00", NOW), new Date(2026, 6, 20, 15, 0, 0));
eq("今晚(无时刻,兜底20:00)", tp.parseNaturalTime("今晚", NOW), new Date(2026, 6, 20, 20, 0, 0));
eq("今早(无时刻,兜底08:00)", tp.parseNaturalTime("今早", NOW), new Date(2026, 6, 20, 8, 0, 0));
eq("中午(无时刻,兜底12:00)", tp.parseNaturalTime("中午", NOW), new Date(2026, 6, 20, 12, 0, 0));

// ── 相对偏移 ──
eq("过去8小时", tp.parseNaturalTime("过去8小时", NOW), new Date(NOW.getTime() - 8 * 3600 * 1000));
eq("8小时前", tp.parseNaturalTime("8小时前", NOW), new Date(NOW.getTime() - 8 * 3600 * 1000));
eq("近3天", tp.parseNaturalTime("近3天", NOW), new Date(NOW.getTime() - 3 * 86400000));
eq("2天前", tp.parseNaturalTime("2天前", NOW), new Date(NOW.getTime() - 2 * 86400000));
eq("30分钟前", tp.parseNaturalTime("30分钟前", NOW), new Date(NOW.getTime() - 30 * 60000));
eq("8h(紧凑)", tp.parseNaturalTime("8h", NOW), new Date(NOW.getTime() - 8 * 3600 * 1000));
eq("1d(紧凑)", tp.parseNaturalTime("1d", NOW), new Date(NOW.getTime() - 86400000));
eq("30min(紧凑)", tp.parseNaturalTime("30min", NOW), new Date(NOW.getTime() - 30 * 60000));

// ── 现在 ──
ok("现在≈NOW", Math.abs(tp.parseNaturalTime("现在", NOW).getTime() - NOW.getTime()) < 2);
ok("now≈NOW", Math.abs(tp.parseNaturalTime("now", NOW).getTime() - NOW.getTime()) < 2);

// ── 本周 ──
const mon = tp.parseNaturalTime("本周", NOW);
ok("本周=周一", mon.getDay() === 1);
ok("本周=00:00", mon.getHours() === 0 && mon.getMinutes() === 0 && mon.getSeconds() === 0);
const sun = tp.parseNaturalTime("本周日", NOW);
ok("本周日=周日", sun.getDay() === 0);
ok("本周日=23:59:59", sun.getHours() === 23 && sun.getMinutes() === 59 && sun.getSeconds() === 59);

// ── 绝对 ISO ──
eq("ISO带时刻", tp.parseNaturalTime("2026-07-19T23:12", NOW), new Date(2026, 6, 19, 23, 12, 0));
eq("ISO空格分隔", tp.parseNaturalTime("2026-07-19 23:12", NOW), new Date(2026, 6, 19, 23, 12, 0));
eq("ISO仅日期", tp.parseNaturalTime("2026-07-19", NOW), new Date(2026, 6, 19, 0, 0, 0));

// ── 仅 HH:MM → 今天 ──
eq("08:30→今天08:30", tp.parseNaturalTime("08:30", NOW), new Date(2026, 6, 20, 8, 30, 0));

// ── toHAISO 带偏移 ──
const iso = tp.toHAISO(new Date(2026, 6, 19, 23, 12, 0));
ok("toHAISO 格式", /^2026-07-19T23:12:00[+-]\d{2}:\d{2}$/.test(iso));
console.log(`   toHAISO(昨晚23:12) = ${iso}`);

// ── parseWindow ──
const w = tp.parseWindow("昨天11:00", "昨天12:00", NOW);
ok("parseWindow start", w.start.getTime() === new Date(2026, 6, 19, 11, 0, 0).getTime());
ok("parseWindow end", w.end.getTime() === new Date(2026, 6, 19, 12, 0, 0).getTime());
ok("parseWindow 含 iso", typeof w.start_iso === "string" && typeof w.end_iso === "string");
const w2 = tp.parseWindow("过去8小时", null, NOW);
ok("parseWindow end 缺省=now", Math.abs(w2.end.getTime() - NOW.getTime()) < 2);

// ── 无法识别 → null ──
ok("乱码→null", tp.parseNaturalTime("foobar", NOW) === null);
ok("空→null", tp.parseNaturalTime("", NOW) === null);

console.log(`\n✅ 全部 ${n} 项基础断言通过（+ 上述日期/偏移断言）`);
