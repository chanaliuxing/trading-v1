storage "file" {
  path = "/vault/data"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1  # Use TLS in production
}

api_addr = "http://0.0.0.0:8200"
cluster_addr = "https://0.0.0.0:8201"
ui = true

# Disable mlock in dev (enable in production)
disable_mlock = true

# Telemetry
telemetry {
  prometheus_retention_time = "30s"
  disable_hostname = true
}

# Audit logging
audit {
  file {
    file_path = "/vault/logs/audit.log"
  }
}
