"""
exit_regime_experiment.py — does a better exit + a market-regime gate turn the
squeeze strategy net-positive and beat buy-and-hold? Same realistic execution as
the honest backtest: enter next-day open, 0.15%/side slippage, ~0.22% costs.
"""
import json, glob, os, math
import numpy as np, pandas as pd

OHLC_DIR = "data/backtest/raw_ohlc"
NIFTY_PATH = "data/backtest/raw_indices/Nifty_50.json"
BB_THRESHOLD = 4.5
SLIP = 0.0015
COST = 0.00222

def add_ind(df):
    df = df.copy()
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_width_pct"] = (4*df["bb_std"]) / df["bb_mid"] * 100
    df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain/loss.replace(0,np.nan); df["rsi"]=100-(100/(1+rs))
    df.loc[(loss==0)&(gain>0),"rsi"]=100.0; df.loc[(loss==0)&(gain==0),"rsi"]=50.0
    rmin=df["rsi"].rolling(14).min(); rmax=df["rsi"].rolling(14).max()
    st=(df["rsi"]-rmin)/(rmax-rmin).replace(0,np.nan)*100
    st=st.where(rmax!=rmin,100.0)
    df["stoch_k"]=st.rolling(3).mean(); df["stoch_d"]=df["stoch_k"].rolling(3).mean()
    df["bb_width_5d_min"]=df["bb_width_pct"].rolling(5).min()
    return df

# Nifty 200-DMA regime: {date -> True if Nifty close > its 200-DMA}
nf = pd.DataFrame(sorted(json.load(open(NIFTY_PATH)), key=lambda p:p["date"]))
nf["date"]=pd.to_datetime(nf["date"]); nf["ma200"]=nf["close"].rolling(200).mean()
regime = {d.strftime("%Y-%m-%d"): (c>m if not pd.isna(m) else None)
          for d,c,m in zip(nf["date"], nf["close"], nf["ma200"])}

def run(exit_cfg, regime_gate):
    files = sorted(glob.glob(os.path.join(OHLC_DIR,"*.json")))
    trades=[]
    for f in files:
        try: raw=json.load(open(f))
        except: continue
        if not raw or len(raw)<220: continue
        df=pd.DataFrame(raw)
        if "open" not in df: continue
        df=df.dropna(subset=["close","high","low","open"]).reset_index(drop=True)
        if len(df)<220: continue
        df=add_ind(df)
        o=df["open"].values; c=df["close"].values; h=df["high"].values; l=df["low"].values
        dt=df["date"].astype(str).values
        bbw=df["bb_width_pct"].values; bb5=df["bb_width_5d_min"].values
        sk=df["stoch_k"].values; sd=df["stoch_d"].values
        e10=df["ema10"].values; e30=df["ema30"].values
        n=len(df); mh=exit_cfg["max_hold"]
        i=20
        while i<n-2:
            if (not np.isnan(bbw[i]) and not np.isnan(sk[i]) and not np.isnan(sd[i])
                and not np.isnan(bb5[i]) and bbw[i]<BB_THRESHOLD and bbw[i]<=bb5[i]*1.6
                and sk[i]<50 and sk[i]>sd[i]):
                edate=dt[i+1][:10]
                if regime_gate:
                    r=regime.get(edate)
                    if r is not True:   # skip if regime is down OR unknown
                        i+=1; continue
                er=o[i+1]
                if not (er>0): i+=1; continue
                ef=er*(1+SLIP)
                init_stop=ef*(1-exit_cfg["stop"])
                tgt=ef*(1+exit_cfg["target"]) if exit_cfg.get("target") else None
                peak=ef; result="TIMEOUT"; xraw=None; xidx=min(i+1+mh,n-1)
                for j in range(i+1, min(i+1+mh,n-1)+1):
                    if exit_cfg["mode"]=="trail":
                        active=max(init_stop, peak*(1-exit_cfg["trail"]))
                    else:
                        active=init_stop
                    if l[j]<=active:
                        xraw=min(active,o[j]); result="STOP"; xidx=j; break
                    if tgt and h[j]>=tgt:
                        xraw=max(tgt,o[j]); result="TARGET"; xidx=j; break
                    if c[j]>peak: peak=c[j]
                if result=="TIMEOUT": xidx=min(i+1+mh,n-1); xraw=c[xidx]
                xf=xraw*(1-SLIP)
                net=((xf-ef)/ef - COST)*100
                bh_idx=min(i+1+mh,n-1); bh=((c[bh_idx]*(1-SLIP)-ef)/ef - COST)*100
                trades.append({"y":edate[:4],"net":net,"bh":bh,"res":result,
                               "tc":bool(er<e10[i] and er<e30[i])})
                i=xidx+1
            else: i+=1
    return trades

def summ(trades):
    n=len(trades)
    if not n: return None
    net=sum(t["net"] for t in trades)/n
    bh=sum(t["bh"] for t in trades)/n
    tg=sum(1 for t in trades if t["res"]=="TARGET")/n*100
    st=sum(1 for t in trades if t["res"]=="STOP")/n*100
    return dict(n=n, net=net, bh=bh, edge=net-bh, tgt=tg, stop=st)

configs = [
    ("Baseline  4%/5%/20d      ", {"mode":"fixed","target":0.04,"stop":0.05,"max_hold":20}),
    ("Wider tgt 10%/5%/30d     ", {"mode":"fixed","target":0.10,"stop":0.05,"max_hold":30}),
    ("Trail 6%init/8%trail/40d ", {"mode":"trail","target":None,"stop":0.06,"trail":0.08,"max_hold":40}),
    ("Trail 6%init/12%trail/60d", {"mode":"trail","target":None,"stop":0.06,"trail":0.12,"max_hold":60}),
]

print(f"{'CONFIG':28} {'GATE':5} {'n':>6} {'net%':>8} {'B&H%':>8} {'edge%':>8} {'tgt%':>6} {'stop%':>6}")
print("-"*84)
best=[]
for label,cfg in configs:
    for gate in (False, True):
        s=summ(run(cfg, gate))
        if s:
            print(f"{label:28} {'ON' if gate else 'off':5} {s['n']:>6} {s['net']:>+8.3f} {s['bh']:>+8.3f} {s['edge']:>+8.3f} {s['tgt']:>6.1f} {s['stop']:>6.1f}")
            best.append((label,gate,s))

# Year detail for the single best (highest net, must beat B&H and be >0)
viable=[b for b in best if b[2]["net"]>0 and b[2]["edge"]>0]
print("\nConfigs that are BOTH net-positive AND beat buy-and-hold:")
if viable:
    for label,gate,s in sorted(viable,key=lambda x:-x[2]["net"]):
        print(f"  {label} gate={'ON' if gate else 'off'}: net {s['net']:+.3f}%, edge {s['edge']:+.3f}%, n={s['n']}")
else:
    print("  NONE.")
