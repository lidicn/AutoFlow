// compute.js — 历史查询 4 类计算（在 NR function 节点内执行）。
// 输入：msg.payload 为 api-get-history 返回的状态数组（HA history REST 形状）。
// 状态条目假设形状：{ entity_id, state, attributes, last_changed, last_updated }
//   - 取值：attribute 给定 → entry.attributes[attribute]；否则 → entry.state
//   - 时间：用 last_changed（状态变化时刻）作为该条状态的有效起点
// 输出：把答案对象写回 msg.payload（供下游『提取/分支』读取）。
//
// 本文件可作 Node 模块 require（module.exports 守卫在 NR 中被跳过）。
// 能量统计（energy）走 statistics 而非 history，由 build_subflows.py 另配 REST 节点，
// 此处 computeEnergy 解析 statistics 桶（sum change 字段）。

function _valOf(entry, attribute) {
  if (attribute) {
    const a = entry.attributes || {};
    return (a[attribute] !== undefined) ? a[attribute] : entry.state;
  }
  return entry.state;
}

function _tsOf(entry) {
  const t = entry.last_changed || entry.last_updated || entry.lu || entry.lc;
  return t ? new Date(t).getTime() : NaN;
}

function _valOfAttr(entry, attribute) {
  return _valOf(entry, attribute);
}

// HA 的 /api/history/period 在某些版本会把单实体历史包成 [[...]]（外层数组只含
// 一个内层数组）。api-get-history 节点可能原样透传。若首元素仍是数组则拍平一层，
// 否则 compute* 会拿 wrapper 当 entry（_tsOf 返回 NaN → best=null → 静默 found=false）。
function _ensureFlat(arr) {
  if (Array.isArray(arr) && arr.length && Array.isArray(arr[0])) {
    return arr[0];
  }
  return arr;
}

// 1) 某时刻状态：as-of 语义 —— 取【目标时刻及之前】最后一次采样。
//
// ⚠️ #118 修复，勿回退为 Math.abs 最近邻：
//   状态是阶跃保持的（一次变化后一直有效到下次变化），所以「T 时刻的状态」= T 之前
//   最后一次变化后的值。旧实现按 |t-target| 取绝对最近，会选到 target【之后】的采样，
//   等于用未来状态回答过去问题。
//   实测反例（2026-08-04，climate.lumi_v3_db16_air_conditioner，目标 2026-08-03T23:12+08）：
//     采样 15:12:00Z='off'（目标前 6h，窗口边界态） / 15:34:31Z='cool'（目标后 22min）
//     旧实现 → 'cool'（错，那时空调还没开）；as-of → 'off'（对）。
//   HA /api/history/period 会在窗口起点合成一条「当时生效状态」，故只要 target 落在
//   窗口内，几乎总能找到 t<=target 的采样。
// 找不到前驱时（target 早于窗口起点）回退最近后继，并用 source='after_target' 显式标注，
// 让下游能区分「确证值」与「近似值」，而不是静默给出可疑答案。
function computeStateAt(msg, arr) {
  arr = _ensureFlat(arr);
  const target = new Date(msg._hist_target).getTime();
  const miss = () => ({
    found: false, entity: msg.entity, at_iso: msg._hist_target_iso,
    value: null, attribute: msg.attribute || null, nearest_ts: null, source: null
  });
  if (!arr || !arr.length) return miss();
  let asOf = null, asOfT = -Infinity;   // t <= target 中最晚的
  let after = null, afterT = Infinity;  // t >  target 中最早的
  for (const e of arr) {
    const t = _tsOf(e);
    if (isNaN(t)) continue;
    if (t <= target) {
      if (t > asOfT) { asOfT = t; asOf = e; }
    } else if (t < afterT) { afterT = t; after = e; }
  }
  const best = asOf || after;
  if (!best) return miss();
  return {
    found: true, entity: msg.entity, at_iso: msg._hist_target_iso,
    value: _valOf(best, msg.attribute), attribute: msg.attribute || null,
    unit: (best.attributes && best.attributes.unit_of_measurement) || null,
    nearest_ts: new Date(_tsOf(best)).toISOString(),
    // 'as_of'       = 目标时刻确实生效的状态（可信）
    // 'after_target'= 目标早于可用历史，返回的是其后最近采样（近似，下游可据此降级措辞）
    source: asOf ? "as_of" : "after_target"
  };
}

