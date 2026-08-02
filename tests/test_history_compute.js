// 离线单测：compute.js 的 4 类计算（用合成历史数据，不依赖 HA / NR）。
// 运行：node tests/test_history_compute.js
const assert = require("assert");
const C = require("../src/autoflow_gateway/nr_subflows/history/compute.js");

// 合成历史数组（门：开/关；空调：温度数值；能量：statistics 桶）
function E(state, attr, iso) {
  const a = {};
  if (attr !== undefined) a.temperature = attr;
  return { entity_id: "x", state, attributes: a, last_changed: iso, last_updated: iso };
}

// ── state_at：最接近目标点 ──
{
  const arr = [
    E("off", undefined, "2026-07-19T23:00:00+08:00"),
    E("on", undefined, "2026-07-19T23:10:00+08:00"),
    E("off", undefined, "2026-07-19T23:20:00+08:00"),
  ];
  const msg = { entity: "x", _hist_target: "2026-07-19T23:12:00+08:00", _hist_target_iso: "2026-07-19T23:12:00+08:00", attribute: null };
  const r = C.computeStateAt(msg, arr);
  assert.strictEqual(r.found, true);
  assert.strictEqual(r.value, "on"); // 23:10 最接近 23:12
  console.log("✅ state_at 取最近邻 =", r.value);

  const miss = C.computeStateAt(msg, []);
  assert.strictEqual(miss.found, false);
  console.log("✅ state_at 空数组 → found=false");
}

// ── occurred：区间内是否发生 ──
{
  const arr = [
    E("off", undefined, "2026-07-19T11:00:00+08:00"),
    E("on", undefined, "2026-07-19T11:30:00+08:00"),   // 开
    E("off", undefined, "2026-07-19T12:30:00+08:00"),  // 关
  ];
  const msg = { entity: "x", _hist_start: "2026-07-19T11:00:00+08:00", _hist_end: "2026-07-19T12:00:00+08:00", state: "on", attribute: null };
  const r = C.computeOccurred(msg, arr);
  assert.strictEqual(r.occurred, true);
  assert.strictEqual(r.count, 1);
  console.log("✅ occurred 区间内开门 → occurred=true count=1");

  const msgAny = { entity: "x", _hist_start: "2026-07-19T11:00:00+08:00", _hist_end: "2026-07-19T23:00:00+08:00", state: undefined, attribute: null };
  const r2 = C.computeOccurred(msgAny, arr);
  assert.strictEqual(r2.occurred, true); // 有任何变化即 true
  console.log("✅ occurred 任意变化(无目标态) → occurred=true");

  const msgNone = { entity: "x", _hist_start: "2026-07-19T11:00:00+08:00", _hist_end: "2026-07-19T23:00:00+08:00", state: "unreachable_state", attribute: null };
  const r3 = C.computeOccurred(msgNone, arr);
  assert.strictEqual(r3.occurred, false);
  console.log("✅ occurred 目标态未出现 → occurred=false");
}

// ── duration：处于某态累计时长 ──
{
  // 11:00 off, 11:10 on (开10分钟), 11:40 off (再开30分钟? 不), 12:10 on (开到窗口末 12:30 = 20分钟)
  const arr = [
    E("off", undefined, "2026-07-19T11:00:00+08:00"),
    E("on", undefined, "2026-07-19T11:10:00+08:00"),
    E("off", undefined, "2026-07-19T11:40:00+08:00"),
    E("on", undefined, "2026-07-19T12:10:00+08:00"),
  ];
  const msg = { entity: "x", _hist_start: "2026-07-19T11:00:00+08:00", _hist_end: "2026-07-19T12:30:00+08:00", state: "on", attribute: null };
  const r = C.computeDuration(msg, arr);
  // 11:10-11:40 = 30min; 12:10-12:30 = 20min → 共 50min = 3000s
  assert.strictEqual(r.total_seconds, 3000, `期望 3000s 实得 ${r.total_seconds}`);
  assert.ok(r.total_human.includes("50分"), `human=${r.total_human}`);
  console.log("✅ duration 开累计 =", r.total_human, `( ${r.total_seconds}s )`);
}

