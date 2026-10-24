#!/usr/bin/env python3
"""
Register Avro schemas to Schema Registry
"""
import os
import json
import requests
from pathlib import Path

SCHEMA_REGISTRY_URL = os.getenv('SCHEMA_REGISTRY_URL', 'http://localhost:8081')
SCHEMAS_DIR = Path(__file__).parent / 'schemas'

# Topic to schema mapping
TOPIC_SCHEMAS = {
    'order.intent.v1': 'order_intent_v1.avsc',
    'risk.decision.v2': 'risk_decision_v2.avsc',
    'exec.order.status.v2': 'exec_order_status_v2.avsc',
    'exec.fill.v2': 'exec_fill_v2.avsc',
    'md.bar.5s.v2': 'bar_5s_v2.avsc',
}

def register_schema(subject, schema_path):
    """Register a schema to Schema Registry"""
    print(f"Registering {subject}...")

    with open(schema_path, 'r') as f:
        schema_json = json.load(f)

    payload = {
        'schema': json.dumps(schema_json)
    }

    url = f"{SCHEMA_REGISTRY_URL}/subjects/{subject}-value/versions"

    try:
        response = requests.post(url, json=payload, headers={'Content-Type': 'application/vnd.schemaregistry.v1+json'})
        response.raise_for_status()

        result = response.json()
        print(f"  ✓ Registered {subject} with ID: {result.get('id')}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Failed to register {subject}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"     Response: {e.response.text}")
        return False

def check_schema_registry():
    """Check if Schema Registry is available"""
    try:
        response = requests.get(f"{SCHEMA_REGISTRY_URL}/subjects")
        response.raise_for_status()
        print(f"✓ Schema Registry is available at {SCHEMA_REGISTRY_URL}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"✗ Schema Registry is not available: {e}")
        return False

def main():
    """Main function"""
    print("=" * 60)
    print("Avro Schema Registration")
    print("=" * 60)

    if not check_schema_registry():
        print("\nPlease start Schema Registry first:")
        print("  cd infra && docker-compose up -d schema-registry")
        return 1

    print(f"\nSchemas directory: {SCHEMAS_DIR}")
    print(f"Registering {len(TOPIC_SCHEMAS)} schemas...\n")

    success_count = 0
    for topic, schema_file in TOPIC_SCHEMAS.items():
        schema_path = SCHEMAS_DIR / schema_file
        if not schema_path.exists():
            print(f"✗ Schema file not found: {schema_path}")
            continue

        if register_schema(topic, schema_path):
            success_count += 1

    print("\n" + "=" * 60)
    print(f"Registered {success_count}/{len(TOPIC_SCHEMAS)} schemas successfully")
    print("=" * 60)

    return 0 if success_count == len(TOPIC_SCHEMAS) else 1

if __name__ == "__main__":
    exit(main())
