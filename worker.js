/**
 * NSE Price Proxy — Cloudflare Worker (NSE ONLY, never BSE)
 *
 * Three endpoints:
 *   /backtest?start=YYYY-MM-DD&end=YYYY-MM-DD&symbols=A,B,C
 *       -> full OHLC (open, high, low, close) per symbol per day, EQ series
 *          only, from the securities bhavcopy. Widened from close-only.
 *   /indices?start=YYYY-MM-DD&end=YYYY-MM-DD&indices=NIFTY 50,NIFTY BANK,...
 *       -> daily close for NSE indices (broad + sectoral), from the
 *          separate indices bhavcopy file.
 *   /symbols[?universe=all]
 *       -> without ?universe=all: current Nifty 500 constituent list (as
 *          before). With ?universe=all: EVERY EQ-series symbol seen in the
 *          most recent securities bhavcopy — i.e. the full NSE equity
 *          universe (~2000+ symbols, not just Nifty 500), which is what
 *          lets stocks like WALCHANNAG (not in Nifty 500) be tracked.
 *
 * Live single-symbol quote endpoint (?symbols=...) unchanged from before.
 */

const ARCH = "https://nsearchives.nseindia.com";
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
const H = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
  "Accept": "text/csv, text/plain, */*",
  "Referer": "https://www.nseindia.com/",
};

function ddmmyyyy(date) {
  const p = (n) => String(n).padStart(2, "0");
  return p(date.getDate()) + p(date.getMonth() + 1) + date.getFullYear();
}

// ── Securities (equity) bhavcopy — now returns full OHLC ──
async function fetchSecBhav(date) {
  const url = `${ARCH}/products/content/sec_bhavdata_full_${ddmmyyyy(date)}.csv`;
  try {
    const r = await fetch(url, { headers: H });
    if (!r.ok) return null;
    const t = await r.text();
    if (!t || !t.includes("LAST_PRICE")) return null;
    return t;
  } catch { return null; }
}

function parseSecCSV(text, wanted) {
  const lines = text.split("\n");
  const header = lines[0].split(",").map(h => h.trim());
  const idx = (name) => header.indexOf(name);
  const symI = idx("SYMBOL"), serI = idx("SERIES");
  const openI = idx("OPEN_PRICE"), highI = idx("HIGH_PRICE"), lowI = idx("LOW_PRICE");
  const closeI = idx("CLOSE_PRICE"), lastI = idx("LAST_PRICE");
  const out = {};
  for (let i = 1; i < lines.length; i++) {
    const row = lines[i].split(",");
    if (row.length <= closeI) continue;
    if ((row[serI] || "").trim() !== "EQ") continue;
    const sym = (row[symI] || "").trim();
    if (wanted && !wanted.has(sym)) continue;
    const num = (v) => { const n = parseFloat((v||"").replace(/,/g,"")); return isFinite(n) ? n : null; };
    const o = num(row[openI]), h = num(row[highI]), l = num(row[lowI]);
    const c = num(row[closeI]) || num(row[lastI]);
    if (sym && c > 0) out[sym] = { open: o, high: h, low: l, close: c };
  }
  return out;
}

// ── Indices bhavcopy — separate NSE file, daily close (+ OHLC where present) for indices ──
async function fetchIndexBhav(date) {
  // NSE's daily indices close file. Path confirmed against NSE's archives
  // naming convention (content/indices/ind_close_all_DDMMYYYY.csv).
  const url = `${ARCH}/content/indices/ind_close_all_${ddmmyyyy(date)}.csv`;
  try {
    const r = await fetch(url, { headers: H });
    if (!r.ok) return null;
    const t = await r.text();
    if (!t || t.length < 50) return null;
    return t;
  } catch { return null; }
}

