# 历史数据回补命令

以下是为 `config.yml` 中 `universe` 列表下的所有股票, 回补最近 10 天 (使用 `--days 10`), 采用 `backfill` 模式, 并将每次 IB API 请求限制为 3 天 (`--chunk-days 3`) 的命令. 

**请在运行这些命令之前, 确保您的 Interactive Brokers TWS (Trader Workstation) 或 IB Gateway 已正确连接并运行.**

**股票代码列表:** `AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC`

---

## 1. 回补 1 分钟数据

```bash
python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC \
    --days 10 \
    --bar-size "1 min" \
    --fill-mode backfill \
    --chunk-days 3
```

---

## 2. 回补 5 分钟数据

```bash
python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC \
    --days 10 \
    --bar-size "5 mins" \
    --fill-mode backfill \
    --chunk-days 3
```

---

## 3. 回补 15 分钟数据

```bash
python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC \
    --days 10 \
    --bar-size "15 mins" \
    --fill-mode backfill \
    --chunk-days 3
```

---

## 4. 回补 30 分钟数据

```bash
python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC \
    --days 10 \
    --bar-size "30 mins" \
    --fill-mode backfill \
    --chunk-days 3
```

---

## 5. 回补 1 小时数据

```bash
python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC \
    --days 10 \
    --bar-size "1 hour" \
    --fill-mode backfill \
    --chunk-days 3
```

---

## 6. 回补 1 天数据

```bash
python tool/load_hist_multi.py \
    --symbols AAPL TSLA NVDA SPY MSFT AMD META AMZN GOOGL QQQ TQQQ SQQQ VOO ORCL INTC \
    --days 10 \
    --bar-size "1 day" \
    --fill-mode backfill \
    --chunk-days 3
```

---

## 关于 `2h`, `3h`, `4h` 时间粒度

`tool/load_hist_multi.py` 脚本的 `normalize_bar_size` 函数目前不直接支持 `"2 hours"`, `"3 hours"`, `"4 hours"` 这样的时间粒度. 如果您确实需要回补这些时间粒度的数据, 可能需要:
*   修改 `tool/load_hist_multi.py` 脚本以支持这些粒度.
*   或者, 使用其他工具或方法来获取这些数据.
