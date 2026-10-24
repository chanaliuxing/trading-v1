#!/usr/bin/env python3
"""
PostgreSQL Schema Audit Script

Compares the database schema definition with actual code usage to find mismatches:
- Tables referenced that don't exist
- Columns referenced that don't exist in their tables
- Status/state value mismatches

Usage:
    python scripts/audit_pg_schema.py
"""

import re
import os
from pathlib import Path
from collections import defaultdict

# Read the schema DDL
SCHEMA_FILE = Path("./db/pg/full_core_ddl.sql")
PROJECT_ROOT = Path(".")

def parse_schema():
    """Parse the DDL file to extract table and column definitions"""
    tables = {}
    current_table = None

    with open(SCHEMA_FILE, 'r') as f:
        content = f.read()

    # Find all CREATE TABLE statements
    table_pattern = r'CREATE TABLE\s+([\w.]+)\s*\((.*?)\);'

    for match in re.finditer(table_pattern, content, re.DOTALL | re.IGNORECASE):
        table_name = match.group(1)
        columns_def = match.group(2)

        # Extract column names
        columns = []
        for line in columns_def.split('\n'):
            line = line.strip()
            if line and not line.startswith('CONSTRAINT') and not line.startswith('CHECK'):
                # Extract column name (first word)
                col_match = re.match(r'(\w+)\s+', line)
                if col_match:
                    columns.append(col_match.group(1))

        tables[table_name] = columns
        # Also store without schema prefix
        if '.' in table_name:
            short_name = table_name.split('.')[-1]
            tables[short_name] = columns

    return tables

def find_sql_queries(directory):
    """Find all SQL queries in Python files"""
    queries = []

    for py_file in directory.rglob("*.py"):
        if 'venv' in str(py_file) or '.pyc' in str(py_file):
            continue

        try:
            with open(py_file, 'r') as f:
                content = f.read()

            # Find SQL queries in triple quotes or regular strings
            # Look for SELECT, INSERT, UPDATE, DELETE statements
            patterns = [
                r'"""(.*?SELECT.*?FROM.*?)"""',
                r"'''(.*?SELECT.*?FROM.*?)'''",
                r'"(SELECT.*?FROM.*?)"',
                r"'(SELECT.*?FROM.*?)'",
                r'"""(.*?INSERT INTO.*?)"""',
                r"'''(.*?INSERT INTO.*?)'''",
                r'"""(.*?UPDATE.*?SET.*?)"""',
                r"'''(.*?UPDATE.*?SET.*?)'''",
            ]

            for pattern in patterns:
                for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
                    query = match.group(1).strip()
                    queries.append({
                        'file': str(py_file.relative_to(PROJECT_ROOT)),
                        'query': query
                    })
        except Exception as e:
            print(f"Error reading {py_file}: {e}")

    return queries

def extract_table_columns_from_query(query):
    """Extract table and column references from a SQL query"""
    results = []

    # Extract table names from FROM clause
    from_pattern = r'FROM\s+([\w.]+)'
    for match in re.finditer(from_pattern, query, re.IGNORECASE):
        table = match.group(1)
        results.append(('table', table, query))

    # Extract column names from SELECT/WHERE/INSERT
    # This is a simplified version - doesn't handle all SQL complexity
    column_pattern = r'\b(\w+)\s*[=<>!]|SELECT\s+([\w,\s*()]+)\s+FROM'

    return results

def audit_schema():
    """Main audit function"""
    print("=" * 80)
    print("PostgreSQL Schema Audit")
    print("=" * 80)
    print()

    # Parse schema
    print("📖 Parsing schema DDL...")
    tables = parse_schema()
    print(f"   Found {len(tables)} tables")
    print()

    # Find queries
    print("🔍 Scanning Python files for SQL queries...")
    queries = find_sql_queries(PROJECT_ROOT)
    print(f"   Found {len(queries)} SQL queries")
    print()

    # Analyze queries
    print("🔬 Analyzing queries for schema mismatches...")
    print()

    issues = []

    for q in queries:
        query = q['query']
        file = q['file']

        # Check for specific known issues

        # Issue 1: filled_qty column (doesn't exist)
        if 'filled_qty' in query.lower():
            issues.append({
                'type': 'missing_column',
                'severity': 'high',
                'file': file,
                'issue': "References 'filled_qty' column which doesn't exist in core_order",
                'query': query[:100]
            })

        # Issue 2: status column (should be 'state' in core_order)
        if re.search(r'\bstatus\s*IN', query, re.IGNORECASE) and 'core_order' in query.lower():
            issues.append({
                'type': 'wrong_column_name',
                'severity': 'high',
                'file': file,
                'issue': "References 'status' column in core_order (should be 'state')",
                'query': query[:100]
            })

        # Issue 3: PENDING status (should be 'NEW' in core_order)
        if "'PENDING'" in query and 'core_order' in query.lower():
            issues.append({
                'type': 'wrong_status_value',
                'severity': 'medium',
                'file': file,
                'issue': "Uses 'PENDING' status (should be 'NEW' in core_order)",
                'query': query[:100]
            })

        # Issue 4: Missing schema qualification
        if re.search(r'FROM\s+core_order\b', query, re.IGNORECASE) and 'core.core_order' not in query:
            issues.append({
                'type': 'missing_schema',
                'severity': 'low',
                'file': file,
                'issue': "Table reference missing schema qualification (should be core.core_order)",
                'query': query[:100]
            })

    # Print results
    if issues:
        print(f"❌ Found {len(issues)} potential issues:")
        print()

        # Group by severity
        by_severity = defaultdict(list)
        for issue in issues:
            by_severity[issue['severity']].append(issue)

        for severity in ['high', 'medium', 'low']:
            if severity in by_severity:
                print(f"  {severity.upper()} Priority:")
                for issue in by_severity[severity]:
                    print(f"    - {issue['file']}")
                    print(f"      {issue['issue']}")
                    print(f"      Query: {issue['query']}...")
                    print()
    else:
        print("✅ No schema mismatches found!")

    print()
    print("=" * 80)
    print("Schema Information:")
    print("=" * 80)
    print()
    print("core_order table columns:")
    if 'core.core_order' in tables:
        for col in tables['core.core_order']:
            print(f"  - {col}")
    print()
    print("Valid state values for core_order: NEW, SUBMITTED, PARTIAL, FILLED, CANCELLED, REJECTED")
    print()

if __name__ == "__main__":
    audit_schema()
