#!/usr/bin/env python3
"""
Victoria Metrics - List all metrics with statistics
Usage: ./01-metrics-list.py [-u <vm-url>] [-o <output-file>] [-f <filter-file>]
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="List all Victoria Metrics metrics with statistics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -u http://localhost:8428
  %(prog)s -u http://vm.example.com -o report.json -f filters.yaml
        """
    )
    parser.add_argument(
        "-u", "--url",
        default="http://localhost:8428",
        help="Victoria Metrics URL (default: http://localhost:8428)"
    )
    parser.add_argument(
        "-o", "--output",
        default="metrics-report.json",
        help="Output JSON report file (default: metrics-report.json)"
    )
    parser.add_argument(
        "-f", "--filter",
        help="Path to filter patterns YAML file"
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="Number of parallel workers (default: CPU count)"
    )
    return parser.parse_args()


def load_filter_patterns(filter_file: str) -> str:
    """
    Load filter patterns from a YAML file.
    Returns regex pattern string (patterns joined with |).
    """
    if not filter_file or not os.path.exists(filter_file):
        return ""

    try:
        with open(filter_file, 'r') as f:
            content = f.read()

        pattern = r'^\s*-\s*"([^"]*)"'
        matches = re.findall(pattern, content, re.MULTILINE)

        if not matches:
            return ""

        return '|'.join(matches)
    except Exception as e:
        print(f"Warning: Error reading filter file '{filter_file}': {e}")
        return ""


def fetch_metric_names(vm_url: str) -> list[str]:
    """Fetch all unique metric names from Victoria Metrics."""
    url = f"{vm_url}/api/v1/label/__name__/values"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data.get('data', [])
    except requests.RequestException as e:
        print(f"Error fetching metric names: {e}")
        return []


