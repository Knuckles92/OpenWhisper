#!/usr/bin/env python3
"""
Script to track GitHub repository clone statistics over time.
Aggregates daily clone data from GitHub API into lifetime statistics.

Improvements for long-term stability:
- Properly extracts individual daily counts from API response
- Backfills all available days from 14-day window on first run
- Avoids double-counting by tracking which dates we've seen
- Includes data validation and error handling
- Stores monthly summaries to keep file size manageable
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repository information
REPO_OWNER = os.getenv('REPO_OWNER', 'Knuckles92')
REPO_NAME = os.getenv('REPO_NAME', 'OpenWhisper')

# File paths
CLONE_DATA_FILE = Path('clone_data_temp.json')
STATS_FILE = Path('clone_statistics.json')


def load_json_file(filepath: Path, default: dict = None) -> dict:
    """Load JSON file, return default if file doesn't exist."""
    if filepath.exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Error reading {filepath}: {e}", file=sys.stderr)
            return default or {}
    return default or {}


def save_json_file(filepath: Path, data: dict) -> None:
    """Save data to JSON file with pretty formatting."""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_utc_now() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(timezone.utc)


def parse_api_timestamp(timestamp: str) -> str:
    """Extract date (YYYY-MM-DD) from API timestamp like '2026-01-14T00:00:00Z'."""
    if not timestamp:
        return get_utc_now().strftime('%Y-%m-%d')
    return timestamp[:10]  # First 10 chars are YYYY-MM-DD


def get_default_stats() -> dict:
    """Return default statistics structure."""
    return {
        'repository': f'{REPO_OWNER}/{REPO_NAME}',
        'schema_version': 2,  # Track schema for future migrations
        'last_updated': None,
        'lifetime_stats': {
            'total_clones': 0,
            'total_unique_cloners': 0,
            'tracking_start_date': None,
            'days_tracked': 0
        },
        'daily_history': {},  # Changed to dict for O(1) lookups: {"2026-01-14": {...}}
        'monthly_summaries': {},  # {"2026-01": {"clones": X, "unique": Y}}
        'last_14_days': {
            'clones': 0,
            'unique_cloners': 0,
            'period_start': None,
            'period_end': None
        }
    }


def migrate_stats_if_needed(stats: dict) -> dict:
    """Migrate old stats format to new format if needed."""
    schema_version = stats.get('schema_version', 1)
    
    if schema_version < 2:
        print("Migrating statistics to schema version 2...")
        
        # Convert daily_history from list to dict
        old_history = stats.get('daily_history', [])
        if isinstance(old_history, list):
            new_history = {}
            for entry in old_history:
                date = entry.get('date')
                if date:
                    new_history[date] = {
                        'clones': entry.get('clones', 0),
                        'unique_cloners': entry.get('unique_cloners', 0),
                        'source': 'migrated'
                    }
            stats['daily_history'] = new_history
        
        # Add monthly_summaries if missing
        if 'monthly_summaries' not in stats:
            stats['monthly_summaries'] = {}
        
        stats['schema_version'] = 2
        print(f"Migration complete. Converted {len(stats['daily_history'])} daily entries.")
    
    return stats


def validate_clone_count(value: Any) -> int:
    """Validate and return a non-negative integer clone count."""
    try:
        count = int(value)
        return max(0, count)  # Ensure non-negative
    except (TypeError, ValueError):
        return 0


def extract_daily_data_from_api(api_data: dict) -> dict:
    """
    Extract individual daily clone counts from API response.
    
    API returns:
    {
        "count": 123,  # Total clones in 14-day period
        "uniques": 45,  # Total unique cloners in 14-day period
        "clones": [
            {"timestamp": "2026-01-01T00:00:00Z", "count": 10, "uniques": 5},
            {"timestamp": "2026-01-02T00:00:00Z", "count": 8, "uniques": 3},
            ...
        ]
    }
    
    Returns dict: {"2026-01-01": {"clones": 10, "unique_cloners": 5}, ...}
    """
    daily_data = {}
    clones_list = api_data.get('clones', [])
    
    if not isinstance(clones_list, list):
        print("Warning: API 'clones' field is not a list", file=sys.stderr)
        return daily_data
    
    for entry in clones_list:
        if not isinstance(entry, dict):
            continue
            
        timestamp = entry.get('timestamp', '')
        date = parse_api_timestamp(timestamp)
        
        clones = validate_clone_count(entry.get('count', 0))
        uniques = validate_clone_count(entry.get('uniques', 0))
        
        # Only store if we have valid data
        if date and (clones > 0 or uniques > 0):
            daily_data[date] = {
                'clones': clones,
                'unique_cloners': uniques,
                'source': 'api'
            }
    
    return daily_data


