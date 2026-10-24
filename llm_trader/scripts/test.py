import yfinance as yf
import pandas as pd

def get_top_20_gainers():
    """
    使用 yfinance 获取美股市场涨幅最高的股票,并过滤出常规股票.
    """
    print("正在从 Yahoo Finance 获取市场领涨股数据...")
    
    try:
        # 获取日内涨幅榜
        gainers = yf.screener.get_day_gainers()
        
        if gainers.empty:
            print("未能获取到数据,可能当前市场休市或数据源无响应.")
            return

        # 将数据转换为 pandas DataFrame,方便处理
        df = pd.DataFrame(gainers)
        
        # 过滤出常规股票 (Equity), 排除 ETF, 基金, 货币等
        # 'quoteType' 列是关键
        equities = df[df['quoteType'] == 'EQUITY'].copy()
        
        if equities.empty:
            print("在领涨股列表中未找到常规股票.")
            return
            
        # 选择我们需要的列并重命名,让输出更清晰
        equities_formatted = equities[[ 
            'symbol', 
            'longName', 
            'regularMarketPrice', 
            'regularMarketChange', 
            'regularMarketChangePercent'
        ]].rename(columns={
            'symbol': '代码',
            'longName': '名称',
            'regularMarketPrice': '价格',
            'regularMarketChange': '涨跌额',
            'regularMarketChangePercent': '涨跌幅'
        })

        # 获取涨幅最高的 20 只
        top_20 = equities_formatted.head(20)

        # --- 格式化输出 ---
        print("\n--- 今日美股涨幅 TOP 20 (仅常规股票) ---")
        
        # 为了美观,将涨跌幅转换为百分比字符串
        top_20['涨跌幅'] = top_20['涨跌幅'].map(lambda x: f"{x:.2%}")
        top_20['价格'] = top_20['价格'].map(lambda x: f"${x:.2f}")
        top_20['涨跌额'] = top_20['涨跌额'].map(lambda x: f"{x:+.2f}")
        
        print(top_20.to_string(index=False))
        print("----------------------------------------")

    except Exception as e:
        print(f"获取数据时发生错误: {e}")
        print("请检查网络连接,或确认 'yfinance' 库已正确安装 (pip install yfinance).")

if __name__ == "__main__":
    # 设置 pandas 显示选项,确保长名称能完整显示
    pd.set_option('display.max_colwidth', 60)
    pd.set_option('display.width', 1000)
    
    get_top_20_gainers()