def get_metric_stats(vm_url: str, metric: str) -> dict[str, Any]:
    """
    Process a single metric and return its statistics.
    """
    end_time = int(datetime.now(timezone.utc).timestamp())
    start_time = end_time - 86400

    result_data = {
        "name": metric,
        "series_count": 0,
        "actual_data_points_retrieved": 0,
        "approx_data_points_24h": 0,
        "approx_data_points_full_retention": 0,
        "labels": [],
        "estimated_size_mb": 0,
        "estimated_size_24h_mb": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "first_date": None,
        "last_date": None,
        "data_retention_days": None
    }

    try:
        url = f"{vm_url}/api/v1/query_range?query={metric}&start={start_time}&end={end_time}&step=5s"
        response = requests.get(url, timeout=60)

        if response.status_code != 200:
            return result_data

        data = response.json()
        result = data.get('data', {}).get('result', [])

        if not result:
            return result_data

        series_count = len(result)
        total_data_points = 0
        first_ts = None
        last_ts = None

        for series in result:
            values = series.get('values', [])
            total_data_points += len(values) // 2

            for value in values:
                if value and len(value) >= 1:
                    ts = int(float(value[0]))
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts

        result_data["series_count"] = series_count
        result_data["actual_data_points_retrieved"] = total_data_points

        if total_data_points > 0 and first_ts is not None and last_ts is not None:
            time_range = last_ts - first_ts
            if time_range > 0:
                approx_points = int(total_data_points * 86400 / time_range)
            else:
                approx_points = total_data_points
        else:
            approx_points = 0

        result_data["approx_data_points_24h"] = approx_points

        first_timestamp = None
        last_timestamp = None

        if series_count > 0:
            early_start = end_time - 157680000

            series_url = f"{vm_url}/api/v1/series?match[]={metric}"
            series_response = requests.get(series_url, timeout=30)

            if series_response.status_code == 200:
                series_data = series_response.json().get('data', [])
                if series_data:
                    first_response = requests.get(
                        f"{vm_url}/api/v1/query_range?query={metric}&start={early_start}&end={end_time}&step=86400s",
                        timeout=60
                    )
                    if first_response.status_code == 200:
                        first_data = first_response.json().get('data', {}).get('result', [])
                        all_timestamps = []
                        for s in first_data:
                            for v in s.get('values', []):
                                if v and len(v) >= 1:
                                    all_timestamps.append(int(float(v[0])))
                        if all_timestamps:
                            first_timestamp = min(all_timestamps)

                    if first_timestamp is None:
                        recent_start = end_time - 15552000
                        first_response = requests.get(
                            f"{vm_url}/api/v1/query_range?query={metric}&start={recent_start}&end={end_time}&step=3600s",
                            timeout=60
                        )
                        if first_response.status_code == 200:
                            first_data = first_response.json().get('data', {}).get('result', [])
                            all_timestamps = []
                            for s in first_data:
                                for v in s.get('values', []):
                                    if v and len(v) >= 1:
                                        all_timestamps.append(int(float(v[0])))
                            if all_timestamps:
                                first_timestamp = min(all_timestamps)

            latest_response = requests.get(f"{vm_url}/api/v1/query?query={metric}", timeout=30)
            if latest_response.status_code == 200:
                latest_data = latest_response.json().get('data', {}).get('result', [])
                latest_timestamps = []
                for s in latest_data:
                    if 'value' in s:
                        latest_timestamps.append(int(float(s['value'][0])))
                if latest_timestamps:
                    last_timestamp = max(latest_timestamps)

        result_data["first_timestamp"] = first_timestamp
        result_data["last_timestamp"] = last_timestamp

        first_date = None
        last_date = None
        data_retention_days = None

        if first_timestamp is not None and last_timestamp is not None:
            try:
                first_dt = datetime.fromtimestamp(first_timestamp, tz=timezone.utc)
                last_dt = datetime.fromtimestamp(last_timestamp, tz=timezone.utc)
                first_date = first_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
                last_date = last_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
                data_retention_days = (last_timestamp - first_timestamp) // 86400
            except (ValueError, OSError):
                pass

        result_data["first_date"] = first_date
        result_data["last_date"] = last_date
        result_data["data_retention_days"] = data_retention_days

        labels_response = requests.get(
            f"{vm_url}/api/v1/series?match[]={metric}&start={start_time}&end={end_time}",
            timeout=30
        )
        if labels_response.status_code == 200:
            labels_data = labels_response.json().get('data', [])
            if labels_data and len(labels_data) > 0:
                result_data["labels"] = list(labels_data[0].keys())

        if data_retention_days is not None and data_retention_days > 0:
            full_approx_points = approx_points * data_retention_days
            estimated_size_mb = full_approx_points // 1048576
        else:
            full_approx_points = approx_points
            estimated_size_mb = (approx_points * 2) // 1048576

        result_data["approx_data_points_full_retention"] = full_approx_points
        result_data["estimated_size_mb"] = estimated_size_mb
        result_data["estimated_size_24h_mb"] = (approx_points * 2) // 1048576 if approx_points > 0 else 0

    except requests.RequestException as e:
        print(f"Error processing metric '{metric}': {e}")
    except Exception as e:
        print(f"Unexpected error processing metric '{metric}': {e}")

    return result_data


