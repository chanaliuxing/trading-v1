#!/bin/bash
# Cleanup backfill jobs by deleting and recreating Kafka topics

set -e

BOOTSTRAP="localhost:9092"

echo "=== Cleaning up backfill Kafka topics ==="

# List of backfill topics to clean
TOPICS=(
  "md.backfill.request.v1"
  "md.backfill.request.5s.v1"
  "md.backfill.request.1m.v1"
  "md.backfill.request.5m.v1"
  "md.backfill.request.10m.v1"
  "md.backfill.request.15m.v1"
  "md.backfill.request.30m.v1"
  "md.backfill.request.1h.v1"
  "md.backfill.request.2h.v1"
  "md.backfill.request.3h.v1"
  "md.backfill.request.4h.v1"
  "md.backfill.request.1d.v1"
  "md.gap.v1"
)

for topic in "${TOPICS[@]}"; do
  echo "Deleting topic: $topic"
  kafka-topics --bootstrap-server "$BOOTSTRAP" --delete --topic "$topic" 2>/dev/null || echo "  (topic doesn't exist or already deleted)"
done

echo ""
echo "Waiting 3 seconds for deletion to complete..."
sleep 3

for topic in "${TOPICS[@]}"; do
  echo "Recreating topic: $topic"
  kafka-topics --bootstrap-server "$BOOTSTRAP" --create --topic "$topic" --partitions 3 --replication-factor 1 2>/dev/null || echo "  (failed to create - may need manual intervention)"
done

echo ""
echo "✅ Backfill topics cleaned up!"
echo ""
echo "To verify, run:"
echo "  kafka-console-consumer --bootstrap-server localhost:9092 --topic md.backfill.request.v1 --from-beginning --timeout-ms 2000"
