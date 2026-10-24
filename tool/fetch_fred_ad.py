import pandas_datareader.data as web
import datetime
import pandas as pd
import os
import sys

def test_fred_data():
    # 设定时间：取最近45天，确保包含至少15-20个交易日
    end = datetime.datetime.now()
    start = end - datetime.timedelta(days=45)
    
    print(f"正在核实 FRED 数据源 (ADNAS & ADNASD)... 范围: {start.date()} 到 {end.date()}")
    
    output_dir = "data"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    try:
        # 获取上涨家数 (ADNAS) 和 下跌家数 (ADNASD)
        # FRED 匿名访问
        df = web.DataReader(["ADNAS", "ADNASD"], "fred", start, end)
        
        if df is None or df.empty:
            print("失败：FRED 返回了空数据。可能该指标在当前时间点未更新。")
            return
            
        # 计算 AD 差值
        df['AD_Value'] = df['ADNAS'] - df['ADNASD']
        
        # 结果过滤
        df = df.dropna()
        
        print("\n✅ 成功拿到数据！最近 5 个交易日样本：")
        print(df[['ADNAS', 'ADNASD', 'AD_Value']].tail(5))
        
        # 检查是否满足 15 天校准
        if len(df) >= 15:
            print(f"\n数据量足够：已获取 {len(df)} 个交易日的数据。")
            file_name = os.path.join(output_dir, "nasdaq_ad_calibration_fred.csv")
            df.to_csv(file_name)
            print(f"已保存至: {os.path.abspath(file_name)}")
            print("\n[测试成功]")
        else:
            print(f"\n警告：仅拿到 {len(df)} 天数据，建议将 start 时间范围再拉长一点。")
            # 即使不足 15 天也保存一下
            file_name = os.path.join(output_dir, "nasdaq_ad_calibration_fred_partial.csv")
            df.to_csv(file_name)

    except Exception as e:
        print(f"⚠️ 运行报错: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    test_fred_data()