def main() -> int:
    args = parse_args()

    num_workers = args.parallel if args.parallel else (os.cpu_count() or 4)

    print(f"Analyzing Victoria Metrics instance at: {args.url}")
    if args.filter:
        print(f"Filter patterns file: {args.filter}")
    else:
        print("No filter file specified - processing all metrics")

    filter_patterns = load_filter_patterns(args.filter or "")

    print(f"Output will be saved to: {args.output}")

    if filter_patterns:
        print(f"Using filter patterns: {filter_patterns}")
    else:
        print("No filter file provided - will process all metrics")

    print("Fetching all metric names...")
    all_metrics = fetch_metric_names(args.url)
    total_metrics = len(all_metrics)
    print(f"Found {total_metrics} unique metrics")

    if filter_patterns:
        print(f"Filtering metrics with pattern: {filter_patterns}")
        pattern = re.compile(filter_patterns)
        filtered_metrics = [m for m in all_metrics if pattern.search(m)]
        filtered_count = len(filtered_metrics)
        print(f"Filtered to {filtered_count} metrics matching the patterns")
        all_metrics = filtered_metrics
        total_metrics = filtered_count
    else:
        print(f"No filtering applied - analyzing all {total_metrics} metrics")

    if total_metrics == 0:
        print("No metrics to process.")
        return 0

    print(f"Analyzing each metric (this may take a while)...")

    results: list[dict[str, Any]] = []

    for i, metric in enumerate(all_metrics, 1):
        print(f"Processing metric {i}/{total_metrics}: {metric}")

        result = get_metric_stats(args.url, metric)
        results.append(result)

        if result.get("series_count", 0) > 0:
            print(f"  Series count for {metric}: {result['series_count']}")
            print(f"  Actual data points in response: {result['actual_data_points_retrieved']}")
            print(f"  Estimated points for 24h: {result['approx_data_points_24h']}")
            if result.get("data_retention_days"):
                print(f"  Estimated size (full retention): {result['estimated_size_mb']} MB for {result['data_retention_days']} days")
            else:
                print(f"  Estimated size (24h): {result['estimated_size_mb']} MB")

    total_series = sum(m.get('series_count', 0) for m in results)
    total_approx_24h = sum(m.get('approx_data_points_24h', 0) for m in results)
    total_approx_retention = sum(m.get('approx_data_points_full_retention', 0) for m in results)
    total_size = sum(m.get('estimated_size_mb', 0) for m in results)
    total_size_24h = sum(m.get('estimated_size_24h_mb', 0) for m in results)

    metrics_with_data = [m for m in results if m.get('series_count', 0) > 0]
    metrics_with_data_count = len(metrics_with_data)

    first_dates = [m['first_date'] for m in results if m.get('first_date')]
    last_dates = [m['last_date'] for m in results if m.get('last_date')]
    retentions = [m['data_retention_days'] for m in results if m.get('data_retention_days')]

    oldest_data = min(first_dates) if first_dates else "N/A"
    newest_data = max(last_dates) if last_dates else "N/A"
    avg_retention = sum(retentions) / len(retentions) if retentions else 0

    top_10_by_series = sorted(results, key=lambda x: x.get('series_count', 0), reverse=True)[:10]
    top_10_by_size = sorted(results, key=lambda x: x.get('estimated_size_mb', 0), reverse=True)[:10]
    top_10_by_retention = sorted(results, key=lambda x: x.get('data_retention_days', 0) or 0, reverse=True)[:10]

    report = {
        "analysis_timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "vm_instance": args.url,
        "total_metrics": total_metrics,
        "metrics": results,
        "summary": {
            "total_series": total_series,
            "total_estimated_points_24h": total_approx_24h,
            "total_estimated_points_full_retention": total_approx_retention,
            "total_estimated_size_mb": total_size,
            "total_estimated_size_24h_mb": total_size_24h,
            "metrics_with_data": metrics_with_data_count,
            "oldest_data_date": oldest_data,
            "newest_data_date": newest_data,
            "avg_retention_days": int(avg_retention),
            "top_10_by_series": top_10_by_series,
            "top_10_by_size": top_10_by_size,
            "top_10_by_retention": top_10_by_retention
        }
    }

    print(f"Analysis complete! Report saved to: {args.output}")
    print("Summary:")
    print(f"Total metrics: {total_series}")
    print(f"Metrics with data: {metrics_with_data_count}")
    print(f"Estimated data points (24h): {total_approx_24h}")
    print(f"Estimated data points (full retention): {total_approx_retention}")
    print(f"Estimated size (24h): {total_size_24h} MB")
    print(f"Estimated size (full retention): {total_size} MB")
    print(f"Oldest data: {oldest_data}")
    print(f"Newest data: {newest_data}")
    print(f"Average retention: {int(avg_retention)} days")

    try:
        with open(args.output, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"Report successfully written to '{args.output}'")
    except IOError as e:
        print(f"Error writing report file: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