// 2) 区间内是否发生（状态变化 / 达到某态）
function computeOccurred(msg, arr) {
  arr = _ensureFlat(arr);
  const start = new Date(msg._hist_start).getTime();
  const end = new Date(msg._hist_end).getTime();
  const want = msg.state; // 目标态；undefined → 任意变化
  const attr = msg.attribute;
  let count = 0;
  const events = [];
  let prev = null;
  for (const e of arr) {
    const t = _tsOf(e);
    if (isNaN(t)) continue;
    if (t < start || t > end) { prev = { v: _valOfAttr(e, attr), t }; continue; }
    const v = _valOfAttr(e, attr);
    if (prev && prev.v !== v) {
      // ⚠️ #118 修复，勿回退加回 `prev.v === want`：
      //   指定 state 时只计「变成 want」的次数。旧实现把「离开 want」也计入，
      //   导致次数翻倍。实测（2026-08-03 空调 state='off'）旧版 count=4，
      //   明细为 cool→off / off→cool / cool→off / off→cool，真正「变成 off」只有 2 次。
      if (!want ? true : v === want) {
        count++;
        events.push({ ts: new Date(t).toISOString(), from: prev.v, to: v });
      }
    } else if (!prev) {
      // 窗口内第一条采样即处于目标态（HA 会在窗口起点合成当时生效状态）→ 记 1 次
      if (want && v === want) { count++; events.push({ ts: new Date(t).toISOString(), from: null, to: v }); }
    }
    prev = { v, t };
  }
  return {
    occurred: count > 0, entity: msg.entity,
    start_iso: msg._hist_start, end_iso: msg._hist_end,
    count, state: want || null,
    events,
    first_ts: events.length ? events[0].ts : null,
    last_ts: events.length ? events[events.length - 1].ts : null
  };
}

// 3) 区间内处于某态的累计时长
function computeDuration(msg, arr) {
  arr = _ensureFlat(arr);
  const start = new Date(msg._hist_start).getTime();
  const end = new Date(msg._hist_end).getTime();
  const want = msg.state;
  const attr = msg.attribute;
  let total = 0;
  let prev = null;
  for (const e of arr) {
    const t = _tsOf(e);
    if (isNaN(t)) continue;
    const v = _valOfAttr(e, attr);
    if (prev) {
      const segStart = Math.max(prev.t, start);
      const segEnd = Math.min(t, end);
      if (segEnd > segStart && prev.v === want) total += (segEnd - segStart);
    }
    prev = { v, t };
  }
  if (prev && prev.v === want) {
    const segStart = Math.max(prev.t, start);
    if (end > segStart) total += (end - segStart);
  }
  const sec = Math.floor(total / 1000);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const human = (h > 0 ? h + "小时" : "") + (m > 0 ? m + "分" : "") +
    (s > 0 || (h === 0 && m === 0) ? s + "秒" : "");
  return {
    total_seconds: sec, total_human: human || "0秒", entity: msg.entity,
    start_iso: msg._hist_start, end_iso: msg._hist_end, state: want,
    ratio: (end > start) ? +(total / (end - start)).toFixed(4) : 0
  };
}

// 4a) 聚合：count / mean / min / max / sum（基于 history 数组的数值属性）
function computeAggregate(msg, arr) {
  arr = _ensureFlat(arr);
  const metric = msg.metric;
  const attr = msg.attribute;
  if (metric === "count") {
    let count = 0, prev = null;
    for (const e of arr) {
      const v = _valOfAttr(e, attr);
      if (prev && prev !== v) count++;
      else if (!prev) count++;
      prev = v;
    }
    return { value: count, unit: "次", entity: msg.entity,
      start_iso: msg._hist_start, end_iso: msg._hist_end, metric, attribute: attr || null };
  }
  const nums = [];
  let unit = null;
  for (const e of arr) {
    const raw = attr ? _valOfAttr(e, attr) : e.state;
    const n = parseFloat(raw);
    if (!isNaN(n)) {
      nums.push(n);
      // 取首个可用的 unit_of_measurement（#118：旧版恒为 null，下游拿不到量纲）
      if (unit === null && e.attributes && e.attributes.unit_of_measurement) {
        unit = e.attributes.unit_of_measurement;
      }
    }
  }
  if (!nums.length) {
    return { value: null, unit: null, entity: msg.entity,
      start_iso: msg._hist_start, end_iso: msg._hist_end, metric,
      attribute: attr || null, samples: 0, error: "无数值数据" };
  }
  let value;
  if (metric === "mean") value = nums.reduce((a, b) => a + b, 0) / nums.length;
  else if (metric === "min") value = Math.min.apply(null, nums);
  else if (metric === "max") value = Math.max.apply(null, nums);
  else if (metric === "sum") value = nums.reduce((a, b) => a + b, 0);
  // ⚠️ mean 为【采样算术均值】而非时间加权均值：采样密集的时段权重更高。
  //    需要时间加权请另开 metric（当前语义保持与旧版一致，避免静默改变已有 flow 的结果）。
  return { value, unit, entity: msg.entity,
    start_iso: msg._hist_start, end_iso: msg._hist_end, metric,
    attribute: attr || null, samples: nums.length };
}

// 4b) 能量聚合：解析 statistics REST 返回的桶数组，累加每段 change（kWh）
// statistics 响应形状（HA）：{ "<statistic_id>": [ {start, end, mean, sum, change, ...}, ... ] }
function computeEnergy(msg, statObj) {
  const buckets = (statObj && typeof statObj === "object")
    ? Object.values(statObj).flat() : [];
  let kwh = 0;
  for (const b of buckets) {
    const c = (b && (b.change != null)) ? parseFloat(b.change) : NaN;
    if (!isNaN(c)) kwh += c;
  }
  return {
    value: +kwh.toFixed(3), unit: "kWh", entity: msg.entity,
    start_iso: msg._hist_start, end_iso: msg._hist_end,
    metric: "energy", attribute: msg.attribute || "energy"
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    computeStateAt, computeOccurred, computeDuration,
    computeAggregate, computeEnergy, _valOf, _tsOf, _valOfAttr
  };
}