def update_monthly_summaries(stats: dict) -> None:
    """
    Update monthly summaries from daily history.
    Keeps data compact for long-term storage.
    """
    monthly = {}
    
    for date, data in stats.get('daily_history', {}).items():
        # Extract month: "2026-01-14" -> "2026-01"
        month = date[:7]
        
        if month not in monthly:
            monthly[month] = {
                'clones': 0,
                'unique_cloners': 0,
                'days_with_data': 0
            }
        
        monthly[month]['clones'] += data.get('clones', 0)
        monthly[month]['unique_cloners'] += data.get('unique_cloners', 0)
        monthly[month]['days_with_data'] += 1
    
    stats['monthly_summaries'] = monthly


def calculate_lifetime_stats(stats: dict) -> None:
    """Calculate lifetime statistics from daily history."""
    daily_history = stats.get('daily_history', {})
    
    if not daily_history:
        stats['lifetime_stats'] = {
            'total_clones': 0,
            'total_unique_cloners': 0,
            'tracking_start_date': None,
            'days_tracked': 0
        }
        return
    
    # Sum all daily clones
    total_clones = sum(
        data.get('clones', 0) 
        for data in daily_history.values()
    )
    
    # For unique cloners, we can't simply sum (same person might clone on multiple days)
    # Best estimate: use the maximum daily unique count we've seen
    # This is a lower bound on true unique cloners
    max_daily_unique = max(
        (data.get('unique_cloners', 0) for data in daily_history.values()),
        default=0
    )
    
    # Get date range
    dates = sorted(daily_history.keys())
    
    stats['lifetime_stats'] = {
        'total_clones': total_clones,
        'total_unique_cloners': max_daily_unique,  # Best available estimate
        'tracking_start_date': dates[0] if dates else None,
        'tracking_end_date': dates[-1] if dates else None,
        'days_tracked': len(dates)
    }


def process_clone_data() -> None:
    """Process clone data from API and update statistics file."""
    now = get_utc_now()
    today = now.strftime('%Y-%m-%d')
    
    # Load current API response
    api_data = load_json_file(CLONE_DATA_FILE, {'count': 0, 'uniques': 0, 'clones': []})
    
    # Load existing statistics and migrate if needed
    stats = load_json_file(STATS_FILE, get_default_stats())
    stats = migrate_stats_if_needed(stats)
    
    # Ensure required fields exist
    if 'daily_history' not in stats or not isinstance(stats['daily_history'], dict):
        stats['daily_history'] = {}
    if 'monthly_summaries' not in stats:
        stats['monthly_summaries'] = {}
    
    # Extract daily data from API response
    api_daily_data = extract_daily_data_from_api(api_data)
    
    # Merge API data into our history (API data takes precedence for overlapping dates)
    daily_history = stats['daily_history']
    new_dates = []
    updated_dates = []
    
    for date, data in api_daily_data.items():
        if date not in daily_history:
            # New date we haven't seen before
            daily_history[date] = data
            new_dates.append(date)
        else:
            # Date exists - update if API has more data
            existing = daily_history[date]
            if data['clones'] > existing.get('clones', 0):
                daily_history[date] = data
                updated_dates.append(date)
    
    stats['daily_history'] = daily_history
    
    # Update 14-day window stats
    total_clones_14d = validate_clone_count(api_data.get('count', 0))
    total_uniques_14d = validate_clone_count(api_data.get('uniques', 0))
    
    clones_list = api_data.get('clones', [])
    period_start = None
    period_end = None
    if clones_list:
        period_start = parse_api_timestamp(clones_list[0].get('timestamp', ''))
        period_end = parse_api_timestamp(clones_list[-1].get('timestamp', ''))
    
    stats['last_14_days'] = {
        'clones': total_clones_14d,
        'unique_cloners': total_uniques_14d,
        'period_start': period_start,
        'period_end': period_end
    }
    
    # Calculate lifetime stats
    calculate_lifetime_stats(stats)
    
    # Update monthly summaries
    update_monthly_summaries(stats)
    
    # Update metadata
    stats['repository'] = f'{REPO_OWNER}/{REPO_NAME}'
    stats['schema_version'] = 2
    stats['last_updated'] = now.isoformat()
    
    # Save updated statistics
    save_json_file(STATS_FILE, stats)
    
    # Print summary
    lifetime = stats['lifetime_stats']
    print(f"\n{'='*60}")
    print(f"Clone Statistics Update - {today}")
    print(f"{'='*60}")
    print(f"API returned {len(api_daily_data)} days of data")
    print(f"  New dates added: {len(new_dates)}")
    print(f"  Dates updated: {len(updated_dates)}")
    print(f"\nLast 14 days: {total_clones_14d} clones, {total_uniques_14d} unique cloners")
    print(f"\nLifetime tracked:")
    print(f"  Total clones: {lifetime['total_clones']}")
    print(f"  Days tracked: {lifetime['days_tracked']}")
    print(f"  Tracking since: {lifetime['tracking_start_date']}")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    try:
        process_clone_data()
        print("✓ Clone statistics updated successfully")
    except Exception as e:
        print(f"✗ Error processing clone statistics: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
