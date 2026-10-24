import yfinance as yf
tickers = ["^ADD", "^ADDN", "^ADDQ", "^NYAD", "^NAAD", "^ADCP", "ADD", "ADDN"]
for t in tickers:
    print(f"Testing {t}...")
    df = yf.download(t, period="1d", interval="1m")
    if not df.empty:
        print(f"SUCCESS: {t} works!")
    else:
        print(f"FAILED: {t} no data.")
