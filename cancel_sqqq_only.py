from ib_insync import IB, util
import time

def cancel_sqqq_sync():
    ib = IB()
    try:
        print("🔌 Connecting...")
        ib.connect('127.0.0.1', 4002, clientId=101)
        
        print("📡 Syncing all open orders...")
        ib.reqAllOpenOrders()
        ib.sleep(1.5)
        
        trades = ib.openTrades()
        print(f"📊 Total open trades found: {len(trades)}")
        
        cancelled_count = 0
        for tr in trades:
            symbol = tr.contract.symbol
            if symbol == "SQQQ":
                print(f"⚠️ Cancelling SQQQ order: ID={tr.order.orderId} {tr.order.action} {tr.order.totalQuantity}")
                ib.cancelOrder(tr.order)
                cancelled_count += 1
        
        if cancelled_count > 0:
            print(f"✅ Successfully sent {cancelled_count} SQQQ cancel requests.")
            ib.sleep(2)
        else:
            print("✓ No SQQQ open orders to cancel.")
            
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        ib.disconnect()

if __name__ == "__main__":
    cancel_sqqq_sync()