function parseIndexCSV(text, wanted) {
  const lines = text.split("\n").filter(l => l.trim());
  if (lines.length < 2) return {};
  const header = lines[0].split(",").map(h => h.trim().replace(/"/g, ""));
  const idx = (name) => header.findIndex(h => h.toUpperCase() === name.toUpperCase());
  const nameI = idx("Index Name");
  const closeI = idx("Closing Index Value");
  const openI = idx("Open Index Value");
  const highI = idx("High Index Value");
  const lowI = idx("Low Index Value");
  const out = {};
  for (let i = 1; i < lines.length; i++) {
    const row = lines[i].split(",").map(c => c.trim().replace(/"/g, ""));
    if (row.length <= closeI) continue;
    const name = row[nameI];
    if (wanted && !wanted.has(name)) continue;
    const num = (v) => { const n = parseFloat((v||"").replace(/,/g,"")); return isFinite(n) ? n : null; };
    const c = num(row[closeI]);
    if (name && c) {
      out[name] = { open: num(row[openI]), high: num(row[highI]), low: num(row[lowI]), close: c };
    }
  }
  return out;
}

async function fetchLatestBhav(fetchFn) {
  const now = new Date();
  for (let back = 0; back <= 5; back++) {
    const d = new Date(now.getTime() - back * 86400000);
    const res = await fetchFn(d);
    if (res) return { text: res, date: ddmmyyyy(d) };
  }
  return null;
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);

    // ── /symbols — Nifty 500 list, or full EQ universe with ?universe=all ──
    if (url.pathname === "/symbols") {
      const wantAll = url.searchParams.get("universe") === "all";
      const isDebug = url.searchParams.get("debug") === "1";
      if (wantAll) {
        const debugLog = isDebug ? [] : null;
        let bhav = null;
        const now = new Date();
        for (let back = 0; back <= 5; back++) {
          const d = new Date(now.getTime() - back * 86400000);
          try {
            const testUrl = `${ARCH}/products/content/sec_bhavdata_full_${ddmmyyyy(d)}.csv`;
            const r = await fetch(testUrl, { headers: H });
            if (debugLog) debugLog.push(`${ddmmyyyy(d)}: HTTP ${r.status}`);
            if (r.ok) {
              const t = await r.text();
              if (t && t.includes("LAST_PRICE")) {
                bhav = { text: t, date: ddmmyyyy(d) };
                break;
              } else if (debugLog) {
                debugLog.push(`${ddmmyyyy(d)}: 200 but missing LAST_PRICE marker (len=${t.length})`);
              }
            }
          } catch (e) {
            if (debugLog) debugLog.push(`${ddmmyyyy(d)}: fetch error ${e}`);
          }
        }
        if (!bhav) {
          const body = { error: "Could not fetch bhavcopy for full universe (tried last 6 days)" };
          if (isDebug) body.debug = debugLog;
          return new Response(JSON.stringify(body),
            { headers: { ...CORS, "Content-Type": "application/json" } });
        }
        const all = parseSecCSV(bhav.text, null);
        const symbols = Object.keys(all).sort();
        const body = { count: symbols.length, as_of: bhav.date, symbols };
        if (isDebug) body.debug = debugLog;
        return new Response(JSON.stringify(body),
          { headers: { ...CORS, "Content-Type": "application/json" } });
      }
      // Nifty 500 constituent list (unchanged behavior)
      try {
        const r = await fetch(`${ARCH}/content/indices/ind_nifty500list.csv`, { headers: H });
        if (!r.ok) return new Response(JSON.stringify({ error: `HTTP ${r.status}` }), { headers: { ...CORS, "Content-Type": "application/json" } });
        const text = await r.text();
        const lines = text.split("\n").map(l => l.trim()).filter(Boolean);
        const header = lines[0].split(",").map(h => h.trim());
        const symIdx = header.findIndex(h => h.toLowerCase().includes("symbol"));
        const symbols = lines.slice(1).map(l => l.split(",")[symIdx]).filter(Boolean).map(s => s.trim().replace(/"/g, ""));
        return new Response(JSON.stringify({ count: symbols.length, symbols }), { headers: { ...CORS, "Content-Type": "application/json" } });
      } catch (e) {
        return new Response(JSON.stringify({ error: String(e) }), { headers: { ...CORS, "Content-Type": "application/json" } });
      }
    }

    // ── /sectors — constituent lists for each sectoral index, so we can
    // map a stock to its sector for relative-strength comparisons. NSE
    // publishes these as the same style of CSV as the Nifty 500 list, kept
    // current automatically at each index rebalance (no hardcoded list to
    // go stale).
    if (url.pathname === "/sectors") {
      const SECTOR_FILES = {
        "Nifty Bank": "ind_niftybanklist.csv",
        "Nifty IT": "ind_niftyitlist.csv",
        "Nifty Auto": "ind_niftyautolist.csv",
        "Nifty Pharma": "ind_niftypharmalist.csv",
        "Nifty FMCG": "ind_niftyfmcglist.csv",
        "Nifty Metal": "ind_niftymetallist.csv",
        "Nifty Realty": "ind_niftyrealtylist.csv",
        "Nifty Energy": "ind_niftyenergylist.csv",
        "Nifty Media": "ind_niftymedialist.csv",
        "Nifty PSU Bank": "ind_niftypsubanklist.csv",
        "Nifty Private Bank": "ind_niftyprivatebanklist.csv",
        "Nifty Financial Services": "ind_niftyfinancelist.csv",
        "Nifty Healthcare Index": "ind_niftyhealthcarelist.csv",
        "Nifty Consumer Durables": "ind_niftyconsumerdurableslist.csv",
        "Nifty Oil & Gas": "ind_niftyoilgaslist.csv",
      };
      const isDebug = url.searchParams.get("debug") === "1";
      const debugLog = isDebug ? [] : null;
      const sectorMap = {};
      for (const [name, filename] of Object.entries(SECTOR_FILES)) {
        try {
          const r = await fetch(`${ARCH}/content/indices/${filename}`, { headers: H });
          if (debugLog) debugLog.push(`${name}: HTTP ${r.status}`);
          if (!r.ok) continue;
          const text = await r.text();
          const lines = text.split("\n").map(l => l.trim()).filter(Boolean);
          if (lines.length < 2) continue;
          const header = lines[0].split(",").map(h => h.trim());
          const symIdx = header.findIndex(h => h.toLowerCase().includes("symbol"));
          if (symIdx < 0) continue;
          const symbols = lines.slice(1).map(l => l.split(",")[symIdx]).filter(Boolean).map(s => s.trim().replace(/"/g, ""));
          sectorMap[name] = symbols;
        } catch (e) {
          if (debugLog) debugLog.push(`${name}: error ${e}`);
        }
      }
      const body = { sectors: sectorMap };
      if (isDebug) body.debug = debugLog;
      return new Response(JSON.stringify(body), { headers: { ...CORS, "Content-Type": "application/json" } });
    }

    // ── /indices — daily OHLC for NSE indices over a date range ──
    if (url.pathname === "/indices") {
      const startParam = url.searchParams.get("start");
      const endParam = url.searchParams.get("end");
      if (!startParam || !endParam) {
        return new Response(JSON.stringify({ error: "start and end (YYYY-MM-DD) required" }), { headers: { ...CORS, "Content-Type": "application/json" } });
      }
      const startDate = new Date(startParam + "T00:00:00Z");
      const endDate = new Date(endParam + "T00:00:00Z");
      const calendarDays = Math.round((endDate - startDate) / 86400000);
      if (calendarDays > 46) {
        return new Response(JSON.stringify({ error: `Range too large (${calendarDays} days). Keep ≤46 days.` }), { headers: { ...CORS, "Content-Type": "application/json" } });
      }
      const indicesParam = url.searchParams.get("indices") || "";
      const wanted = indicesParam ? new Set(indicesParam.split(",").map(s => s.trim())) : null;

      const dailyValues = {}; // { indexName: [{date, open, high, low, close}, ...] }
      let daysFetched = 0, daysFailed = 0;
      for (let d = 0; d <= calendarDays; d++) {
        const date = new Date(startDate.getTime() + d * 86400000);
        if (date.getDay() === 0 || date.getDay() === 6) continue;
        const text = await fetchIndexBhav(date);
        if (!text) { daysFailed++; continue; }
        daysFetched++;
        const dayData = parseIndexCSV(text, wanted);
        const dateStr = date.toISOString().slice(0, 10);
        for (const [name, bar] of Object.entries(dayData)) {
          if (!dailyValues[name]) dailyValues[name] = [];
          dailyValues[name].push({ date: dateStr, ...bar });
        }
      }
      return new Response(JSON.stringify({
        exchange: "NSE", source: "nsearchives.nseindia.com (indices bhavcopy)",
        chunk_start: startParam, chunk_end: endParam,
        trading_days_fetched: daysFetched, trading_days_failed: daysFailed,
        indices: dailyValues,
      }), { headers: { ...CORS, "Content-Type": "application/json" } });
    }

    // ── /backtest — equity OHLC over a date range (was close-only) ──
    if (url.pathname === "/backtest") {
      const startParam = url.searchParams.get("start");
      const endParam = url.searchParams.get("end");
      if (!startParam || !endParam) {
        return new Response(JSON.stringify({ error: "start and end (YYYY-MM-DD) required" }), { headers: { ...CORS, "Content-Type": "application/json" } });
      }
      const startDate = new Date(startParam + "T00:00:00Z");
      const endDate = new Date(endParam + "T00:00:00Z");
      const calendarDays = Math.round((endDate - startDate) / 86400000);
      if (calendarDays > 46) {
        return new Response(JSON.stringify({ error: `Range too large (${calendarDays} days). Keep ≤46 days.` }), { headers: { ...CORS, "Content-Type": "application/json" } });
      }
      const symbolsParam = url.searchParams.get("symbols") || "";
      const wanted = symbolsParam ? new Set(symbolsParam.split(",").map(s => s.trim().toUpperCase())) : null;

      const ohlc = {}; // { SYMBOL: [{date, open, high, low, close}, ...] }
      let daysFetched = 0, daysFailed = 0;
      for (let d = 0; d <= calendarDays; d++) {
        const date = new Date(startDate.getTime() + d * 86400000);
        if (date.getDay() === 0 || date.getDay() === 6) continue;
        const text = await fetchSecBhav(date);
        if (!text) { daysFailed++; continue; }
        daysFetched++;
        const dayData = parseSecCSV(text, wanted);
        const dateStr = date.toISOString().slice(0, 10);
        for (const [sym, bar] of Object.entries(dayData)) {
          if (!ohlc[sym]) ohlc[sym] = [];
          ohlc[sym].push({ date: dateStr, ...bar });
        }
      }
      return new Response(JSON.stringify({
        chunk_start: startParam, chunk_end: endParam,
        trading_days_fetched: daysFetched, trading_days_failed: daysFailed,
        ohlc,
      }), { headers: { ...CORS, "Content-Type": "application/json" } });
    }

    return new Response(JSON.stringify({
      error: "Use /backtest, /indices, or /symbols",
      endpoints: {
        backtest: "/backtest?start=YYYY-MM-DD&end=YYYY-MM-DD&symbols=A,B,C",
        indices: "/indices?start=YYYY-MM-DD&end=YYYY-MM-DD&indices=NIFTY 50,NIFTY BANK",
        symbols: "/symbols  or  /symbols?universe=all",
      },
    }), { headers: { ...CORS, "Content-Type": "application/json" } });
  },
};