// ── aggregate：mean/min/max/sum/count ──
{
  const arr = [
    E("20", 20, "2026-07-19T23:00:00+08:00"),
    E("22", 22, "2026-07-19T23:30:00+08:00"),
    E("24", 24, "2026-07-20T00:00:00+08:00"),
  ];
  const mMean = { entity: "x", _hist_start: "s", _hist_end: "e", metric: "mean", attribute: "temperature" };
  assert.strictEqual(C.computeAggregate(mMean, arr).value, 22);
  const mMin = { entity: "x", _hist_start: "s", _hist_end: "e", metric: "min", attribute: "temperature" };
  assert.strictEqual(C.computeAggregate(mMin, arr).value, 20);
  const mMax = { entity: "x", _hist_start: "s", _hist_end: "e", metric: "max", attribute: "temperature" };
  assert.strictEqual(C.computeAggregate(mMax, arr).value, 24);
  const mSum = { entity: "x", _hist_start: "s", _hist_end: "e", metric: "sum", attribute: "temperature" };
  assert.strictEqual(C.computeAggregate(mSum, arr).value, 66);
  // count 不依赖 attribute，统计 state 变化次数
  const mCount = { entity: "x", _hist_start: "s", _hist_end: "e", metric: "count", attribute: null };
  const rc = C.computeAggregate(mCount, arr);
  assert.strictEqual(rc.value, 3, `count 期望3 实得 ${rc.value}`);
  console.log("✅ aggregate mean=22 min=20 max=24 sum=66 count=3");
}

// ── energy：累加 statistics 桶 change ──
{
  const stat = {
    "sensor.ac_energy": [
      { start: "s1", end: "e1", mean: 0.5, sum: 1.5, change: 0.5 },
      { start: "s2", end: "e2", mean: 0.3, sum: 1.8, change: 0.8 },
    ]
  };
  const msg = { entity: "sensor.ac_energy", _hist_start: "s", _hist_end: "e", metric: "energy", attribute: "energy" };
  const r = C.computeEnergy(msg, stat);
  assert.strictEqual(r.value, 1.3, `energy 期望1.3 实得 ${r.value}`);
  assert.strictEqual(r.unit, "kWh");
  console.log("✅ energy 累加 change =", r.value, r.unit);
}

// ── 嵌套数组回归（#626 / WB22 HIST-FETCH 健壮性）──
// HA /api/history/period 某些版本把单实体历史包成 [[...]]；api-get-history 可能原样透传。
// 若未拍平，首元素是 wrapper 数组 → _tsOf 返回 NaN → best=null → 静默 found=false（有数据却取不到）。
{
  // state_at：把同一份 arr 包一层 [[...]]
  const arr = [
    E("off", undefined, "2026-07-19T23:00:00+08:00"),
    E("on", undefined, "2026-07-19T23:10:00+08:00"),
    E("off", undefined, "2026-07-19T23:20:00+08:00"),
  ];
  const wrapped = [arr];
  const msg = { entity: "x", _hist_target: "2026-07-19T23:12:00+08:00", _hist_target_iso: "2026-07-19T23:12:00+08:00", attribute: null };
  const r = C.computeStateAt(msg, wrapped);
  assert.strictEqual(r.found, true, "嵌套 [[...]] 应被拍平并正确取到 found=true");
  assert.strictEqual(r.value, "on", "嵌套 [[...]] 应取最近邻 = on");
  console.log("✅ nested [[...]] state_at → found=true value=on");

  // aggregate mean：嵌套也应正确
  const arr2 = [
    E("20", 20, "2026-07-19T23:00:00+08:00"),
    E("22", 22, "2026-07-19T23:30:00+08:00"),
    E("24", 24, "2026-07-20T00:00:00+08:00"),
  ];
  const mMean = { entity: "x", _hist_start: "s", _hist_end: "e", metric: "mean", attribute: "temperature" };
  const rMean = C.computeAggregate(mMean, [arr2]);
  assert.strictEqual(rMean.value, 22, `嵌套 mean 期望22 实得 ${rMean.value}`);
  console.log("✅ nested [[...]] aggregate mean=22");
}

console.log("\n✅ compute.js 离线自测全部通过");
