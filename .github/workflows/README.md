# Clone Statistics Tracking

This workflow automatically tracks repository clone statistics over time, since GitHub only provides the last 14 days of data.

## How It Works

1. **Daily Schedule**: Runs automatically every day at midnight UTC
2. **API Call**: Fetches clone statistics from GitHub's API (returns 14 days of data)
3. **Data Extraction**: Parses individual daily counts from the API response
4. **Backfill**: On first run, captures all 14 days of available history
5. **Merge**: Adds new dates and updates existing ones if API has better data
6. **Auto-Commit**: Commits and pushes the updated statistics back to the repository

## Key Features

- **No double-counting**: Stores actual daily values, not 14-day totals
- **Backfill support**: First run captures all available historical data
- **Schema versioning**: Automatic migration when format changes
- **Monthly summaries**: Aggregated data for long-term storage efficiency
- **Data validation**: Sanity checks on all values

## Setup

The workflow uses GitHub's built-in `GITHUB_TOKEN` which is automatically available and has the necessary permissions. No additional setup is required!

## Manual Run

You can manually trigger the workflow from the GitHub Actions tab:
1. Go to your repository on GitHub
2. Click on the **Actions** tab
3. Select **Track Clone Statistics** workflow
4. Click **Run workflow**

## Viewing Statistics

After the workflow runs, check `clone_statistics.json` in the root of your repository:

```json
{
  "repository": "Knuckles92/OpenWhisper",
  "schema_version": 2,
  "lifetime_stats": {
    "total_clones": 247,
    "days_tracked": 30,
    "tracking_start_date": "2026-01-14"
  },
  "daily_history": {
    "2026-01-14": {"clones": 12, "unique_cloners": 8},
    "2026-01-15": {"clones": 15, "unique_cloners": 11}
  },
  "monthly_summaries": {
    "2026-01": {"clones": 89, "unique_cloners": 45, "days_with_data": 17}
  },
  "last_14_days": {
    "clones": 52,
    "unique_cloners": 31
  }
}
```

## Data Structure

| Field | Description |
|-------|-------------|
| `lifetime_stats` | Aggregated totals since tracking started |
| `daily_history` | Per-day breakdown (keyed by date for fast lookups) |
| `monthly_summaries` | Monthly aggregations for compact long-term storage |
| `last_14_days` | Current 14-day window from GitHub's API |

## Notes

- The workflow uses `[skip ci]` in commit messages to prevent infinite loops
- Statistics are tracked starting from when you first run this workflow
- If a workflow run is missed, that day won't be counted (GitHub doesn't store older data)
- Monthly summaries help keep the file manageable over years of tracking
