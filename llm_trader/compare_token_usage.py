#!/usr/bin/env python3
"""Compare token usage between standard and compact prompts"""

import re
import sys
from pathlib import Path


def count_tokens(text: str) -> int:
    """Estimate token count (1 token ≈ 4 characters)"""
    return len(text) // 4


def extract_prompt_from_log(log_file: Path) -> tuple[str, str]:
    """Extract system and user messages from API detailed log

    Returns:
        Tuple of (system_message, user_message)
    """
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Find SYSTEM MESSAGE section
    system_match = re.search(
        r'-+\s*SYSTEM MESSAGE\s*-+\s*(.*?)\s*-+\s*USER MESSAGE',
        content,
        re.DOTALL
    )
    system_msg = system_match.group(1).strip() if system_match else ""

    # Find USER MESSAGE section (up to next ===)
    user_match = re.search(
        r'-+\s*USER MESSAGE\s*-+\s*(.*?)\s*={80}',
        content,
        re.DOTALL
    )
    user_msg = user_match.group(1).strip() if user_match else ""

    return system_msg, user_msg


def main():
    # Find latest logs
    logs_dir = Path("logs")

    # Get all deepseek_nvidia API detailed logs sorted by modification time
    log_files = sorted(
        logs_dir.glob("deepseek_nvidia_*_api_detailed.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if len(log_files) < 2:
        print("ERROR: Need at least 2 log files to compare")
        print(f"Found only {len(log_files)} log files")
        sys.exit(1)

    # Assume the latest is compact, second latest is standard
    compact_log = log_files[0]
    standard_log = log_files[1]

    print("="*80)
    print("TOKEN USAGE COMPARISON: Standard vs Compact Prompt")
    print("="*80)
    print()

    # Analyze standard prompt
    print(f"📄 Standard Prompt Log: {standard_log.name}")
    sys_std, user_std = extract_prompt_from_log(standard_log)
    sys_std_tokens = count_tokens(sys_std)
    user_std_tokens = count_tokens(user_std)
    total_std_tokens = sys_std_tokens + user_std_tokens

    print(f"   System message: {len(sys_std):>6,} chars ≈ {sys_std_tokens:>5,} tokens")
    print(f"   User message:   {len(user_std):>6,} chars ≈ {user_std_tokens:>5,} tokens")
    print(f"   TOTAL:          {len(sys_std) + len(user_std):>6,} chars ≈ {total_std_tokens:>5,} tokens")
    print()

    # Analyze compact prompt
    print(f"📄 Compact Prompt Log: {compact_log.name}")
    sys_compact, user_compact = extract_prompt_from_log(compact_log)
    sys_compact_tokens = count_tokens(sys_compact)
    user_compact_tokens = count_tokens(user_compact)
    total_compact_tokens = sys_compact_tokens + user_compact_tokens

    print(f"   System message: {len(sys_compact):>6,} chars ≈ {sys_compact_tokens:>5,} tokens")
    print(f"   User message:   {len(user_compact):>6,} chars ≈ {user_compact_tokens:>5,} tokens")
    print(f"   TOTAL:          {len(sys_compact) + len(user_compact):>6,} chars ≈ {total_compact_tokens:>5,} tokens")
    print()

    # Calculate savings
    print("="*80)
    print("📊 TOKEN SAVINGS ANALYSIS")
    print("="*80)
    print()

    saved_tokens = total_std_tokens - total_compact_tokens
    reduction_pct = (saved_tokens / total_std_tokens * 100) if total_std_tokens > 0 else 0

    print(f"Tokens saved:     {saved_tokens:>5,} tokens")
    print(f"Reduction:        {reduction_pct:>5.1f}%")
    print()

    # Show data sections comparison
    print("="*80)
    print("📋 DATA SECTION COMPARISON")
    print("="*80)
    print()

    # Analyze user message structure
    print("Standard prompt structure:")
    if "DATA:" in user_std:
        parts = user_std.split("DATA:", 1)
        print(f"  Instructions: {len(parts[0]):>6,} chars")
        if len(parts) > 1:
            print(f"  Data section: {len(parts[1]):>6,} chars")

    print()
    print("Compact prompt structure:")
    # Count different sections in compact format
    meta_count = user_compact.count("META|")
    pos_count = user_compact.count("POS|")
    mkt_count = user_compact.count("MKT|")
    series_sections = len(re.findall(r'\d+[sm]:t,dbps,volk', user_compact))
    feat_count = user_compact.count("FEAT_")
    mem_count = user_compact.count("MEM|")
    constraint_count = user_compact.count("CONSTRAINT|")

    print(f"  META sections:        {meta_count}")
    print(f"  POS sections:         {pos_count}")
    print(f"  MKT sections:         {mkt_count}")
    print(f"  Time series (1s-15m): {series_sections}")
    print(f"  FEAT sections:        {feat_count}")
    print(f"  MEM sections:         {mem_count}")
    print(f"  CONSTRAINT sections:  {constraint_count}")
    print()

    # Success criteria
    print("="*80)
    print("✅ SUCCESS CRITERIA")
    print("="*80)
    print()

    target_reduction = 75.0  # Target 75%+ reduction
    if reduction_pct >= target_reduction:
        print(f"✅ PASSED: {reduction_pct:.1f}% reduction (target: {target_reduction}%+)")
    else:
        print(f"⚠️  BELOW TARGET: {reduction_pct:.1f}% reduction (target: {target_reduction}%+)")

    if total_compact_tokens <= 1800:
        print(f"✅ PASSED: {total_compact_tokens} tokens ≤ 1800 token target")
    else:
        print(f"⚠️  ABOVE TARGET: {total_compact_tokens} tokens > 1800 token target")

    print()


if __name__ == "__main__":
    main()
