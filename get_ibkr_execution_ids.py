from ib_insync import IB, ExecutionFilter
import asyncio

async def fetch_real_ids():
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4002, clientId=102)
        print("📡 Requesting today's executions...")
        
        # Filter for TQQQ executions today
        execs = await ib.reqExecutionsAsync(ExecutionFilter(symbol="TQQQ"))
        
        if not execs:
            print("❌ No TQQQ executions found in IBKR for today.")
            return

        for e in execs:
            ex = e.execution
            print(f"✅ Found Execution: Symbol={e.contract.symbol} Qty={ex.shares} Side={ex.side} Price={ex.price}")
            print(f"   OrderId={ex.orderId} PermId={ex.permId} ExecId={ex.execId} Time={ex.time}")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(fetch_real_ids())
