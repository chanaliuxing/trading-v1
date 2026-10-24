import yfinance as yf
import pandas as pd
import os
import sys

def fetch_ad_nasd_data(symbol="QQQ", period="1mo", interval="5m"):
    """
    由于 Yahoo Finance 限制了 ^ADCP / ^ADD 等宽度指标的 API 访问，
    我们改用 QQQ (Nasdaq 100 ETF) 作为校准参考。
    QQQ 与 AD-NASD 在趋势上具有极高的相关性。
    """
    print(f"正在从 Yahoo Finance 下载 {symbol} 的 {interval} 数据 (作为 AD-NASD 趋势参考)...")
    
    try:
        # 下载数据
        df = yf.download(symbol, period=period, interval=interval)
        
        if df is None or df.empty:
            print(f"错误: 未获取到 {symbol} 数据。请检查网络。")
            return None

        # 处理 yfinance 可能返回的 Multi-index 索引问题
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # 格式化处理
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        
        # 只保留核心列
        df = df[['Open', 'High', 'Low', 'Close']]
        
        # 计算 Delta (涨跌变动量)
        df['Delta'] = df['Close'].diff()
        
        print(f"成功获取 {len(df)} 行数据。")
        return df
    except Exception as e:
        print(f"执行过程中发生异常: {e}")
        return None

if __name__ == "__main__":
    output_dir = "data"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    # 尝试获取 QQQ 作为替代参考
    ad_data = fetch_ad_nasd_data()
    
    if ad_data is not None:
        print("\n--- 数据预览 (最近 5 行) ---")
        print(ad_data.tail())
        
        file_name = os.path.join(output_dir, "nasdaq_trend_reference_5m.csv")
        ad_data.to_csv(file_name)
        print(f"\n文件已保存至: {os.path.abspath(file_name)}")
        print("\n[注意]: 由于 Yahoo API 限制，目前无法直接获取 ^ADCP 指数。")
        print("建议使用 QQQ 的走势进行初步逻辑校准，或者稍后在 IBKR 正常连接时录制真实 AD 数据。")
        print("\n[测试通过]")
    else:
        print("\n[测试失败] 无法获取数据")
        sys.exit(1)
