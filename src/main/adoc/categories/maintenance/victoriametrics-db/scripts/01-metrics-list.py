#!/usr/bin/env python3
"""
Victoria Metrics - List all metrics with statistics
Usage: ./01-metrics-list.py [-u <vm-url>] [-o <output-file>] [-f <filter-file>] [--render] [--html-output <path>] [--template <path>]
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import reduce
from typing import Any

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List all Victoria Metrics metrics with statistics"
    )
    parser.add_argument("-u", "--url", default="http://victoriametrics:8428")
    parser.add_argument("-o", "--output", default="metrics-report.json")
    parser.add_argument("-f", "--filter", help="Path to filter patterns YAML file")
    parser.add_argument("--parallel", type=int, default=None, help="Number of parallel workers")
    parser.add_argument("--render", action="store_true", help="Generate HTML report after JSON output")
    parser.add_argument("--html-output", default=None, help="Output path for HTML report (default: <json-output>.html)")
    parser.add_argument("--template", default=None, help="Custom Jinja2 template path")
    return parser.parse_args()


def load_filter_patterns(filter_file: str) -> str:
    if not filter_file or not os.path.exists(filter_file):
        return ""
    try:
        with open(filter_file, 'r') as f:
            content = f.read()
        matches = re.findall(r'^\s*-\s*"([^"]*)"', content, re.MULTILINE)
        return '|'.join(matches) if matches else ""
    except Exception as e:
        print(f"Warning: Error reading filter file '{filter_file}': {e}")
        return ""


def fetch_metric_names(vm_url: str) -> list[str]:
    try:
        response = requests.get(f"{vm_url}/api/v1/label/__name__/values", timeout=30)
        response.raise_for_status()
        return response.json().get('data', [])
    except requests.RequestException as e:
        print(f"Error fetching metric names: {e}")
        return []


def get_metric_stats(session: requests.Session, vm_url: str, metric: str, end_time: int) -> dict[str, Any]:
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
        response = session.get(
            f"{vm_url}/api/v1/query_range",
            params={"query": metric, "start": start_time, "end": end_time, "step": "5s"},
            timeout=60
        )

        if response.status_code != 200:
            return result_data

        result = response.json().get('data', {}).get('result', [])
        if not result:
            return result_data

        series_count = len(result)
        total_data_points = 0
        first_ts = None
        last_ts = None

        for series in result:
            values = series.get('values', [])
            data_points = len(values) // 2
            total_data_points += data_points

            if values:
                first_val_ts = int(float(values[0][0]))
                last_val_ts = int(float(values[-1][0]))
                if first_ts is None or first_val_ts < first_ts:
                    first_ts = first_val_ts
                if last_ts is None or last_val_ts > last_ts:
                    last_ts = last_val_ts

        result_data["series_count"] = series_count
        result_data["actual_data_points_retrieved"] = total_data_points

        if total_data_points > 0 and first_ts is not None and last_ts is not None:
            time_range = last_ts - first_ts
            approx_points = int(total_data_points * 86400 / time_range) if time_range > 0 else total_data_points
        else:
            approx_points = 0

        result_data["approx_data_points_24h"] = approx_points

        series_response = session.get(
            f"{vm_url}/api/v1/series",
            params={"match[]": metric},
            timeout=30
        )

        first_timestamp = None
        if series_response.status_code == 200 and series_response.json().get('data'):
            first_response = session.get(
                f"{vm_url}/api/v1/query_range",
                params={"query": metric, "start": end_time - 157680000, "end": end_time, "step": "86400s"},
                timeout=60
            )
            if first_response.status_code == 200:
                all_ts = [
                    int(float(v[0]))
                    for s in first_response.json().get('data', {}).get('result', [])
                    for v in s.get('values', [])
                ]
                first_timestamp = min(all_ts) if all_ts else None

            if first_timestamp is None:
                first_response = session.get(
                    f"{vm_url}/api/v1/query_range",
                    params={"query": metric, "start": end_time - 15552000, "end": end_time, "step": "3600s"},
                    timeout=60
                )
                if first_response.status_code == 200:
                    all_ts = [
                        int(float(v[0]))
                        for s in first_response.json().get('data', {}).get('result', [])
                        for v in s.get('values', [])
                    ]
                    first_timestamp = min(all_ts) if all_ts else None

        latest_response = session.get(
            f"{vm_url}/api/v1/query",
            params={"query": metric},
            timeout=30
        )
        last_timestamp = None
        if latest_response.status_code == 200:
            all_ts = [
                int(float(s['value'][0]))
                for s in latest_response.json().get('data', {}).get('result', [])
                if 'value' in s
            ]
            last_timestamp = max(all_ts) if all_ts else None

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

        labels_response = session.get(
            f"{vm_url}/api/v1/series",
            params={"match[]": metric, "start": start_time, "end": end_time},
            timeout=30
        )
        if labels_response.status_code == 200:
            labels_data = labels_response.json().get('data', [])
            if labels_data:
                result_data["labels"] = list(labels_data[0].keys())

        if data_retention_days and data_retention_days > 0:
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


def process_metric_parallel(args_tuple: tuple) -> dict[str, Any]:
    session, vm_url, metric, end_time = args_tuple
    return get_metric_stats(session, vm_url, metric, end_time)


def render_html(report: dict[str, Any], output_path: str, template_path: str | None) -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))

    if template_path:
        template_dir = os.path.dirname(os.path.abspath(template_path))
        template_name = os.path.basename(template_path)
    else:
        template_dir = script_dir
        template_name = "01-metrics-report.html.j2"

    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(['html', 'xml'])
    )
    env.filters['default'] = lambda val, default: val if val is not None else default

    template = env.get_template(template_name)

    html_content = template.render(
        vm_instance=report.get('vm_instance', 'N/A'),
        analysis_timestamp=report.get('analysis_timestamp', 'N/A'),
        total_metrics=report.get('total_metrics', 0),
        summary=report.get('summary', {}),
        json_data=report
    )

    with open(output_path, 'w') as f:
        f.write(html_content)

    print(f"HTML report generated: {output_path}")


def main() -> int:
    args = parse_args()

    num_workers = args.parallel if args.parallel else (os.cpu_count() or 4)
    use_parallel = num_workers > 1

    print(f"Analyzing Victoria Metrics instance at: {args.url}")
    print(f"Filter patterns file: {args.filter}" if args.filter else "No filter file specified")

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
        pattern = re.compile(filter_patterns)
        all_metrics = [m for m in all_metrics if pattern.search(m)]
        total_metrics = len(all_metrics)
        print(f"Filtered to {total_metrics} metrics matching the patterns")
    else:
        print(f"No filtering applied - analyzing all {total_metrics} metrics")

    if total_metrics == 0:
        print("No metrics to process.")
        return 0

    end_time = int(datetime.now(timezone.utc).timestamp())
    results: list[dict[str, Any]] = []

    if use_parallel:
        print(f"Processing {total_metrics} metrics with {num_workers} parallel workers...")

        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=num_workers, pool_maxsize=num_workers)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(process_metric_parallel, (session, args.url, metric, end_time)): metric
                for metric in all_metrics
            }

            completed = 0
            for future in as_completed(futures):
                metric = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"Error processing metric '{metric}': {e}")

                completed += 1
                if completed % 10 == 0 or completed == total_metrics:
                    print(f"Progress: {completed}/{total_metrics} ({100*completed/total_metrics:.1f}%)")

    else:
        print("Analyzing each metric (this may take a while)...")

        session = requests.Session()

        for i, metric in enumerate(all_metrics, 1):
            print(f"Processing metric {i}/{total_metrics}: {metric}")

            result = get_metric_stats(session, args.url, metric, end_time)
            results.append(result)

            if result.get("series_count", 0) > 0:
                print(f"  Series: {result['series_count']}, Points: {result['actual_data_points_retrieved']}, Est 24h: {result['approx_data_points_24h']}")
                if result.get("data_retention_days"):
                    print(f"  Size: {result['estimated_size_mb']} MB for {result['data_retention_days']} days")
                else:
                    print(f"  Size: {result['estimated_size_mb']} MB")

    results.sort(key=lambda x: x.get('name', ''))

    total_series = sum(m.get('series_count', 0) for m in results)
    total_approx_24h = sum(m.get('approx_data_points_24h', 0) for m in results)
    total_approx_retention = sum(m.get('approx_data_points_full_retention', 0) for m in results)
    total_size = sum(m.get('estimated_size_mb', 0) for m in results)
    total_size_24h = sum(m.get('estimated_size_24h_mb', 0) for m in results)

    metrics_with_data_count = sum(1 for m in results if m.get('series_count', 0) > 0)

    first_dates = [m['first_date'] for m in results if m.get('first_date')]
    last_dates = [m['last_date'] for m in results if m.get('last_date')]
    retentions = [m['data_retention_days'] for m in results if m.get('data_retention_days')]

    oldest_data = min(first_dates) if first_dates else "N/A"
    newest_data = max(last_dates) if last_dates else "N/A"
    avg_retention = sum(retentions) / len(retentions) if retentions else 0

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
            "top_10_by_series": sorted(results, key=lambda x: x.get('series_count', 0), reverse=True)[:10],
            "top_10_by_size": sorted(results, key=lambda x: x.get('estimated_size_mb', 0), reverse=True)[:10],
            "top_10_by_retention": sorted(results, key=lambda x: x.get('data_retention_days', 0) or 0, reverse=True)[:10]
        }
    }

    print(f"\nAnalysis complete! Report saved to: {args.output}")
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

    if args.render:
        html_output = args.html_output if args.html_output else args.output.replace('.json', '.html')
        try:
            render_html(report, html_output, args.template)
        except Exception as e:
            print(f"Error generating HTML report: {e}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
