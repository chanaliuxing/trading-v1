One-Click Stack Management

  I've created three simple scripts in ~/grafana-stack/:

  🚀 Start All Services

  cd ~/grafana-stack
  ./start-stack.sh

  This script will:
  - Check if services are already running (won't duplicate)
  - Start Loki, Tempo, and Promtail in the background
  - Perform health checks
  - Display status and quick links

  🛑 Stop All Services

  cd ~/grafana-stack
  ./stop-stack.sh

  Gracefully stops all running services.

  📊 Check Status

  cd ~/grafana-stack
  ./status-stack.sh

  Shows the current status of all services with health checks.

  Current Status

  All services are running and healthy:
  - ✅ Grafana: http://localhost:3000
  - ✅ Loki: http://localhost:3100
  - ✅ Tempo: http://localhost:3200
  - ✅ Promtail: http://localhost:9080/targets (4 jobs configured)

  Optional: Create an Alias

  Add this to your ~/.zshrc or ~/.bashrc for even easier access:

  alias grafana-start='~/grafana-stack/start-stack.sh'
  alias grafana-stop='~/grafana-stack/stop-stack.sh'
  alias grafana-status='~/grafana-stack/status-stack.sh'

  Then you can just type grafana-start from anywhere!